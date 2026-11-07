#!/usr/bin/env python3
"""
transcribe.py — транскрибация видео/аудио в текст (речь -> .txt / .srt / .vtt / .json).

Отдельный самостоятельный скрипт, остальной проект не трогает.
Движок: faster-whisper (Whisper от OpenAI, но в 4 раза быстрее и экономнее по памяти).

Установка:
    pip install faster-whisper
    # для видеокарты NVIDIA (сильно быстрее) дополнительно:
    pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"

Примеры:
    python transcribe.py video.mp4
    python transcribe.py video.mp4 --model large-v3 --lang ru
    python transcribe.py video.mp4 --start 01:20:00 --end 01:35:00
    python transcribe.py a.mp4 b.mp4 --formats txt,srt,json --out texts
    python transcribe.py video.mp4 --device cpu --model small
    python transcribe.py video.mp4 --fast                   # слабый ПК: модель base
    python transcribe.py video.mp4 --engine groq --groq-key gsk_...   # облако, секунды

Скорость на слабом процессоре (4 ядра), 25 минут видео — примерно:
    small, beam 5 (как было)  ~70 мин
    small, beam 1 (сейчас)    ~45 мин
    base  (--fast)            ~15-20 мин
    tiny                      ~8-10 мин (много ошибок в словах)
    Groq (облако)             ~30 секунд, качество лучше всех (large-v3-turbo)
Ключ Groq бесплатный: https://console.groq.com/keys

Модели (скачиваются сами при первом запуске в кэш HuggingFace):
    tiny / base / small / medium / large-v3 / large-v3-turbo (turbo)
    По умолчанию: large-v3-turbo на видеокарте, small на процессоре.

Из кода:
    from transcribe import transcribe_file
    segs = transcribe_file("video.mp4", lang="ru")   # [{'start','end','text'}, ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


try:
    import bot_debug as _debug
except ImportError:
    _debug = None


def _stage(message):
    if _debug is not None:
        _debug.stage(message)


# ---------------------------------------------------------------------------
# CUDA на Windows: DLL из pip-пакетов nvidia-* надо явно добавить в поиск
# ---------------------------------------------------------------------------
_NVIDIA_DLL_HANDLES = []


def _add_nvidia_dlls() -> None:
    if os.name != "nt":
        return
    import site
    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    for root in roots:
        for d in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            try:
                _NVIDIA_DLL_HANDLES.append(os.add_dll_directory(d))
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


_add_nvidia_dlls()


class TranscribeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# время
# ---------------------------------------------------------------------------
def time_to_sec(s: str | None) -> float | None:
    """'1:02:03' / '62:03' / '3723' / '3723.5' -> секунды."""
    if s in (None, ""):
        return None
    parts = str(s).strip().replace(",", ".").split(":")
    try:
        val = 0.0
        for p in parts:
            val = val * 60 + float(p)
        return val
    except ValueError:
        raise TranscribeError(f"не понял время: {s!r} (нужно ЧЧ:ММ:СС)")


def fmt_ts(sec: float, sep: str = ",") -> str:
    ms = int(round(max(sec, 0) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def fmt_short(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
def find_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise TranscribeError("ffmpeg/ffprobe не найдены в PATH")
    return ffmpeg, ffprobe


def probe(path: str, ffprobe: str) -> dict:
    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json",
                        "-show_streams", "-show_format", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise TranscribeError(f"ffprobe не смог открыть файл: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "{}")


def pick_track(info: dict, forced: str) -> int:
    """Дорожка с голосом: явно указанная, иначе моно (обычно это микрофон), иначе первая."""
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise TranscribeError("в файле нет звука")
    if forced not in (None, "", "auto"):
        idx = int(forced)
        if not 0 <= idx < len(streams):
            raise TranscribeError(f"нет дорожки {idx} (всего {len(streams)})")
        return idx
    if len(streams) > 1:
        for i, s in enumerate(streams):
            if int(s.get("channels") or 0) == 1:
                return i
    return 0


def extract_audio(src: str, track: int, ffmpeg: str, start: float | None,
                  end: float | None, dst: str) -> None:
    """Вытаскивает дорожку в WAV 16 кГц моно — ровно то, что ест Whisper."""
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0):.3f}"]
    cmd += ["-map", f"0:a:{track}", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", dst]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.exists(dst):
        raise TranscribeError(f"ffmpeg не смог извлечь звук: {r.stderr.strip()[:300]}")


# ---------------------------------------------------------------------------
# модель (локально)
# ---------------------------------------------------------------------------
_MODEL_CACHE: dict = {}


def cuda_available() -> bool:
    """Есть ли ВИДЕОКАРТА, которую потянет faster-whisper.

    Старые карты (GT 730/710 и т.п., Kepler) CUDA 12 не поддерживают и
    не умеют float16 — на них считать бессмысленно, сразу берём процессор."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        types = ctranslate2.get_supported_compute_types("cuda")
        return "float16" in types or "int8_float16" in types
    except Exception:
        return False


