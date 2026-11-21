# Ported from narezkobot/narezka-restored; comments and docstrings omitted for transport.
from __future__ import annotations
import banner_blur
import argparse
import functools
from fractions import Fraction
import bisect
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    import low_impact as _low_impact
except ImportError:  # stream_cutter можно запускать и отдельно
    _low_impact = None

def _proc_flags() -> int:
    if _low_impact is not None:
        return _low_impact.creation_flags()
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)
ANALYSIS_RATE = 4000
FRAME = 160
STRIDE = 2
VIDEO = 'C:\\Users\\danil\\PycharmProjects\\pythonProject2\\recordings\\[9-23-26] Nix - Прекрасные серые будни.mp4'
FAST = False
JOBS = 0
HWACCEL = 'auto'
SPEED = 'turbo'
# eco: 2 процесса NVENC, пресет p4, потоки только из «бюджета» ядер (low_impact): рендер дольше,
# но процессор и память остаются для игры. turbo: максимум скорости, грузит всё.
SPEED_PRESETS = {'eco': {'preset': 'superfast', 'nvenc_preset': 'p4', 'nvenc_jobs': 2, 'x264_div': 6, 'x264_max_jobs': 2, 'crf_bonus': 0, 'nvenc_tune': None}, 'quality': {'preset': 'veryfast', 'nvenc_preset': 'p5', 'nvenc_jobs': 2, 'x264_div': 4, 'x264_max_jobs': 6, 'crf_bonus': 0, 'nvenc_tune': None}, 'fast': {'preset': 'superfast', 'nvenc_preset': 'p3', 'nvenc_jobs': 3, 'x264_div': 4, 'x264_max_jobs': 6, 'crf_bonus': 0, 'nvenc_tune': None}, 'max': {'preset': 'ultrafast', 'nvenc_preset': 'p1', 'nvenc_jobs': 3, 'x264_div': 4, 'x264_max_jobs': 6, 'crf_bonus': 0, 'nvenc_tune': None}, 'turbo': {'preset': 'ultrafast', 'nvenc_preset': 'p1', 'nvenc_jobs': 4, 'x264_div': 2, 'x264_max_jobs': 12, 'crf_bonus': 3, 'nvenc_tune': 'll'}}
BLUR_REGIONS: list[tuple[int, int, int, int]] = []
BLUR_STRENGTH = 20

class CutterError(Exception):
    pass

class InvalidRenderTimeline(CutterError):
    pass

def run(cmd: list[str], capture: bool=True, timeout: int | None=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None, text=True, encoding='utf-8', errors='replace', timeout=timeout, creationflags=_proc_flags())

