"""Groq speech adapter: 10-minute compressed uploads, no secrets in errors."""
import math
import subprocess
import tempfile
from pathlib import Path
import requests
import transcribe

URL = 'https://api.groq.com/openai/v1/audio/transcriptions'
MODEL = 'whisper-large-v3-turbo'

class GroqError(RuntimeError):
    pass


def parse_segments(data, offset, length):
    rows = data.get('segments') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise GroqError('Некорректный ответ Groq: нет сегментов с таймкодами')
    result = []
    for row in rows:
        try:
            if not isinstance(row, dict) or not isinstance(row.get('text'), str): raise ValueError()
            if isinstance(row.get('start'), bool) or isinstance(row.get('end'), bool): raise ValueError()
            a, b = float(row['start']), float(row['end'])
            if not math.isfinite(a) or not math.isfinite(b) or a < 0 or b < a or b > length + 1: raise ValueError()
            text = row['text'].strip()
        except (TypeError, ValueError, KeyError):
            raise GroqError('Некорректные сегменты или таймкоды Groq') from None
        if text: result.append({'start': offset + a, 'end': offset + b, 'text': text})
    return result


def transcribe_chunk(path, *, start, end, api_key, language='ru', proxy_url=''):
    # Extraction errors are not service outages; allow them to surface normally.
    ffmpeg, ffprobe = transcribe.find_tools()
    info = transcribe.probe(path, ffprobe)
    track = transcribe.pick_track(info, 'auto')
    with tempfile.TemporaryDirectory(prefix='narezka-groq-') as temp:
        audio = Path(temp) / 'audio.mp3'
        cmd = [ffmpeg, '-hide_banner', '-v', 'error', '-y', '-ss', f'{start:.3f}',
               '-i', str(path), '-t', f'{end-start:.3f}', '-map', f'0:a:{track}',
               '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '32k', str(audio)]
        run = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             encoding='utf8', errors='replace')
        if run.returncode or not audio.is_file() or audio.stat().st_size == 0:
            raise transcribe.TranscribeError('Не удалось подготовить аудио для Groq (FFmpeg)')
        if audio.stat().st_size >= 24_000_000:
            raise GroqError('Аудиочасть превышает допустимый размер для Groq')
        data = {'model': MODEL, 'response_format': 'verbose_json'}
        if language and language != 'auto': data['language'] = language
        session = requests.Session()
        session.trust_env = False
        if proxy_url:
            url = proxy_url.replace('socks5://', 'socks5h://', 1) if proxy_url.startswith('socks5://') else proxy_url
            session.proxies.update({'http': url, 'https': url})
        try:
            with audio.open('rb') as f:
                response = session.post(URL, headers={'Authorization': 'Bearer ' + api_key}, data=data,
                                        files={'file': ('audio.mp3', f, 'audio/mpeg')}, timeout=(15, 300))
            if not 200 <= response.status_code < 300:
                reason = 'Groq вернул HTTP ' + str(response.status_code)
                if response.status_code == 403:
                    # Allowlist known codes only. Never print an arbitrary body, key or proxy URL.
                    try:
                        body = response.json()
                        error = body.get('error', {}) if isinstance(body, dict) else {}
                        code = error.get('code') if isinstance(error, dict) else None
                    except ValueError:
                        code = None
                    if code == 'model_permission_blocked_org': reason += ': модель запрещена организацией в Groq'
                    elif code == 'model_permission_blocked_project': reason += ': модель запрещена проектом в Groq'
                    else: reason += ': доступ запрещён; проверьте права модели и доступ сети'
                raise GroqError(reason)
            try: payload = response.json()
            except ValueError: raise GroqError('Некорректный JSON от Groq') from None
            return parse_segments(payload, start, end-start)
        except requests.RequestException:
            # No automatic replay after an uncertain POST: it might already be billable.
            raise GroqError('Ошибка сети или тайм-аут Groq') from None
        finally:
            session.close()