def load_model(model: str | None, device: str = "auto", compute: str = "auto"):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscribeError("не установлен faster-whisper:  pip install faster-whisper")

    auto_profile = model == "auto"
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    if auto_profile or not model:
        model = "large-v3-turbo" if device == "cuda" else ("base" if auto_profile else "small")
    if model == "turbo":
        model = "large-v3-turbo"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"

    key = (model, device, compute)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], key

    _stage(f"Whisper: загружаю {model} ({device}, {compute})")
    print(f"модель      : {model}  ({device}, {compute}) — загрузка...", flush=True)
    t = time.time()
    try:
        m = WhisperModel(model, device=device, compute_type=compute,
                         cpu_threads=(os.cpu_count() or 4) if device == "cpu" else 0)
    except Exception as e:
        if device != "cuda":
            raise
        print(f"  ! видеокарта не завелась ({e}); перехожу на процессор", flush=True)
        return load_model("auto" if auto_profile else (None if model == "large-v3-turbo" else model), "cpu", "int8")
    print(f"  загружена за {time.time() - t:.1f} c", flush=True)
    _MODEL_CACHE[key] = m
    return m, key


# ---------------------------------------------------------------------------
# транскрибация
# ---------------------------------------------------------------------------
def read_wav(path: str):
    """WAV 16 кГц моно s16 -> numpy float32 (без PyAV, чтобы не зависеть от его версии)."""
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _report(progress, quiet, pct, item):
    if progress:
        try:
            progress(pct, item)
        except Exception:
            pass
    if not quiet:
        print(f"[{pct:5.1f}%] {fmt_short(item['start'])}  {item['text']}", flush=True)


def _run_local(model, wav: str, lang: str | None, beam: int, vad: bool, offset: float,
               total: float, words: bool, prompt: str | None, quiet: bool,
               progress=None) -> list[dict]:
    segments, info = model.transcribe(
        read_wav(wav),
        language=lang,
        beam_size=beam,
        vad_filter=vad,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=words,
        condition_on_previous_text=False,   # меньше "зацикливаний" на длинных записях
        initial_prompt=prompt,
    )
    if not lang:
        print(f"язык        : {info.language} ({info.language_probability:.0%})", flush=True)
    out = []
    for s in segments:
        text = s.text.strip()
        if not text:
            continue
        item = {"start": round(s.start + offset, 3), "end": round(s.end + offset, 3), "text": text}
        if words and s.words:
            item["words"] = [{"start": round(w.start + offset, 3), "end": round(w.end + offset, 3),
                              "word": w.word} for w in s.words]
        out.append(item)
        _report(progress, quiet, min(100.0, (s.end / total * 100) if total else 0), item)
    return out


