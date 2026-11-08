"""Fast local banner search by user samples (OpenCV template matching).

No neural network, GPU memory or model download is used. Each advertising
banner is added once as a sample image in ``banners/``; afterwards every new
clip is scanned automatically. Only video key frames are decoded for the main
pass (about one frame every 2 seconds on Twitch), so a 30-minute clip is
analysed in seconds; banner boundaries are then refined with a few extra frames.

A missing sample, an unreadable image or a decoding error is never reported as
"no banners": such conditions stop the job with a clear message.
"""
from __future__ import annotations
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / 'banners'
INTERVAL = 2.0
ANALYSIS_WIDTH = 640
THRESHOLD = 0.80          # grayscale normalized correlation, 1.0 = identical
COLOR_THRESHOLD = 0.70    # second, colour check of the same place
MAX_MEAN_COLOR_DIFF = 48  # 0..255 per channel; rejects same shape in other colours
SCALES = (0.88, 0.94, 1.0, 1.06, 1.13)
WIDE_SCALES = tuple(round(0.5 * 1.1 ** i, 3) for i in range(12))  # samples without frame size
DEFAULT_FRAME_WIDTH = 1920
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.bmp', '.webp')
MAX_GAP_SAMPLES = 2       # an animated banner may not match on 1-2 sampled frames
MAX_GAP_SECONDS = 6.5
PAD = 0.10                # blur this much around the sample on every side
MARGIN = 0.15             # seconds added before/after a refined appearance
REFINE_STEPS = 3
MIN_SAMPLE = (16, 12)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
NO_TEMPLATES = ('Нет образцов баннеров. Откройте «Настройки → Обработка» и нажмите «Добавить образец из видео» '
                '(или выключите блюр баннеров), затем повторите задание')


class BannerError(RuntimeError): pass
class BannerSetupError(BannerError): pass
class BannerResponseError(BannerError): pass   # kept for compatibility with older imports


def _cv():
    try:
        import cv2
        import numpy
    except ImportError:
        raise BannerSetupError('Для блюра баннеров не установлен OpenCV. Закройте приложение и запустите START.bat '
                               '(он доустановит пакет) или INSTALL.bat') from None
    return cv2, numpy


# ---------------------------------------------------------------------------
# sample files
# ---------------------------------------------------------------------------
def read_image(path):
    """BGR image or None. Works with non-ASCII Windows paths (cv2.imread does not)."""
    cv2, np = _cv()
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if not data.size:
        return None
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return image if image is not None and image.size else None


def write_png(path, image):
    cv2, np = _cv()
    ok, buf = cv2.imencode('.png', image)
    if not ok:
        raise BannerError('Не удалось сохранить изображение')
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    try:
        buf.tofile(str(tmp))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def safe_stem(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name or '')).strip(' ._')
    if not name or name.upper() in {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'LPT1'}:
        name = 'баннер'
    return name[:60]


def texture_problem(image):
    """Reason why a sample is unusable, or None."""
    cv2, np = _cv()
    h, w = image.shape[:2]
    if w < MIN_SAMPLE[0] or h < MIN_SAMPLE[1]:
        return f'область слишком маленькая (минимум {MIN_SAMPLE[0]}×{MIN_SAMPLE[1]} пикселей)'
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < 12:
        return 'область почти однотонная — выделите часть баннера с надписью или логотипом'
    return None


def list_samples(folder=None):
    folder = Path(folder or TEMPLATE_DIR)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                   and not p.name.startswith(('.', '_'))), key=lambda p: p.name.casefold())


def template_count(folder=None):
    return len(list_samples(folder))


