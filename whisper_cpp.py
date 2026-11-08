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


class InvalidWhisperSegments(transcribe.TranscribeError):
    """The local recognizer ran, but its output cannot be safely timed."""


def parse_segments(data, offset, length):
    rows = data.get('transcription') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise InvalidWhisperSegments('whisper.cpp: нет списка сегментов')
    result = []
    for i, row in enumerate(rows, 1):
        prefix = f'whisper.cpp: сегмент {i}/{len(rows)}'
        if not isinstance(row, dict) or not isinstance(row.get('text'), str):
            raise InvalidWhisperSegments(prefix + ': нет текста')
        text = row['text'].strip()
        if not text: continue
        try:
            offsets=row['offsets']
            if isinstance(offsets['from'],bool) or isinstance(offsets['to'],bool): raise ValueError()
            begin,end=float(offsets['from'])/1000,float(offsets['to'])/1000
        except (KeyError, TypeError, ValueError, OverflowError):
            raise InvalidWhisperSegments(prefix + ': нет числовых таймкодов') from None
        if not math.isfinite(begin) or not math.isfinite(end):
            raise InvalidWhisperSegments(prefix + ': нечисловые таймкоды')
        limit = 30.5 if i == len(rows) else 1.0
        if begin < -0.25 or end < begin or end > length + limit or begin >= length or end < 0:
            # Numeric diagnostics only: never log the segment text.
            raise InvalidWhisperSegments(prefix + f': таймкоды {begin:.3g}–{end:.3g} вне части 0–{length:.3g} с')
        uncertain = end > length + 1
        begin=max(0.,begin);end=min(length,end)
        if end < begin: raise InvalidWhisperSegments(prefix + ': неверный порядок таймкодов')
        segment={'start': round(begin + offset, 3), 'end': round(end + offset, 3), 'text': text}
        if uncertain: segment['timing_warning']='end_clipped'
        result.append(segment)
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
                                 encoding='utf-8', errors='replace', timeout=3600, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
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