# ---------------------------------------------------------------------------
# Groq (облако, бесплатно): whisper-large-v3-turbo, ~полминуты на час звука
# ---------------------------------------------------------------------------
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _groq_post(path: str, key: str, model: str, lang: str | None, prompt: str | None,
               proxies: dict | None, max_wait: float) -> dict:
    import requests
    waited = 0.0
    while True:
        with open(path, "rb") as fh:
            data = {"model": model, "response_format": "verbose_json", "temperature": "0"}
            if lang:
                data["language"] = lang
            if prompt:
                data["prompt"] = prompt[:800]
            try:
                r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                                  data=data, files={"file": (os.path.basename(path), fh, "audio/mpeg")},
                                  proxies=proxies or None, timeout=600)
            except requests.RequestException as e:
                if waited >= max_wait:
                    raise TranscribeError(f"Groq недоступен: {e}")
                print(f"  ! сеть: {e}; повтор через 15 c", flush=True)
                time.sleep(15)
                waited += 15
                continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            # бесплатный лимит: ~2 часа звука в час — ждём, сколько просят
            try:
                wait = float(r.headers.get("retry-after") or 30)
            except ValueError:
                wait = 30
            wait = min(max(wait, 5), 900)
            if waited + wait > max_wait:
                raise TranscribeError("Groq: исчерпан бесплатный лимит, попробуй позже")
            print(f"  лимит Groq, жду {wait:.0f} c...", flush=True)
            time.sleep(wait)
            waited += wait
            continue
        if r.status_code in (401, 403):
            raise TranscribeError(f"Groq: ключ не подходит или доступ закрыт ({r.status_code}): "
                                  f"{r.text[:200]}")
        if r.status_code == 413:
            raise TranscribeError("Groq: кусок звука слишком большой")
        if r.status_code >= 500 and waited < max_wait:
            time.sleep(10)
            waited += 10
            continue
        raise TranscribeError(f"Groq ответил {r.status_code}: {r.text[:300]}")


def _run_groq(src: str, track: int, ffmpeg: str, start: float, end: float, key: str,
              model: str, lang: str | None, prompt: str | None, quiet: bool, progress,
              proxies: dict | None, chunk_sec: float, tmp: str, max_wait: float) -> list[dict]:
    """Режем звук на куски по chunk_sec (mp3 32 кбит/с, ~5 МБ на 20 минут —
    бесплатный лимит Groq 25 МБ на файл) и отправляем по очереди."""
    total = end - start
    out: list[dict] = []
    pos, n = start, 0
    while pos < end - 0.5:
        length = min(chunk_sec, end - pos)
        part = os.path.join(tmp, f"part{n}.mp3")
        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{pos:.3f}", "-i", src,
               "-t", f"{length:.3f}", "-map", f"0:a:{track}", "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "libmp3lame", "-b:a", "32k", part]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise TranscribeError(f"ffmpeg не смог извлечь звук: {r.stderr.strip()[:300]}")
        res = _groq_post(part, key, model, lang, prompt, proxies, max_wait)
        for s in res.get("segments") or []:
            text = (s.get("text") or "").strip()
            # Whisper на тишине любит "Продолжение следует..." — такие сегменты
            # у Groq помечены высокой no_speech_prob
            if not text or float(s.get("no_speech_prob") or 0) > 0.8:
                continue
            item = {"start": round(float(s["start"]) + pos, 3),
                    "end": round(float(s["end"]) + pos, 3), "text": text}
            out.append(item)
            _report(progress, quiet, min(100.0, (item["end"] - start) / total * 100), item)
        os.remove(part)
        pos += length
        n += 1
    return out