def save_template(image, box, name='', folder=None, source='', time_s=None):
    """Save box=(x,y,w,h) of a full video frame as a sample plus its frame size."""
    h0, w0 = image.shape[:2]
    x, y, w, h = (int(round(float(v))) for v in box)
    x = max(0, min(x, w0 - 1)); y = max(0, min(y, h0 - 1))
    w = max(0, min(w, w0 - x)); h = max(0, min(h, h0 - y))
    if w * h > 0.4 * w0 * h0:
        raise ValueError('Область занимает больше 40% кадра; выделите только баннер')
    crop = image[y:y + h, x:x + w].copy()
    problem = texture_problem(crop) if w and h else 'область не выделена'
    if problem:
        raise ValueError('Образец не подходит: ' + problem)
    folder = Path(folder or TEMPLATE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(name); candidate = stem; n = 2
    while any((folder / (candidate + s)).exists() for s in IMAGE_SUFFIXES + ('.json',)):
        candidate = f'{stem}_{n}'; n += 1
    meta = {'version': 1, 'frame_width': w0, 'frame_height': h0, 'x': x, 'y': y, 'w': w, 'h': h,
            'source': Path(source).name if source else '', 'time': None if time_s is None else round(float(time_s), 3)}
    write_png(folder / (candidate + '.png'), crop)
    tmp = folder / (candidate + '.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, folder / (candidate + '.json'))
    return folder / (candidate + '.png')


class Template:
    def __init__(self, name, image, frame_width=None):
        self.name = name
        self.image = image
        self.known = bool(frame_width)
        self.frame_width = int(frame_width or DEFAULT_FRAME_WIDTH)
        self._cache = {}

    def variants(self, width, height):
        key = (width, height)
        if key not in self._cache:
            cv2, np = _cv()
            h, w = self.image.shape[:2]
            base = width / self.frame_width
            out = []
            for s in (SCALES if self.known else WIDE_SCALES):
                tw, th = round(w * base * s), round(h * base * s)
                if tw < 10 or th < 8 or tw >= width or th >= height:
                    continue
                color = cv2.resize(self.image, (tw, th), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
                if float(gray.std()) < 6:
                    continue
                out.append((s, gray, color, color.reshape(-1, 3).mean(axis=0)))
            self._cache[key] = out
        return self._cache[key]


def load_templates(folder=None):
    result = []
    for path in list_samples(folder):
        image = read_image(path)
        if image is None:
            raise BannerSetupError(f'Образец баннера «{path.name}» не читается; удалите или замените его')
        frame_width = None
        meta_path = path.with_suffix('.json')
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
                value = meta.get('frame_width') if isinstance(meta, dict) else None
                if isinstance(value, int) and not isinstance(value, bool) and 64 <= value <= 16384:
                    frame_width = value
            except (OSError, ValueError):
                pass
        result.append(Template(path.stem, image, frame_width))
    return result


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
class Detector:
    def __init__(self, templates, width, height):
        self.cv2, self.np = _cv()
        self.width, self.height = width, height
        self.templates = list(templates)
        self.items = [t.variants(width, height) for t in self.templates]
        unusable = [t.name for t, v in zip(self.templates, self.items) if not v]
        if unusable:
            raise BannerSetupError('Образцы не подходят для этого видео (слишком маленькие или однотонные): '
                                   + ', '.join(unusable[:5]))

    def match(self, frame, only=None, near=None):
        """{template index: (score, (x, y, w, h))} in analysis-frame pixels."""
        cv2, np = self.cv2, self.np
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hits = {}
        for index, variants in enumerate(self.items):
            if only is not None and index != only:
                continue
            ox = oy = 0; area = gray
            if near is not None:
                x, y, w, h = near
                ox, oy = max(0, int(x - w * .5 - 4)), max(0, int(y - h * .5 - 4))
                area = gray[oy:min(self.height, int(y + h * 1.5 + 4)), ox:min(self.width, int(x + w * 1.5 + 4))]
            best = None
            for scale, tg, tc, mean in variants:
                if tg.shape[0] > area.shape[0] or tg.shape[1] > area.shape[1]:
                    continue
                res = cv2.matchTemplate(area, tg, cv2.TM_CCOEFF_NORMED)
                np.nan_to_num(res, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                _, score, _, loc = cv2.minMaxLoc(res)
                if best is None or score > best[0]:
                    best = (score, loc, tg, tc, mean)
            if best is None or best[0] < THRESHOLD:
                continue
            score, (x, y), tg, tc, mean = best
            x += ox; y += oy; h, w = tg.shape
            patch = frame[y:y + h, x:x + w]
            color = cv2.matchTemplate(patch, tc, cv2.TM_CCOEFF_NORMED)
            color_score = float(np.nan_to_num(color, nan=0.0, posinf=0.0, neginf=0.0)[0, 0])
            diff = float(np.abs(patch.reshape(-1, 3).mean(axis=0) - mean).max())
            if color_score >= COLOR_THRESHOLD and diff <= MAX_MEAN_COLOR_DIFF and score <= 1.0001:
                hits[index] = (min(1.0, float(score)), (x, y, w, h))
        return hits


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------
def _ffprobe_for(ffmpeg):
    path = Path(ffmpeg)
    sibling = path.with_name('ffprobe' + path.suffix)
    return str(sibling) if sibling.is_file() else (shutil.which('ffprobe') or 'ffprobe')


def video_info(src, ffmpeg):
    cmd = [_ffprobe_for(ffmpeg), '-v', 'error', '-select_streams', 'v:0', '-show_entries',
           'stream=width,height:format=duration', '-of', 'json', str(src)]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, creationflags=NO_WINDOW)
        data = json.loads(r.stdout or b'{}')
        stream = data['streams'][0]
        width, height = int(stream['width']), int(stream['height'])
        duration = float(data.get('format', {}).get('duration') or 0)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        raise BannerError('Не удалось прочитать размер видео для поиска баннеров') from None
    if width < 16 or height < 16:
        raise BannerError('Некорректный размер видео для поиска баннеров')
    return width, height, (duration if math.isfinite(duration) and duration > 0 else 0.0)


def analysis_size(width, height):
    w = min(ANALYSIS_WIDTH, width // 2 * 2)
    h = max(16, int(round(height * w / width / 2)) * 2)
    return w, h


_SHOWINFO = re.compile(r'\bn:\s*(\d+)\s.*?\bpts_time:\s*(-?[0-9.]+(?:e[-+]?\d+)?)')


def decode_frames(src, start, end, ffmpeg, size, keyframes=True, interval=INTERVAL):
    """Yield (source time, BGR frame) for key frames (fast) or every `interval` seconds."""
    cv2, np = _cv()
    width, height = size
    scale = f'scale={width}:{height}:flags=area,format=bgr24'
    vf = ('showinfo,' + scale) if keyframes else (f'fps=1/{interval:g}:start_time=0,showinfo,' + scale)
    cmd = [ffmpeg, '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'info']
    if keyframes:
        cmd += ['-skip_frame', 'nokey']
    cmd += ['-ss', f'{start:.3f}', '-t', f'{max(0.05, end - start):.3f}', '-i', str(src),
            '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', vf, '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    except OSError:
        raise BannerError('Не удалось запустить FFmpeg для поиска баннеров') from None
    times = queue.Queue(); tail = deque(maxlen=4)

    def read_log():
        try:
            for raw in proc.stderr:
                line = raw.decode('utf-8', 'replace')
                if 'showinfo' in line:
                    m = _SHOWINFO.search(line)
                    if m:
                        times.put(float(m.group(2)))
                elif line.strip():
                    tail.append(line.strip()[:200])
        finally:
            times.put(None)

    reader = threading.Thread(target=read_log, daemon=True); reader.start()
    frame_bytes = width * height * 3
    try:
        while True:
            data = proc.stdout.read(frame_bytes)
            if not data:
                break
            if len(data) != frame_bytes:
                raise BannerError('FFmpeg вернул неполный кадр при поиске баннеров')
            try:
                stamp = times.get(timeout=30)
            except queue.Empty:
                stamp = None
            if stamp is None:
                raise BannerError('FFmpeg не сообщил время кадра при поиске баннеров')
            yield start + stamp, np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
        proc.wait(timeout=60)
        reader.join(timeout=5)
        if proc.returncode != 0:
            raise BannerError('FFmpeg не смог прочитать кадры для поиска баннеров' + (': ' + tail[-1] if tail else ''))
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        for stream in (proc.stdout, proc.stderr):
            try: stream.close()
            except OSError: pass


def frame_at(src, t, ffmpeg, size):
    cv2, np = _cv()
    width, height = size
    cmd = [ffmpeg, '-hide_banner', '-v', 'error', '-ss', f'{max(0., t):.3f}', '-i', str(src), '-map', '0:v:0',
           '-frames:v', '1', '-vf', f'scale={width}:{height}:flags=area,format=bgr24', '-f', 'rawvideo',
           '-pix_fmt', 'bgr24', 'pipe:1']
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or len(r.stdout) < width * height * 3:
        return None
    return np.frombuffer(r.stdout[:width * height * 3], dtype=np.uint8).reshape(height, width, 3)


def full_frame_at(src, t, ffmpeg):
    """Full-resolution BGR frame (for the sample picker and previews)."""
    cv2, np = _cv()
    cmd = [ffmpeg, '-hide_banner', '-v', 'error', '-ss', f'{max(0., t):.3f}', '-i', str(src), '-map', '0:v:0',
           '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1']
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        raise BannerError('Не удалось извлечь кадр из видео') from None
    if r.returncode != 0 or not r.stdout:
        raise BannerError('Не удалось извлечь кадр: время за пределами видео или файл повреждён')
    image = cv2.imdecode(np.frombuffer(r.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise BannerError('Не удалось декодировать кадр из видео')
    return image


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------
def _iou(a, b):
    l = max(a[0], b[0]); t = max(a[1], b[1]); r = min(a[2], b[2]); bot = min(a[3], b[3])
    inter = max(0, r - l) * max(0, bot - t)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter) if inter else 0


def _corners(box):
    x, y, w, h = box
    return (x, y, x + w, y + h)


def _sample(src, start, end, ffmpeg, size, interval, detector, progress, label=''):
    expected = max(1, int((end - start) / interval))
    limit = max(interval * 1.5, interval + 1.0) + 0.1
    for keyframes in (True, False):
        samples = []; last = None; began = time.monotonic(); reported = began
        for t, frame in decode_frames(src, start, end, ffmpeg, size, keyframes, interval):
            if t < start - 0.5 or t > end + 0.5:
                continue
            if keyframes and last is not None and t - last < interval * 0.9:
                continue
            last = t
            samples.append((t, detector.match(frame)))
            now = time.monotonic()
            if progress and now - reported >= 10:
                reported = now
                progress(f'Поиск баннеров{label}: {int(t // 60):02}:{int(t % 60):02} из {int(end // 60):02}:{int(end % 60):02}')
        stamps = [start] + [t for t, _ in samples] + [end]
        gap = max(b - a for a, b in zip(stamps, stamps[1:]))
        if gap <= limit or not keyframes:
            if not samples:
                raise BannerError('Не удалось получить ни одного кадра для поиска баннеров')
            if gap > max(limit, interval * 3):
                raise BannerError(f'Пропуск {gap:.1f} с без кадров при поиске баннеров; видео повреждено?')
            return samples
        if progress:
            progress(f'Ключевые кадры редкие (до {gap:.1f} с); поиск баннеров по всем кадрам — это медленнее')
    return samples


def _tracks(samples, count):
    tracks = []
    for index in range(count):
        current = None
        for i, (t, hits) in enumerate(samples):
            hit = hits.get(index)
            if hit is None:
                continue
            score, box = hit
            c = _corners(box)
            if (current and i - current['last'] - 1 <= MAX_GAP_SAMPLES and t - samples[current['last']][0] <= MAX_GAP_SECONDS
                    and _iou(c, current['box']) >= 0.3):
                b = current['box']
                current['box'] = (min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3]))
                current['last'] = i; current['score'] = max(current['score'], score)
            else:
                current = {'index': index, 'first': i, 'last': i, 'box': c, 'score': score, 'ref': box}
                tracks.append(current)
    return tracks


def _refine(src, ffmpeg, size, detector, index, ref, absent, present):
    """Narrow the change between a time without the banner and one with it."""
    for _ in range(REFINE_STEPS):
        if abs(present - absent) <= 0.3:
            break
        mid = (absent + present) / 2
        frame = frame_at(src, mid, ffmpeg, size)
        if frame is None:
            break
        if index in detector.match(frame, only=index, near=ref):
            present = mid
        else:
            absent = mid
    return absent


def to_grid(box, width, height):
    x1, y1, x2, y2 = box
    px = max(2.0, (x2 - x1) * PAD); py = max(2.0, (y2 - y1) * PAD)
    return [max(0, math.floor((x1 - px) * 1000 / width)), max(0, math.floor((y1 - py) * 1000 / height)),
            min(1000, math.ceil((x2 + px) * 1000 / width)), min(1000, math.ceil((y2 + py) * 1000 / height))]


def scan(src, keeps, ffmpeg, progress=None, interval=INTERVAL, templates=None):
    """[{'start','end','box' (0–1000 grid),'name','score'}] in source-file seconds."""
    interval = float(interval)
    if not math.isfinite(interval) or interval <= 0:
        raise BannerError('Неверный интервал анализа')
    templates = load_templates() if templates is None else list(templates)
    if not templates:
        raise BannerSetupError(NO_TEMPLATES)
    width, height, _ = video_info(src, ffmpeg)
    size = analysis_size(width, height)
    detector = Detector(templates, *size)
    if progress:
        progress(f'Поиск баннеров по образцам ({len(templates)} шт.): ключевые кадры, шаг ~{interval:g} с')
    began = time.monotonic(); results = []; frames = 0
    for start, end in keeps:
        start, end = float(start), float(end)
        if end - start <= 0.01:
            continue
        samples = _sample(src, start, end, ffmpeg, size, interval, detector, progress)
        frames += len(samples)
        for track in _tracks(samples, len(templates)):
            index, ref = track['index'], track['ref']
            first, last = track['first'], track['last']
            begin = start if first == 0 else _refine(src, ffmpeg, size, detector, index, ref,
                                                     samples[first - 1][0], samples[first][0])
            finish = end if last == len(samples) - 1 else _refine(src, ffmpeg, size, detector, index, ref,
                                                                  samples[last + 1][0], samples[last][0])
            begin = max(start, begin - MARGIN); finish = min(end, finish + MARGIN)
            if finish - begin < 0.05:
                continue
            results.append({'start': round(begin, 3), 'end': round(finish, 3), 'box': to_grid(track['box'], *size),
                            'name': templates[index].name, 'score': round(track['score'], 3)})
    results.sort(key=lambda d: (d['start'], d['name']))
    if progress:
        progress(f'Поиск баннеров: проверено кадров {frames} за {time.monotonic() - began:.0f} с; найдено появлений {len(results)}')
    return results


def check_video(src, ffmpeg, out_dir, progress=None, interval=INTERVAL, templates=None):
    """Scan a whole video and save preview frames with the found areas (for manual checks)."""
    cv2, np = _cv()
    width, height, duration = video_info(src, ffmpeg)
    if duration <= 0:
        raise BannerError('Не удалось определить длительность видео')
    found = scan(src, [(0., duration)], ffmpeg, progress, interval, templates)
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f'Проверка образцов баннеров: {Path(src).name}', f'Найдено появлений: {len(found)}', '']
    for n, d in enumerate(found, 1):
        mid = (d['start'] + d['end']) / 2
        stamp = f"{int(mid // 3600):02}-{int(mid % 3600 // 60):02}-{int(mid % 60):02}"
        try:
            frame = full_frame_at(src, mid, ffmpeg)
            fh, fw = frame.shape[:2]
            x1, y1, x2, y2 = d['box']
            p1 = (int(x1 * fw / 1000), int(y1 * fh / 1000)); p2 = (int(x2 * fw / 1000), int(y2 * fh / 1000))
            cv2.rectangle(frame, p1, p2, (0, 0, 255), max(2, fw // 400))
            ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                buf.tofile(str(out_dir / f'{n:03} {stamp} {safe_stem(d["name"])}.jpg'))
        except BannerError:
            pass
        lines.append(f"{n:03}. {_hms(d['start'])} — {_hms(d['end'])}; образец «{d['name']}», совпадение {d['score']:.2f}")
    if not found:
        lines.append('Баннеры не найдены. Проверьте, что образец вырезан из такого же баннера и без фона игры.')
    (out_dir / 'отчёт.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    return found


def _hms(t):
    ms = int(round(max(0., t) * 1000))
    return f'{ms // 3600000:02}:{ms % 3600000 // 60000:02}:{ms % 60000 // 1000:02}.{ms % 1000:03}'


# ---------------------------------------------------------------------------
# render helpers (used by stream_cutter)
# ---------------------------------------------------------------------------
def timed_regions(detections, start, end, width, height):
    """Group similar rectangles, map to current render chunk (relative times)."""
    clusters = []
    for d in detections:
        a = max(start, d['start']); b = min(end, d['end'])
        if b <= a:
            continue
        box = d['box']; best = None
        for c in clusters:
            if _iou(c['box'], box) >= .7:
                union = [min(c['box'][0], box[0]), min(c['box'][1], box[1]), max(c['box'][2], box[2]), max(c['box'][3], box[3])]
                area = (union[2] - union[0]) * (union[3] - union[1]); orig = (box[2] - box[0]) * (box[3] - box[1])
                if area <= orig * 1.6:
                    best = c; break
        if best is None:
            best = {'box': list(box), 'times': []}; clusters.append(best)
        else:
            best['box'] = union
        best['times'].append((a - start, b - start))
    if len(clusters) > 64:
        raise BannerError('Слишком много разных областей баннеров для рендера; задача остановлена')
    result = []
    for c in clusters:
        x1, y1, x2, y2 = c['box']; x = math.floor(x1 * width / 1000); y = math.floor(y1 * height / 1000)
        x = x // 2 * 2; y = y // 2 * 2
        x2 = min(width, math.ceil(x2 * width / 1000)); y2 = min(height, math.ceil(y2 * height / 1000))
        w = (x2 - x) // 2 * 2; h = (y2 - y) // 2 * 2
        if w < 4 or h < 4:
            raise BannerError('Область баннера слишком мала для размытия')
        times = []
        for a, b in sorted(c['times']):
            if times and a <= times[-1][1] + .001:
                times[-1] = (times[-1][0], max(times[-1][1], b))
            else:
                times.append((a, b))
        result.append((x, y, w, h, times))
    return result


def filter_for_chunk(regions, strength=20):
    if not regions:
        return None
    # Each overlay is only active in its detected interval. Output timestamps in
    # these precise single-range chunks start at zero.
    parts = []; prev = '[bin]'
    for i, (x, y, w, h, times) in enumerate(regions):
        # Strong enough for large logos, bounded by the chroma plane size.
        radius = max(1, min(w // 4, h // 4, max(strength, min(w, h) // 6)))
        parts.append(f'{prev}split=2[b{i}][s{i}]')
        parts.append(f'[s{i}]crop={w}:{h}:{x}:{y},boxblur={radius}:3[r{i}]')
        enabled = '+'.join(f'between(t,{a:.3f},{b:.3f})' for a, b in times)
        out = f'[o{i}]' if i < len(regions) - 1 else '[bout]'
        parts.append(f"[b{i}][r{i}]overlay={x}:{y}:enable='{enabled}'{out}")
        prev = out
    # A filter chain uses anonymous in/out in -vf or after a filter graph label.
    return ';'.join(parts).replace('[bin]', '', 1).removesuffix('[bout]')