def run_bytes(cmd: list[str], timeout: int | None=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

def require_tools() -> tuple[str, str]:
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise CutterError('ffmpeg/ffprobe not found in PATH')
    return (ffmpeg, ffprobe)

def probe(path: str, ffprobe: str) -> dict:
    p = run([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path])
    if p.returncode != 0:
        raise CutterError(f'ffprobe failed: {p.stderr.strip()}')
    return json.loads(p.stdout)

def ffloat(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def audio_streams(info: dict) -> list[dict]:
    return [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']

def video_stream(info: dict) -> dict | None:
    for s in info.get('streams', []):
        if s.get('codec_type') == 'video':
            return s
    return None

def duration_of(info: dict) -> float:
    dur = ffloat(info.get('format', {}).get('duration'))
    if dur <= 0:
        dur = max((ffloat(s.get('duration')) for s in info['streams']), default=0.0)
    return dur

def describe_audio(info: dict) -> list[str]:
    lines = []
    for i, s in enumerate(audio_streams(info)):
        tags = s.get('tags', {}) or {}
        title = tags.get('title') or tags.get('handler_name') or ''
        ch = s.get('channels') or 0
        layout = s.get('channel_layout') or f'{ch}ch'
        if not layout.endswith('ch'):
            layout += 'ch'
        lines.append(f"  [0:a:{i}] {s.get('codec_name', '?')} {layout} {s.get('sample_rate', '?')}Hz  {title}")
    return lines
COMMON_FPS: list[tuple[float, str]] = [(23.976, '24000/1001'), (24.0, '24'), (25.0, '25'), (29.97, '30000/1001'), (30.0, '30'), (48.0, '48'), (50.0, '50'), (59.94, '60000/1001'), (60.0, '60'), (120.0, '120')]

def clean_fps(num: int, den: int) -> str:
    value = num / den
    best, best_diff = (None, None)
    for target, frac in COMMON_FPS:
        diff = abs(value - target) / target
        if best_diff is None or diff < best_diff:
            best, best_diff = (frac, diff)
    if best is not None and best_diff is not None and (best_diff < 0.0005):
        return best
    return f'{num}/{den}'

def fps_of(info: dict) -> str | None:
    v = video_stream(info)
    if not v:
        return None
    for key in ('avg_frame_rate', 'r_frame_rate'):
        r = v.get(key) or '0/0'
        try:
            num, den = r.split('/')
            num, den = (int(num), int(den))
        except (ValueError, AttributeError):
            continue
        if num > 0 and den > 0 and (num / den < 200):
            return clean_fps(num, den)
    return None

def pick_mic_stream(info: dict, forced: str | None) -> int:
    streams = audio_streams(info)
    if not streams:
        raise CutterError('no audio streams in the file')
    if forced not in (None, 'auto'):
        if forced == 'mix':
            return -1
        idx = int(forced)
        if not 0 <= idx < len(streams):
            raise CutterError(f'audio track {idx} does not exist (have {len(streams)})')
        return idx
    if len(streams) == 1:
        return 0
    mono = [i for i, s in enumerate(streams) if int(s.get('channels') or 0) == 1]
    if mono:
        return mono[0]
    return 0

def decode_pcm(path: str, stream_idx: int, ffmpeg: str, start: float=0, end: float | None=None) -> bytes:
    cmd = [ffmpeg, '-v', 'error']
    if start > 0: cmd += ['-ss', f'{start:.6f}']
    cmd += ['-i', path]
    if end is not None: cmd += ['-t', f'{end-start:.6f}']
    cmd += ['-map', f'0:a:{stream_idx}', '-vn', '-ac', '1', '-ar', str(ANALYSIS_RATE), '-c:a', 'pcm_s16le', '-f', 's16le', '-']
    p = run_bytes(cmd)
    if p.returncode != 0:
        raise CutterError(f"audio decode failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout

def _levels_numpy(buf: bytes) -> array:
    import numpy as np
    a = np.frombuffer(buf, dtype='<i2')
    n = len(a) // FRAME
    if n == 0:
        return array('f')
    a = a[:n * FRAME].reshape(n, FRAME).astype(np.float32)
    rms = np.sqrt(np.einsum('ij,ij->i', a, a) / FRAME)
    lvl = 20.0 * np.log10(rms / 32768.0 + 1e-09)
    return array('f', lvl.tolist())

def _levels_audioop(buf: bytes) -> array:
    import audioop
    step = FRAME * 2
    n = len(buf) // step
    out = array('f', bytes(4 * n))
    log10 = math.log10
    for i in range(n):
        rms = audioop.rms(buf[i * step:(i + 1) * step], 2)
        out[i] = 20.0 * log10(rms / 32768.0 + 1e-09)
    return out

def _levels_python(buf: bytes) -> array:
    data = array('h')
    data.frombytes(buf[:len(buf) // 2 * 2])
    n = len(data) // FRAME
    out = array('f', bytes(4 * n))
    norm = 32768.0
    log10 = math.log10
    sqrt = math.sqrt
    for i in range(n):
        base = i * FRAME
        acc = 0
        for j in range(base, base + FRAME, STRIDE):
            v = data[j]
            acc += v * v
        out[i] = 20.0 * log10(sqrt(acc / (FRAME // STRIDE)) / norm + 1e-09)
    return out

def frame_levels(buf: bytes) -> tuple[array, str]:
    for name, fn in (('numpy', _levels_numpy), ('audioop', _levels_audioop)):
        try:
            return (fn(buf), name)
        except ImportError:
            continue
    return (_levels_python(buf), 'pure-python (slow, поставьте numpy)')

def percentile(values: list[float], pct: float) -> float:
    if not values:
        return -90.0
    values = sorted(values)
    k = int((len(values) - 1) * pct)
    return values[k]

def voice_thresholds(floor: float, args) -> tuple[float, float]:
    if args.silence_db is not None:
        on_db = args.silence_db
        gap = max(1.0, args.onset_db - args.offset_db)
    else:
        on_db = floor + args.onset_db
        gap = args.onset_db - args.offset_db
    gap = max(0.5, gap)
    off_db = on_db - gap
    off_db = max(off_db, floor + 1.0)
    off_db = min(off_db, on_db - 0.5)
    return (on_db, off_db)

def detect_speech(levels: array, on: float, off: float, min_speech: float) -> list[tuple[float, float]]:
    frame_dur = FRAME / ANALYSIS_RATE
    segments: list[tuple[float, float]] = []
    start: int | None = None
    last_loud = -999
    gap_limit = max(1, int(round(0.25 / frame_dur)))
    for i, lvl in enumerate(levels):
        if lvl >= on:
            if start is None:
                start = i
            last_loud = i
        elif start is not None and lvl < off and (i - last_loud > gap_limit):
            segments.append((start * frame_dur, (i - gap_limit) * frame_dur))
            start = None
    if start is not None:
        segments.append((start * frame_dur, len(levels) * frame_dur))
    return [(s, e) for s, e in segments if e - s >= min_speech]

def merge_gaps(segs, max_gap: float):
    out = []
    for s, e in segs:
        if out and s - out[-1][1] <= max_gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

def pad(segs, before: float, after: float, total: float):
    out = []
    for s, e in segs:
        a = max(0.0, s - before)
        b = min(total, e + after) if total > 0 else e + after
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out

def complement(segs, total: float):
    out = []
    prev = 0.0
    for s, e in segs:
        if s > prev:
            out.append((prev, s))
        prev = max(prev, e)
    if prev < total:
        out.append((prev, total))
    return out

def sweep(levels, floor: float, args, total: float) -> None:
    onsets = [6.0, 8.0, 10.0, 12.0, 14.0]
    min_cuts = [1.0, 2.0, 3.0, 5.0]
    print('\nподбор порогов - сколько % стрима будет вырезано:')
    print('  голос выше шума |' + ''.join((f'  cut>={mc:.0f}s ' for mc in min_cuts)))
    print('  ----------------+' + ''.join(('---------' for _ in min_cuts)))
    for on in onsets:
        on_db = floor + on
        off_db = on_db - max(1.0, args.onset_db - args.offset_db)
        sp = detect_speech(levels, on_db, off_db, args.min_speech)
        cells = []
        for mc in min_cuts:
            a = copy.copy(args)
            a.min_cut = mc
            _, cuts = build_plan(sp, total, a)
            removed = sum((e - s for s, e in cuts))
            cells.append(f'  {removed / total * 100:6.1f}% ')
        print(f'  {on:>15.0f} dB |' + ''.join(cells))
    print()

def build_plan(speech, total: float, args):
    if not speech:
        return ([], [(0.0, total)])
    keep = merge_gaps(speech, args.max_gap)
    keep = pad(keep, args.pad_before, args.pad_after, total)

    def speech_time(r):
        return sum((max(0.0, min(e, r[1]) - max(s, r[0])) for s, e in speech))
    keep = [r for r in keep if speech_time(r) >= args.min_speech]
    cuts = [c for c in complement(keep, total) if c[1] - c[0] >= args.min_cut]
    keep = complement(cuts, total) if cuts else [(0.0, total)]
    return (keep, cuts)

def fmt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'

def hhmmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'

def video_size(info: dict) -> tuple[int, int]:
    v = video_stream(info) or {}
    try:
        return (int(v.get('width') or 0), int(v.get('height') or 0))
    except (TypeError, ValueError):
        return (0, 0)

def parse_region(spec: str) -> tuple[int, int, int, int] | None:
    try:
        parts = [int(p.strip()) for p in str(spec).replace(';', ',').split(',')]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    x, y, w, h = parts
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)

def norm_region(raw) -> tuple[int, int, int, int] | None:
    try:
        if all((k in raw for k in ('x', 'y', 'w', 'h'))):
            x, y = (int(raw['x']), int(raw['y']))
            w, h = (int(raw['w']), int(raw['h']))
        else:
            x1, y1 = (int(raw['x1']), int(raw['y1']))
            x2, y2 = (int(raw['x2']), int(raw['y2']))
            x, y, w, h = (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)

def clamp_regions(regions, width: int, height: int):
    if not width or not height:
        return list(regions)
    out = []
    for x, y, w, h in regions:
        x0 = max(0, min(int(x), width - 1))
        y0 = max(0, min(int(y), height - 1))
        x1 = max(x0 + 1, min(int(x) + int(w), width))
        y1 = max(y0 + 1, min(int(y) + int(h), height))
        out.append((x0, y0, x1 - x0, y1 - y0))
    return out

def blur_filter(regions, strength) -> str | None:
    if not regions:
        return None
    radius = max(1, min(40, int(strength)))
    n = len(regions)
    parts = [f'split={n + 1}[base]' + ''.join((f'[c{i}]' for i in range(n)))]
    for i, (x, y, w, h) in enumerate(regions):
        parts.append(f'[c{i}]crop={w}:{h}:{x}:{y},boxblur={radius}:2[r{i}]')
    prev = '[base]'
    for i, (x, y, w, h) in enumerate(regions):
        if i == n - 1:
            parts.append(f'{prev}[r{i}]overlay={x}:{y}')
        else:
            parts.append(f'{prev}[r{i}]overlay={x}:{y}[o{i}]')
            prev = f'[o{i}]'
    return ';'.join(parts)

def bump_rate(value: str) -> str | None:
    s = str(value).strip()
    for suffix in ('M', 'm', 'K', 'k', ''):
        if s.endswith(suffix) and s[:len(s) - len(suffix)].isdigit():
            num = int(s[:len(s) - len(suffix)] or 0)
            return f'{num * 2}{suffix}'
    return None

def encode_args(args) -> list[str]:
    extra: list[str] = []
    maxrate = getattr(args, 'maxrate', None)
    if maxrate:
        extra += ['-maxrate', str(maxrate)]
        bufsize = bump_rate(maxrate)
        if bufsize:
            extra += ['-bufsize', bufsize]
    if args.vcodec.endswith('_nvenc'):
        bonus = int(getattr(args, 'crf_bonus', 0) or 0)
        cmd = [args.vcodec, '-preset', args.nvenc_preset]
        ntune = getattr(args, 'nvenc_tune', None)
        if ntune:
            cmd += ['-tune', str(ntune)]
        return cmd + ['-rc', 'vbr', '-cq', f'{float(args.cq) + bonus:g}', *extra]
    bonus = int(getattr(args, 'crf_bonus', 0) or 0)
    cmd = [args.vcodec, '-crf', f'{float(args.crf) + bonus:g}', '-preset', args.preset]
    tune = getattr(args, 'tune', None)
    if tune:
        cmd += ['-tune', str(tune)]
    return cmd + extra

def final_mux_args(out: str) -> list[str]:
    ext = os.path.splitext(out)[1].lower()
    if ext in ('.mp4', '.m4v', '.mov'):
        return ['-movflags', '+faststart']
    return []

@functools.lru_cache(maxsize=8)
def pick_encoder(ffmpeg: str, requested: str) -> str:
    if requested != 'auto':
        return requested
    try:
        listed = run([ffmpeg, '-hide_banner', '-encoders']).stdout or ''
    except OSError:
        return 'libx264'
    if 'h264_nvenc' in listed:
        test = [ffmpeg, '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=256x144:d=1', '-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'vbr', '-cq', '20', '-f', 'null', '-']
        if run(test, timeout=60).returncode == 0:
            return 'h264_nvenc'
    return 'libx264'

@functools.lru_cache(maxsize=8)
def fps_mode_flag(ffmpeg: str) -> list[str]:
    test = [ffmpeg, '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=64x64:d=0.2', '-fps_mode:v', 'cfr', '-f', 'null', '-']
    try:
        if run(test, timeout=60).returncode == 0:
            return ['-fps_mode:v', 'cfr']
    except subprocess.TimeoutExpired:
        pass
    return ['-vsync', 'cfr']

def detect_hwaccel(ffmpeg: str, src: str, want: str, vcodec: str) -> str | None:
    if want == 'none':
        return None
    if want == 'auto':
        cands = ['cuda', 'd3d11va', 'dxva2'] if os.name == 'nt' else ['cuda', 'vaapi']
    else:
        cands = [want]
    if vcodec.endswith('_nvenc'):
        enc = [vcodec, '-preset', 'p4']
    else:
        enc = ['libx264', '-preset', 'ultrafast', '-crf', '40']
    for h in cands:
        cmd = [ffmpeg, '-hide_banner', '-v', 'error', '-hwaccel', h, '-ss', '1', '-t', '2', '-i', src, '-map', '0:v:0', '-an', '-c:v', *enc, '-f', 'null', '-']
        try:
            if run(cmd, timeout=120).returncode == 0:
                return h
        except subprocess.TimeoutExpired:
            continue
    return None

def keyframes(ffprobe: str, src: str) -> list[float]:
    p = run([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'packet=pts_time,flags', '-of', 'csv=p=0', src])
    times = []
    for line in (p.stdout or '').splitlines():
        parts = [x.strip() for x in line.split(',')]
        if len(parts) < 2 or not parts[0] or parts[0] == 'N/A':
            continue
        if 'K' in parts[-1].upper():
            try:
                times.append(float(parts[0]))
            except ValueError:
                continue
    if times:
        return sorted(set(times))
    p = run([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-skip_frame', 'nokey', '-show_entries', 'frame=pts_time', '-of', 'csv=p=0', src])
    for line in (p.stdout or '').splitlines():
        try:
            times.append(float(line.split(',')[0].strip()))
        except ValueError:
            continue
    return sorted(set(times))

def snap_to_keyframes(keeps, kfs: list[float]):
    out = []
    for s, e in keeps:
        i = bisect.bisect_right(kfs, s + 0.001) - 1
        start = kfs[i] if i >= 0 else 0.0
        nxt = kfs[i + 1] if i + 1 < len(kfs) else start + 1.0
        delta = max(0.02, min(0.05, (nxt - start) / 2))
        seek = start + delta
        if out and start < out[-1][1] - 0.001:
            out[-1] = (out[-1][0], max(out[-1][1], e), out[-1][2])
        else:
            out.append((start, e, seek))
    return out

def build_chunks(keeps, piece: float, gap_split: float, span_limit: float):
    pieces: list[tuple[float, float]] = []
    for s, e in keeps:
        if piece > 0 and e - s > piece * 1.5:
            t = s
            while e - t > piece * 1.5:
                pieces.append((t, t + piece))
                t += piece
            pieces.append((t, e))
        else:
            pieces.append((s, e))
    groups = [[pieces[0]]]
    for prev, cur in zip(pieces, pieces[1:]):
        gap = cur[0] - prev[1]
        span = cur[1] - groups[-1][0][0]
        if gap > gap_split or span > span_limit:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return groups

def write_filter_script(group, n_audio: int, base: float, path: str) -> None:
    parts = []
    for i, (s, e) in enumerate(group):
        a, b = (s - base, e - base)
        parts.append(f'[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]')
        for k in range(n_audio):
            parts.append(f'[0:a:{k}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}_{k}]')
    labels = ''.join((f'[v{i}]' + ''.join((f'[a{i}_{k}]' for k in range(n_audio))) for i in range(len(group))))
    a_outs = ''.join((f'[aout{k}]' for k in range(n_audio)))
    if n_audio:
        parts.append(f'{labels}concat=n={len(group)}:v=1:a={n_audio}[vout]{a_outs}')
    else:
        parts.append(f'{labels}concat=n={len(group)}:v=1:a=0[vout]')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(';\n'.join(parts))

def apply_speed(args, speed: str | None=None) -> str:
    speed = speed or getattr(args, 'speed', None) or SPEED
    if speed not in SPEED_PRESETS:
        speed = 'turbo'
    for key, value in SPEED_PRESETS[speed].items():
        setattr(args, key, value)
    args.speed = speed
    return speed

def chunk_cmd(ffmpeg: str, src: str, group, out_path: str, args, n_audio: int, hwaccel: str | None, threads: int, fps_flags: list[str], script_path: str) -> list[str]:
    timed=getattr(args,'timed_banners',None) or []
    blur_vf=getattr(args,'blur_vf',None)
    if timed:
        if len(group)!=1:raise banner_blur.BannerError('Нет точной монтажной карты для размытия баннеров')
        w,h=args.banner_output_size
        dynamic=banner_blur.filter_for_chunk(banner_blur.timed_regions(timed,group[0][0],group[0][1],w,h))
        if dynamic:blur_vf=(blur_vf+',' if blur_vf else '')+dynamic
    head = [ffmpeg, '-hide_banner', '-v', 'warning']
    if hwaccel:
        head += ['-hwaccel', hwaccel]
    head += ['-threads', str(threads)]
    if len(group) == 1:
        s, e = group[0]
        cmd = head + ['-ss', f'{s:.3f}', '-i', src, '-t', f'{max(e - s, 0.01):.3f}', '-map', '0:v:0']
        for a in range(n_audio):
            cmd += ['-map', f'0:a:{a}']
    else:
        base = group[0][0]
        write_filter_script(group, n_audio, base, script_path)
        vlabel = '[vout]'
        if blur_vf:
            with open(script_path, 'a', encoding='utf-8') as fh:
                fh.write(f';\n[vout]{blur_vf}[vfinal]')
            vlabel = '[vfinal]'
        cmd = head + ['-ss', f'{base:.3f}', '-t', f'{group[-1][1]-base:.3f}', '-i', src, '-filter_complex_script', script_path, '-map', vlabel]
        for a in range(n_audio):
            cmd += ['-map', f'[aout{a}]']
    if blur_vf and len(group) == 1:
        # -filter_script was removed in newer FFmpeg. Our manual streamer
        # profiles have at most 16 fixed areas, so this graph stays short
        # enough for a Windows command line and works on old and new FFmpeg.
        cmd += ['-vf', blur_vf]
    cmd += ['-c:v', *encode_args(args), '-pix_fmt', 'yuv420p']
    if fps_flags:
        cmd += fps_flags
    cmd += ['-c:a', args.acodec, '-b:a', args.audio_bitrate, '-threads', str(threads), '-max_muxing_queue_size', '4096', '-progress', 'pipe:1', '-nostats', '-y', out_path]
    return cmd

def copy_cmd(ffmpeg: str, src: str, item, out_path: str, n_audio: int) -> list[str]:
    s, e = (item[0], item[1])
    seek = item[2] if len(item) > 2 else s
    cmd = [ffmpeg, '-hide_banner', '-v', 'error', '-ss', f'{seek:.3f}', '-i', src, '-t', f'{max(e - seek, 0.01):.3f}', '-map', '0:v:0']
    for a in range(n_audio):
        cmd += ['-map', f'0:a:{a}']
    cmd += ['-c', 'copy', '-avoid_negative_ts', 'make_zero', '-progress', 'pipe:1', '-nostats', '-y', out_path]
    return cmd

class Progress:

    def __init__(self, total_sec: float, n: int):
        self.lock = threading.Lock()
        self.ms: dict[int, float] = {}
        self.done = 0
        self.total = max(total_sec, 1.0)
        self.n = n
        self.t0 = time.time()

    def set(self, i: int, sec: float) -> None:
        with self.lock:
            self.ms[i] = max(0.0, sec)

    def complete(self, i: int) -> None:
        with self.lock:
            self.done += 1

    def snapshot(self) -> tuple[float, int]:
        with self.lock:
            return (sum(self.ms.values()), self.done)

def _exec(cmd: list[str], idx: int, prog: Progress, log_path: str) -> int:
    with open(log_path, 'w', encoding='utf-8', errors='replace') as log:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8', errors='replace', creationflags=_proc_flags())
        with p.stdout:
            for line in p.stdout:
                if line.startswith('out_time_ms='):
                    try:
                        prog.set(idx, int(line.split('=', 1)[1]) / 1000000.0)
                    except ValueError:
                        pass
        return p.wait()

def item_duration(item) -> float:
    if item and isinstance(item[0], (int, float)):
        return float(item[1] - item[0])
    return sum((e - s for s, e in item))

def run_jobs(ffmpeg: str, jobs: list, cmd_builder, prog: Progress, tmpdir: str, hwaccel: str | None, jobs_n: int, progress_callback=None) -> list[str]:
    results: dict[int, str] = {}
    failures: list[str] = []

    def worker(idx: int, item):
        out = os.path.join(tmpdir, f'part{idx:05d}.mkv')
        script = os.path.join(tmpdir, f'filter{idx:05d}.txt')
        cmd = cmd_builder(item, out, script, hwaccel)
        rc = _exec(cmd, idx, prog, os.path.join(tmpdir, f'log{idx:05d}.txt'))
        if rc != 0 and hwaccel:
            rc = _exec(cmd_builder(item, out, script, None), idx, prog, os.path.join(tmpdir, f'log{idx:05d}.txt'))
        return (idx, out, rc)
    stop = threading.Event()

    def printer():
        while not stop.wait(10):
            done_s, done_n = prog.snapshot()
            pct = min(100.0, done_s / prog.total * 100.0)
            el = time.time() - prog.t0
            eta = el / pct * (100 - pct) if pct > 1 else 0
            print(f'\r  прогресс: {pct:5.1f}%  кусков {done_n}/{prog.n}  прошло {hhmmss(el)}  осталось ~{hhmmss(eta)}   ', end='', flush=True)
            if progress_callback:
                try: progress_callback(f'Рендер: {pct:.1f}% · частей готово {done_n}/{prog.n} · прошло {hhmmss(el)}')
                except Exception: pass  # Progress reporting must not stop encoding.
    t = threading.Thread(target=printer, daemon=True)
    t.start()
    with ThreadPoolExecutor(max_workers=jobs_n) as ex:
        futs = [ex.submit(worker, i, item) for i, item in enumerate(jobs)]
        for fut in as_completed(futs):
            idx, out, rc = fut.result()
            prog.complete(idx)
            if rc != 0:
                failures.append(os.path.join(tmpdir, f'log{idx:05d}.txt'))
            else:
                results[idx] = out
    stop.set()
    t.join(timeout=1)
    print('\r' + ' ' * 90 + '\r', end='', flush=True)
    if failures:
        tail = ''
        try:
            with open(failures[0], 'r', encoding='utf-8', errors='replace') as fh:
                tail = '\n'.join(fh.read().splitlines()[-15:])
        except OSError:
            pass
        raise CutterError(f'{len(failures)} заданий упали. Лог:\n{tail}')
    return [results[i] for i in range(len(jobs))]

def concat_parts(ffmpeg: str, parts: list[str], out: str) -> None:
    lst = os.path.join(os.path.dirname(parts[0]), 'final.txt')
    with open(lst, 'w', encoding='utf-8') as fh:
        for f in parts:
            fh.write(f"file '{f}'\n")
    cmd = [ffmpeg, '-hide_banner', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', lst, '-map', '0', '-c', 'copy', *final_mux_args(out), '-y', out]
    if run(cmd).returncode != 0:
        raise CutterError('финальная склейка не удалась')

def measured_timeline(groups, part_info, output_info):
    """Map single-range encoded parts using actual concat container durations.

    Concat demuxer advances by each file's container duration, not the ideal
    range length. Initial video timestamps include encoder/muxer delay.
    Reject unusual per-part drift instead of silently claiming exact timing.
    """
    if not groups or len(groups)!=len(part_info) or any(len(g)!=1 for g in groups):
        raise InvalidRenderTimeline('Не удалось построить измеренную монтажную карту')
    out_video=video_stream(output_info)
    if not out_video: raise InvalidRenderTimeline('В итоговом файле нет видеодорожки')
    try:
        rate=float(Fraction(fps_of(output_info) or '25'))
        if not math.isfinite(rate) or rate<=0: raise ValueError()
    except (ValueError,ZeroDivisionError): rate=25.
    tolerance=max(.15,2./rate+.05)
    first=part_info[0]; first_video=video_stream(first)
    if not first_video: raise InvalidRenderTimeline('В части нарезки нет видеодорожки')
    global_shift=ffloat(out_video.get('start_time'))-(ffloat(first_video.get('start_time'))-ffloat(first.get('format',{}).get('start_time')))
    cursor=0.; timeline=[]
    for index,(group,info) in enumerate(zip(groups,part_info),1):
        a,b=map(float,group[0]); duration=duration_of(info); v=video_stream(info)
        if not v or not math.isfinite(duration) or duration<=0:
            raise InvalidRenderTimeline(f'Не удалось измерить часть {index}')
        delta=duration-(b-a)
        if abs(delta)>tolerance:
            raise InvalidRenderTimeline(f'Часть {index}: длительность {duration:.3f} с, ожидалось {b-a:.3f} с; расхождение слишком велико для точных таймкодов')
        out_start=cursor+ffloat(v.get('start_time'))-ffloat(info.get('format',{}).get('start_time'))+global_shift
        if out_start < -.05: raise InvalidRenderTimeline('Некорректное начало монтажной карты')
        timeline.append((a,b,max(0.,out_start)));cursor+=duration
    actual=duration_of(output_info)
    if not math.isfinite(actual) or actual<=0 or abs(actual-cursor)>max(.3,tolerance):
        raise InvalidRenderTimeline(f'После склейки длительность {actual:.3f} с, сумма частей {cursor:.3f} с; таймкоды не подтверждены')
    return timeline

PROBE_WORKERS = 8

def probe_parts(parts: list[str], ffprobe: str, progress_callback=None) -> list[dict]:
    """ffprobe of every rendered part in parallel (ported from v2, R1). Order of results = order of parts;
    progress messages and the first error are the same as in the old one-by-one loop."""
    out: list[dict] = []
    if not parts:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(PROBE_WORKERS, len(parts)))) as ex:
        for index, info in enumerate(ex.map(lambda p: probe(p, ffprobe), parts), 1):
            out.append(info)
            if progress_callback and (index % 20 == 0 or index == len(parts)):
                progress_callback(f'Проверено частей: {index}/{len(parts)}')
    return out

def render_file(res: dict, args, ffmpeg: str, ffprobe: str, progress_callback=None) -> str:
    src, keep = (res['src'], res['keep'])
    out = args.out or os.path.splitext(src)[0] + '_cut' + os.path.splitext(src)[1]
    n_audio = len(res['streams'])
    cores = (_low_impact.cpu_budget() if _low_impact is not None else os.cpu_count()) or 4
    nvenc_jobs = int(getattr(args, 'nvenc_jobs', 2) or 2)
    jobs_n = args.jobs if args.jobs > 0 else nvenc_jobs if args.vcodec.endswith('_nvenc') else max(2, min(int(getattr(args, 'x264_max_jobs', 6) or 6), cores // int(getattr(args, 'x264_div', 4) or 4)))
    threads = max(1, cores // jobs_n)
    width, height = video_size(res['info'])
    scale_h = max(0, int(getattr(args, 'scale_height', 0) or 0))
    scale_vf = None
    if scale_h and height and (height > scale_h):
        out_w = width * scale_h // height // 2 * 2
        scale_vf = f'scale=-2:{scale_h}'
        print(f'масштаб     : {width}x{height} -> {out_w}x{scale_h}')
    else:
        out_w, scale_h = (width, height or scale_h)
    regions = clamp_regions(list(getattr(args, 'blur_regions', []) or []), out_w, scale_h)
    blur_vf = blur_filter(regions, getattr(args, 'blur_strength', BLUR_STRENGTH))
    if scale_vf and blur_vf:
        args.blur_vf = f'{scale_vf},{blur_vf}'
    else:
        args.blur_vf = scale_vf or blur_vf
    args.banner_output_size=(out_w,scale_h)
    if getattr(args,'timed_banners',None) and args.fast:
        raise banner_blur.BannerError('Для блюра требуется точный рендер')
    print(f'\nрендер      : {out}')
    enc_preset = args.nvenc_preset if args.vcodec.endswith('_nvenc') else args.preset
    print(f"энкодер     : {('copy (без перекодирования)' if args.fast else args.vcodec)}" + ('' if args.fast else f"  (скорость {getattr(args, 'speed', '-')}, пресет {enc_preset})"))
    if regions:
        print(f"блюр        : {len(regions)} обл., сила {getattr(args, 'blur_strength', BLUR_STRENGTH)}, кадр {width}x{height}")
        for x, y, w, h in regions:
            print(f'              x={x} y={y} {w}x{h}')
        if args.fast:
            print('              ВНИМАНИЕ: в быстром режиме (copy) блюр не применяется - нужен точный режим с перекодированием')
    elif getattr(args,'timed_banners',None):
        print(f"блюр        : автоматический, найдено областей на кадрах {len(args.timed_banners)}")
    else:
        print('блюр        : нет (области не заданы)')
    if scale_vf and args.fast:
        print('              ВНИМАНИЕ: в быстром режиме (copy) уменьшение кадра не применяется — нужен точный режим, иначе выход останется исходного размера')
    if args.fast:
        print('читаю ключевые кадры ...', flush=True)
        kfs = keyframes(ffprobe, src)
        if not kfs:
            print('ключевых кадров нет - переключаюсь на перекодирование')
            args.fast = False
        else:
            keep = snap_to_keyframes(keep, kfs)
    hwaccel = None
    fps_flags: list[str] = []
    if not args.fast:
        hwaccel = getattr(args, 'hwaccel_flag', None)
        fps_src = args.fps or fps_of(res['info'])
        fps_flags = fps_mode_flag(ffmpeg)
        if fps_src:
            fps_flags = ['-r', str(fps_src)] + fps_flags
    with tempfile.TemporaryDirectory() as tmpdir:
        if args.fast:
            jobs = list(keep)
            total = sum((e - s for s, e, *_ in jobs))
            prog = Progress(total, len(jobs))

            def builder(item, out_path, script, hw):
                return copy_cmd(ffmpeg, src, item, out_path, n_audio)
            print(f'заданий     : {len(jobs)}  процессов: {min(jobs_n, len(jobs))}  (копирование)')
            parts = run_jobs(ffmpeg, jobs, builder, prog, tmpdir, None, jobs_n, progress_callback)
        else:
            groups = build_chunks(keep, args.piece, -1 if getattr(args,'map_timeline',False) else args.gap_split, args.span_limit)
            jobs_n = max(1, min(jobs_n, len(groups)))
            total = sum((e - s for g in groups for s, e in g))
            prog = Progress(total, len(groups))

            def builder(item, out_path, script, hw):
                return chunk_cmd(ffmpeg, src, item, out_path, args, n_audio, hw, threads, fps_flags, script)
            dec = hwaccel or 'CPU'
            print(f'заданий     : {len(groups)}  процессов: {jobs_n}  потоков на процесс: {threads}  декод: {dec}')
            if fps_flags:
                print(f"fps         : {' '.join(fps_flags)}")
            print('старт ...', flush=True)
            parts = run_jobs(ffmpeg, groups, builder, prog, tmpdir, hwaccel, jobs_n, progress_callback)
        print('склейка ...', flush=True)
        if progress_callback: progress_callback('Склейка частей')
        concat_parts(ffmpeg, parts, out)
        if getattr(args,'map_timeline',False):
            if args.fast: raise InvalidRenderTimeline('Измеренная монтажная карта недоступна в режиме копирования')
            if progress_callback: progress_callback('Проверка длительности частей и таймкодов')
            part_info=probe_parts(parts,ffprobe,progress_callback)
            output_info=probe(out,ffprobe)
            res['render_timeline']=measured_timeline(groups,part_info,output_info)
    print(f'готово      : {out}')
    print(f'              длительность {fmt(duration_of(probe(out, ffprobe)))}')
    return out

def parse_args(argv=None):
    p = argparse.ArgumentParser(description='Вырезает тишину из записи стрима, сохраняя всё, где есть голос стримера. Быстрая версия: параллельный рендер.', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('file', nargs='?', help='исходное видео (или VIDEO сверху файла)')
    p.add_argument('-o', '--out', help='выходной файл (по умолчанию <name>_cut.<ext>)')
    g = p.add_argument_group('детекция')
    g.add_argument('--audio-track', default='auto', help="номер микрофонной дорожки или 'mix'")
    g.add_argument('--min-cut', type=float, default=2.0, help='минимальная длина вырезаемой паузы, сек')
    g.add_argument('--max-gap', type=float, default=2.0, help='паузы между фразами короче этого остаются в видео')
    g.add_argument('--pad-before', type=float, default=0.35)
    g.add_argument('--pad-after', type=float, default=0.45)
    g.add_argument('--min-speech', type=float, default=0.15, help='игнорировать звуки короче этого (щелчки, стуки)')
    g.add_argument('--noise-db', type=float, default=None, help='шумовой пол микрофона dBFS (по умолчанию измеряется автоматически; перекрывает автозамер)')
    g.add_argument('--silence-db', type=float, default=None, help='АБСОЛЮТНЫЙ порог тишины, dBFS: всё, что тише этого, считается тишиной. Перекрывает и шум, и --onset-db. Например -45 (типично: -50..-40)')
    g.add_argument('--sweep', action='store_true', help="подобрать пороги: таблица 'сколько %% вырежется' для разных dB и --min-cut (анализ без рендера)")
    g.add_argument('--onset-db', type=float, default=8.0, help='насколько выше шума должен быть звук, чтобы считать голосом')
    g.add_argument('--offset-db', type=float, default=4.0, help='гистерезис')
    g.add_argument('--conservative', action='store_true', help='резать меньше: вырез от 4 с, пауза между фразами до 5 с')
    g = p.add_argument_group('скорость')
    g.add_argument('--jobs', type=int, default=JOBS, help='сколько процессов ffmpeg одновременно (0 = авто)')
    g.add_argument('--hwaccel', default=HWACCEL, choices=['auto', 'none', 'cuda', 'd3d11va', 'dxva2', 'vaapi'], help='аппаратный декод')
    g.add_argument('--piece', type=float, default=900.0, help='длинные фрагменты режутся на куски по столько секунд (0 = не резать)')
    g.add_argument('--gap-split', type=float, default=1.5, help='пауза больше этого между сохранёнными кусками = отдельное задание (пауза тогда не декодируется)')
    g.add_argument('--span-limit', type=float, default=900.0, help='максимальный охват одного задания по источнику, сек')
    g = p.add_argument_group('вывод')
    g.add_argument('--precise', action='store_true', help='точная склейка с перекодированием')
    g.add_argument('--fast', action='store_true', help='мгновенная склейка без перекодирования')
    g.add_argument('--speed', choices=list(SPEED_PRESETS), default=None, help='скорость рендера: quality | fast | max (задаёт --preset/--nvenc-preset)')
    g.add_argument('--vcodec', default='auto', help='видеокодек: auto = NVIDIA если есть, иначе x264')
    g.add_argument('--acodec', default='aac')
    g.add_argument('--crf', type=float, default=18)
    g.add_argument('--preset', default='veryfast')
    g.add_argument('--nvenc-preset', default='p5')
    g.add_argument('--cq', type=float, default=20)
    g.add_argument('--audio-bitrate', default='192k')
    g.add_argument('--fps', default=None, help='принудительный fps выхода (по умолчанию fps источника, CFR)')
    g.add_argument('--maxrate', default=None, metavar='12M', help='потолок битрейта видео (например 12M). Режет пики, из-за которых плеер захлёбывается на 1440p')
    g.add_argument('--scale-height', type=int, default=0, metavar='H', help='ужать выход до этой высоты (1080), 0 = как исходник')
    g.add_argument('--tune', default=None, metavar='fastdecode', help='x264 -tune: fastdecode — видео легче декодируется на слабом железе')
    g = p.add_argument_group('блюр областей')
    g.add_argument('--blur-region', action='append', default=None, metavar='X,Y,W,H', help='заблюрить область (можно указать несколько раз). X,Y - левый верхний угол, W,H - размер, например 1728,0,187,335')
    g.add_argument('--blur-strength', type=int, default=BLUR_STRENGTH, help='сила блюра: радиус boxblur, 1..40')
    p.add_argument('--list-audio', action='store_true', help='показать дорожки и выйти')
    p.add_argument('--dry-run', action='store_true', help='только анализ, вывести список вырезов, не рендерить')
    p.add_argument('--timeline', help='путь для JSON со списком вырезов')
    args = p.parse_args(argv)
    if args.blur_region:
        regions = [parse_region(s) for s in args.blur_region]
        args.blur_regions = [r for r in regions if r]
        bad = len(regions) - len(args.blur_regions)
        if bad:
            print(f'предупреждение: {bad} областей блюра не разобраны (нужен формат X,Y,W,H)')
    else:
        args.blur_regions = list(BLUR_REGIONS)
    return args

def analyse(src: str, args, ffmpeg: str, ffprobe: str, start: float=0, end: float | None=None) -> dict:
    info = probe(src, ffprobe)
    streams = audio_streams(info)
    if not streams:
        raise CutterError('нет аудиодорожек - нечего анализировать')
    mic = pick_mic_stream(info, args.audio_track)
    track = mic if mic >= 0 else 0
    source_total = duration_of(info)
    if source_total <= 0:
        raise CutterError('не удалось определить длительность')
    start = float(start)
    end = source_total if end is None else min(float(end), source_total)
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise CutterError('некорректный интервал анализа')
    total = end-start
    print(f'источник    : {src}')
    print(f'длительность: {fmt(total)}')
    print(f'аудиодорожки: {len(streams)}')
    for line in describe_audio(info):
        print(line)
    print(f'микрофон    : 0:a:{track}' + ('  (общий микс - стримера от звука видео не отделить)' if mic < 0 else ''))
    t0 = time.time()
    print('декодирую аудио ...', flush=True)
    buf = decode_pcm(src, track, ffmpeg, start, end)
    print('анализирую ...', flush=True)
    levels, backend = frame_levels(buf)
    del buf
    print(f'бэкенд      : {backend}  ({time.time() - t0:.1f} с, {len(levels)} кадров)')
    sample = list(levels[::5]) or list(levels)
    floor = percentile(sample, 0.1)
    floor = max(-70.0, min(-35.0, floor))
    if args.noise_db is not None:
        floor = args.noise_db
    on_db, off_db = voice_thresholds(floor, args)
    if args.sweep:
        sweep(levels, floor, args, total)
    speech = detect_speech(levels, on_db, off_db, args.min_speech)
    keep, cuts = build_plan(speech, total, args)
    kept = sum((e - s for s, e in keep))
    removed = sum((e - s for s, e in cuts))
    print(f'шум (пол)   : {floor:.1f} dBFS')
    print(f'порог голоса: >= {on_db:.1f} dBFS - речь, <= {off_db:.1f} dBFS - тишина' + ('   [абсолютный, --silence-db]' if args.silence_db is not None else f'   [пол + {args.onset_db:.0f} dB, --onset-db]'))
    if on_db <= floor + 1.0:
        print('предупреждение: порог голоса на уровне шума или ниже - почти всё считается речью, вырежется мало. Подними --onset-db или --silence-db (в сторону 0)')
    voice_time = sum((e - s for s, e in speech))
    print(f'голос       : {len(speech)} отрезков, {voice_time:.1f} с')
    if voice_time / total > 0.9:
        print('предупреждение: голос найден в >90% стрима - либо порог слишком низкий (--onset-db 12), либо это не микрофон (--list-audio)')
    print(f'вырезов     : {len(cuts)}')
    print(f'остаётся    : {fmt(kept)}')
    print(f'вырезается  : {fmt(removed)} ({removed / total * 100:.1f}% стрима)' if total else '')
    if cuts:
        print('\nсписок вырезов (начало -> конец, длина):')
        for s, e in cuts[:40]:
            print(f'  {fmt(s)} -> {fmt(e)}   {e - s:7.2f} с')
        if len(cuts) > 40:
            print(f'  ... и ещё {len(cuts) - 40}')
    # The plan was computed relative to the selected range; renderer needs
    # coordinates in the original source, including on nonzero Twitch offsets.
    keep = [(a+start,b+start) for a,b in keep]
    cuts = [(a+start,b+start) for a,b in cuts]
    timeline = {'source': src, 'duration': source_total, 'analysis_start': start, 'analysis_end': end, 'mic_track': track, 'mixed_audio': mic < 0, 'noise_floor_db': round(floor, 2), 'voice_threshold_db': round(on_db, 2), 'silence_threshold_db': round(off_db, 2), 'params': {'min_cut': args.min_cut, 'max_gap': args.max_gap, 'pad_before': args.pad_before, 'pad_after': args.pad_after, 'min_speech': args.min_speech}, 'cuts': [{'start': round(s, 3), 'end': round(e, 3), 'dur': round(e - s, 3)} for s, e in cuts], 'keeps': [{'start': round(s, 3), 'end': round(e, 3), 'dur': round(e - s, 3)} for s, e in keep]}
    tl_path = args.timeline or os.path.splitext(src)[0] + '_cuts.json'
    with open(tl_path, 'w', encoding='utf-8') as fh:
        json.dump(timeline, fh, indent=2)
    print(f'таймлайн    : {tl_path}')
    return {'src': src, 'info': info, 'streams': streams, 'total': source_total, 'track': track, 'mic': mic, 'floor': floor, 'keep': keep, 'cuts': cuts, 'kept': kept, 'removed': removed, 'timeline': tl_path, 'levels': levels, 'on_db': on_db, 'off_db': off_db}

def main(argv=None) -> int:
    args = parse_args(argv)
    if args.conservative:
        args.min_cut = max(args.min_cut, 4.0)
        args.max_gap = max(args.max_gap, 5.0)
    args.fast = (FAST or args.fast) and (not args.precise)
    if args.speed or SPEED:
        apply_speed(args)
    ffmpeg, ffprobe = require_tools()
    src = os.path.abspath(args.file or VIDEO)
    if args.list_audio:
        print(src)
        print('\n'.join(describe_audio(probe(src, ffprobe))) or '  (нет аудио)')
        return 0
    if not os.path.isfile(src):
        raise CutterError(f'файл не найден: {src}\n  -> укажите реальный путь в переменной VIDEO сверху файла')
    if not args.fast:
        args.vcodec = pick_encoder(ffmpeg, args.vcodec)
    res = analyse(src, args, ffmpeg, ffprobe)
    if res['kept'] < 0.5:
        raise CutterError('получилось, что режется весь стрим - детекция неверна. Проверьте дорожку микрофона (--list-audio) или порог (--onset-db)')
    if not args.fast and args.hwaccel != 'none':
        hw = detect_hwaccel(ffmpeg, src, args.hwaccel, args.vcodec)
        if hw:
            print(f'декод       : {hw} (аппаратный)')
        args.hwaccel_flag = hw
    else:
        args.hwaccel_flag = None
    if args.dry_run or not res['cuts']:
        if not res['cuts']:
            print('резать нечего - файл не тронут')
        return 0
    render_file(res, args, ffmpeg, ffprobe)
    return 0
if __name__ == '__main__':
    try:
        sys.exit(main())
    except CutterError as exc:
        print(f'ошибка: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nпрервано', file=sys.stderr)
        sys.exit(130)