def transcribe_file(path: str, *, model: str | None = None, lang: str | None = "ru",
                    device: str = "auto", compute: str = "auto", track: str = "auto",
                    start: float | None = None, end: float | None = None, beam: int | None = None,
                    vad: bool = True, words: bool = False, prompt: str | None = None,
                    quiet: bool = False, engine: str = "auto", groq_key: str | None = None,
                    groq_model: str = "whisper-large-v3-turbo", proxies: dict | None = None,
                    chunk_sec: float = 1200, progress=None, max_wait: float = 3600) -> list[dict]:
    """Возвращает список сегментов [{'start': сек, 'end': сек, 'text': str}, ...].
    Таймкоды — относительно начала исходного видео (даже если задан start).

    engine: 'local' — faster-whisper на этом компьютере;
            'groq'  — бесплатное облако Groq (нужен ключ);
            'auto'  — Groq, если есть ключ, иначе локально.
    progress: необязательный callback(pct, segment) для прогресса."""
    if not os.path.isfile(path):
        raise TranscribeError(f"файл не найден: {path}")
    if lang in ("", "auto"):
        lang = None
    groq_key = groq_key or os.environ.get("GROQ_API_KEY")
    if engine == "auto":
        engine = "groq" if groq_key else "local"
    if engine == "groq" and not groq_key:
        raise TranscribeError("для Groq нужен ключ: --groq-key или переменная GROQ_API_KEY")

    ffmpeg, ffprobe = find_tools()
    info = probe(path, ffprobe)
    dur = float(info.get("format", {}).get("duration") or 0)
    tr = pick_track(info, track)
    if end is not None and start is not None and end <= start:
        raise TranscribeError("конец фрагмента раньше начала")
    s0 = start or 0.0
    e0 = min(end, dur) if end and dur else (end or dur)
    total = e0 - s0

    print(f"файл        : {path}", flush=True)
    print(f"длительность: {fmt_short(dur)}"
          + (f"   фрагмент {fmt_short(s0)}–{fmt_short(e0)}" if start or end else ""), flush=True)
    print(f"дорожка     : 0:a:{tr}", flush=True)

    tmp = tempfile.mkdtemp(prefix="transcribe_")
    t = time.time()
    try:
        if engine == "groq":
            print(f"движок      : Groq {groq_model} (облако)", flush=True)
            _stage("Groq: передаю звук на облачное распознавание")
            segs = _run_groq(path, tr, ffmpeg, s0, e0, groq_key, groq_model, lang, prompt,
                             quiet, progress, proxies, chunk_sec, tmp, max_wait)
        else:
            m, (mname, mdev, mcomp) = load_model(model, device, compute)
            if beam is None:
                # на процессоре beam 1 ~в 1.5 раза быстрее, а текст почти тот же
                beam = 5 if mdev == "cuda" else 1
            wav = os.path.join(tmp, "audio.wav")
            _stage("FFmpeg: извлекаю PCM для Whisper")
            extract_audio(path, tr, ffmpeg, start, end, wav)
            print(f"звук извлечён за {time.time() - t:.1f} c, распознаю (beam {beam})...", flush=True)
            t = time.time()
            try:
                _stage(f"Whisper: распознаю {mname} на {mdev}, beam={beam}")
                segs = _run_local(m, wav, lang, beam, vad, s0, total, words, prompt, quiet, progress)
            except RuntimeError as e:
                # на Windows без cuBLAS/cuDNN ошибка всплывает только при первом прогоне
                if mdev != "cuda":
                    raise
                print(f"  ! ошибка CUDA ({e}); повторяю на процессоре", flush=True)
                m, _ = load_model(None if mname == "large-v3-turbo" else mname, "cpu", "int8")
                _stage("Whisper: после ошибки CUDA распознаю на CPU")
                segs = _run_local(m, wav, lang, 1, vad, s0, total, words, prompt, quiet, progress)
        el = time.time() - t
        speed = f", x{total / el:.1f} от реального времени" if el > 0 and total else ""
        print(f"готово за {el:.1f} c{speed}; фраз: {len(segs)}", flush=True)
        return segs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# запись результатов
# ---------------------------------------------------------------------------
def to_txt(segs: list[dict], timestamps: bool = True) -> str:
    if timestamps:
        return "\n".join(f"[{fmt_short(s['start'])}] {s['text']}" for s in segs) + "\n"
    # сплошной текст, абзац после паузы > 2 c
    out, prev = [], None
    for s in segs:
        if prev is not None and s["start"] - prev > 2.0:
            out.append("\n\n")
        elif out:
            out.append(" ")
        out.append(s["text"])
        prev = s["end"]
    return "".join(out).strip() + "\n"


def to_srt(segs: list[dict]) -> str:
    return "\n".join(f"{i}\n{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}\n{s['text']}\n"
                     for i, s in enumerate(segs, 1))


def to_vtt(segs: list[dict]) -> str:
    return "WEBVTT\n\n" + "\n".join(
        f"{fmt_ts(s['start'], '.')} --> {fmt_ts(s['end'], '.')}\n{s['text']}\n" for s in segs)


