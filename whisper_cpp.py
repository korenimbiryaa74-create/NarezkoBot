"""Experimental whisper.cpp adapter for the desktop pipeline (no Python ASR wheels)."""
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
import transcribe

ROOT = Path(__file__).resolve().parent
CLI = ROOT / 'desktop' / 'tools' / 'whisper' / 'whisper-cli.exe'
MODEL = ROOT / 'desktop' / 'tools' / 'whisper-model' / 'ggml-large-v3-turbo.bin'


def parse_segments(data, offset, length):
    rows = data.get('transcription') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise transcribe.TranscribeError('whisper.cpp вернул некорректный JSON')
    result = []
    for row in rows:
        try:
            text = row['text'].strip()
            begin, end = float(row['offsets']['from']) / 1000, float(row['offsets']['to']) / 1000
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise transcribe.TranscribeError('Некорректный сегмент whisper.cpp') from e
        if not math.isfinite(begin + end) or begin < 0 or end < begin or end > length + 1:
            raise transcribe.TranscribeError('Некорректные таймкоды whisper.cpp')
        if text:
            result.append({'start': round(begin + offset, 3), 'end': round(end + offset, 3), 'text': text})
    return result


def transcribe_file(path, *, model=None, lang='ru', device='auto', compute='auto', beam=1,
                    start=None, end=None, quiet=True, engine='local', **kwargs):
    if model not in (None, 'auto', 'turbo', 'large-v3-turbo'):
        raise transcribe.TranscribeError('В этой сборке доступна только модель large-v3-turbo')
    if device not in ('auto', 'cpu', 'cuda'):
        raise transcribe.TranscribeError('Недопустимое устройство Whisper')
    if not CLI.is_file() or not MODEL.is_file():
        raise transcribe.TranscribeError('Нет whisper.cpp или модели. Запустите INSTALL.bat')
    ffmpeg, ffprobe = transcribe.find_tools()
    info = transcribe.probe(path, ffprobe)
    duration = float(info.get('format', {}).get('duration') or 0)
    begin = float(start or 0)
    finish = min(float(end), duration) if end is not None and duration else float(end or duration)
    if begin < 0 or finish <= begin:
        raise transcribe.TranscribeError('Некорректный диапазон аудио')
    with tempfile.TemporaryDirectory(prefix='narezka-whisper-') as tmp:
        wav = str(Path(tmp) / 'audio.wav')
        out = Path(tmp) / 'transcript'
        transcribe.extract_audio(path, transcribe.pick_track(info, 'auto'), ffmpeg, begin, finish, wav)
        # Passing an absolute model path containing non-ASCII characters can crash
        # Windows builds while converting argv. Use the relative path between the
        # bundled ASCII-named directories, while retaining the CLI working directory.
        model_arg = os.path.relpath(MODEL, CLI.parent)
        cmd = [str(CLI), '-m', model_arg, '-f', wav, '-l', lang or 'auto', '-bs', str(int(beam)), '-mc', '0', '-oj', '-of', str(out)]
        if device == 'cpu': cmd.append('-ng')
        try:
            run = subprocess.run(cmd, cwd=str(CLI.parent), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 encoding='utf-8', errors='replace', timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise transcribe.TranscribeError('Не удалось запустить whisper.cpp: ' + str(e)) from e
        target = out.with_suffix('.json')
        if run.returncode != 0 or not target.is_file():
            raise transcribe.TranscribeError('Ошибка whisper.cpp (код %s): %s' % (run.returncode, run.stderr[-1200:]))
        try:
            data = json.loads(target.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as e:
            raise transcribe.TranscribeError('Не удалось прочитать результат whisper.cpp') from e
        return parse_segments(data, begin, finish - begin)
