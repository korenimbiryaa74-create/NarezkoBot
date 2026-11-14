"""Язык стримера/канала: русский (по умолчанию) или английский — для американских каналов.

Язык задаётся в config.json → "languages": {"jynxzi": "en"} (вкладка «Каналы» → «Английские каналы (США)»).
На время обработки задачи язык ставится в контекст потока (clip-worker, Shorts), и все тексты берут его
оттуда: распознавание речи, все запросы к ChatGPT, названия, описание, таймкоды, текст на превью, Shorts."""
from __future__ import annotations

import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_FOOTER_EN = '💬 My Telegram - https://t.me/korenmbr'
_local = threading.local()


def _config():
    try:
        return json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def for_streamer(login, cfg=None):
    cfg = _config() if cfg is None else cfg
    value = str(((cfg.get('languages') or {}).get(str(login or '').strip().lower())) or 'ru').lower()
    return 'en' if value == 'en' else 'ru'


def set_current(language):
    _local.lang = 'en' if language == 'en' else 'ru'


def current():
    return getattr(_local, 'lang', 'ru')


def is_en():
    return current() == 'en'


def footer(cfg=None):
    """Подпись описания для английских каналов (своё поле во вкладке «Каналы»)."""
    yt = ((_config() if cfg is None else cfg).get('youtube') or {})
    value = yt.get('description_footer_en')
    return DEFAULT_FOOTER_EN if value is None else str(value)


EN_RULES = (
    '\n\n=== LANGUAGE OVERRIDE — HIGHEST PRIORITY ===\n'
    'This is an ENGLISH-language stream for a US YouTube channel; the transcript and chat are in English. '
    'Follow every rule above, but write EVERY human-readable text field of your answer (title, video_title, '
    'preview_text, preview_context, hook, why, summaries, chapter titles, descriptions) in natural, catchy '
    'American English for a US audience. Never use Russian. Russian example phrases above only illustrate the '
    'meaning — use English equivalents (for example reaction openers like "let\'s watch this", "let\'s check this out", '
    '"okay chat, let\'s see"). Thumbnail text (preview_text): 2–5 punchy English words in CAPS. '
    'Keep exactly the same JSON structure, keys, time formats and enum values (reaction / discussion / game).')


def adapt(prompt):
    """Промпт для ChatGPT: для английских каналов добавляет требование отвечать на английском."""
    return prompt + EN_RULES if is_en() else prompt


def display_name(login, aliases=None):
    """Имя стримера в названиях: русский псевдоним для русских каналов, Twitch-ник с заглавной для английских."""
    login = str(login or '').strip()
    if not login:
        return ''
    if not is_en() and aliases and login.lower() in aliases:
        return aliases[login.lower()]
    if is_en():
        return login[:1].upper() + login[1:]
    return (aliases or {}).get(login.lower(), login)