def save(segs: list[dict], src: str, out_dir: str | None, formats: list[str],
         plain: bool, suffix: str = "") -> list[str]:
    base = os.path.splitext(os.path.basename(src))[0] + suffix
    out_dir = out_dir or os.path.dirname(os.path.abspath(src))
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for f in formats:
        p = os.path.join(out_dir, f"{base}.{f}")
        if f == "txt":
            data = to_txt(segs, timestamps=not plain)
        elif f == "srt":
            data = to_srt(segs)
        elif f == "vtt":
            data = to_vtt(segs)
        elif f == "json":
            data = json.dumps(segs, ensure_ascii=False, indent=1)
        else:
            continue
        with open(p, "w", encoding="utf-8-sig" if f in ("txt", "srt") else "utf-8") as fh:
            fh.write(data)
        written.append(p)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Транскрибация видео/аудио в текст (faster-whisper)")
    ap.add_argument("files", nargs="+", help="видео или аудио файлы")
    ap.add_argument("--model", default=None,
                    help="tiny/base/small/medium/large-v3/large-v3-turbo "
                         "(по умолчанию turbo на GPU, small на CPU)")
    ap.add_argument("--lang", default="ru", help="язык речи: ru, en, ... или auto (по умолч. ru)")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--compute", default="auto",
                    help="float16 / int8_float16 / int8 / float32 (по умолч. авто)")
    ap.add_argument("--track", default="auto",
                    help="номер звуковой дорожки (0,1,...); auto = моно-микрофон или первая")
    ap.add_argument("--start", default=None, help="начало фрагмента ЧЧ:ММ:СС")
    ap.add_argument("--end", default=None, help="конец фрагмента ЧЧ:ММ:СС")
    ap.add_argument("--formats", default="txt,srt", help="txt,srt,vtt,json (по умолч. txt,srt)")
    ap.add_argument("--out", default=None, help="папка для результатов (по умолч. рядом с видео)")
    ap.add_argument("--plain", action="store_true", help="txt сплошным текстом без таймкодов")
    ap.add_argument("--beam", type=int, default=None,
                    help="beam size: 1 = быстрее, 5 = точнее (по умолч. 5 на GPU, 1 на CPU)")
    ap.add_argument("--fast", action="store_true",
                    help="быстрый режим для слабого ПК: модель base (в ~3-4 раза быстрее small)")
    ap.add_argument("--engine", default="auto", choices=["auto", "local", "groq"],
                    help="local = на этом ПК, groq = бесплатное облако (нужен ключ), "
                         "auto = groq, если есть ключ")
    ap.add_argument("--groq-key", default=None, help="ключ Groq (или переменная GROQ_API_KEY)")
    ap.add_argument("--no-vad", action="store_true", help="не пропускать тишину (VAD)")
    ap.add_argument("--words", action="store_true", help="таймкоды каждого слова (в json)")
    ap.add_argument("--prompt", default=None,
                    help="подсказка модели: ники, термины (напр. 'Twitch, стрим, донат')")
    ap.add_argument("--quiet", action="store_true", help="не печатать текст по ходу")
    a = ap.parse_args(argv)

    formats = [f.strip().lower() for f in a.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in ("txt", "srt", "vtt", "json")]
    if bad:
        print(f"неизвестный формат: {', '.join(bad)}", file=sys.stderr)
        return 2
    try:
        start, end = time_to_sec(a.start), time_to_sec(a.end)
    except TranscribeError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    suffix = f"_{fmt_short(start or 0).replace(':', '-')}" if start or end else ""

    rc = 0
    for f in a.files:
        try:
            segs = transcribe_file(f, model=a.model or ("base" if a.fast else None),
                                   engine=a.engine, groq_key=a.groq_key, lang=a.lang, device=a.device,
                                   compute=a.compute, track=a.track, start=start, end=end,
                                   beam=a.beam, vad=not a.no_vad, words=a.words,
                                   prompt=a.prompt, quiet=a.quiet)
            for p in save(segs, f, a.out, formats, a.plain, suffix):
                print(f"сохранено   : {p}")
        except TranscribeError as e:
            print(f"ошибка: {e}", file=sys.stderr)
            rc = 1
        except KeyboardInterrupt:
            print("\nпрервано", file=sys.stderr)
            return 130
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
