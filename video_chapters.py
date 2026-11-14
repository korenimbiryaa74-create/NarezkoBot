"""Таймкоды (главы YouTube) для разговорных нарезок — локальной моделью через Ollama, без ChatGPT.

Берётся расшифровка готовой нарезки «<видео>.txt» (времена уже от начала готового видео, с учётом
вырезанной тишины), модель размечает её кусками, затем главы приводятся к правилам YouTube:
первая 00:00, не меньше 3 глав, между главами не меньше 10 секунд. Модель выгружается из видеопамяти
сразу после работы, чтобы не мешать рендеру и превью."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests

OLLAMA = 'http://127.0.0.1:11434'
DEFAULT_MODEL = 'qwen3:8b'
CHUNK_CHARS = 3500      # кусок расшифровки на один запрос (умещается в видеопамять с запасом)
NUM_CTX = 6144
MIN_GAP = 30            # минимум секунд между главами
MAX_CHAPTERS = 20
_LINE = re.compile(r'^\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)$')
_TIME = re.compile(r'^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$')


class ChaptersError(RuntimeError):
    pass


def read_clip_transcript(video_path):
    """(разговорная ли нарезка, [(секунда, текст)]) из «<видео>.txt» рядом с нарезкой."""
    p = Path(video_path).with_suffix('.txt')
    if not p.is_file():
        return False, []
    text = p.read_text(encoding='utf-8-sig', errors='replace')
    head = '\n'.join(text.splitlines()[:3]).lower()
    lines = []
    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if m and m.group(4).strip():
            lines.append((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), m.group(4).strip()))
    return ('обсуждение' in head), lines


def _hms(sec, long):
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f'{h}:{m:02}:{s:02}' if long else f'{m:02}:{s:02}'


def _parse_time(value):
    m = _TIME.match(str(value or '').strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _clean_title(t):
    t = re.sub(r'\s+', ' ', str(t or '')).strip().strip('"\'«»“”').rstrip('.').strip()
    return t[:70]


def _ensure_server(log):
    try:
        requests.get(OLLAMA + '/api/tags', timeout=3)
        return
    except requests.RequestException:
        pass
    exe = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Ollama' / 'ollama.exe'
    if not exe.is_file():
        raise ChaptersError('Ollama не запущена и не найдена')
    log('таймкоды: запускаю Ollama')
    subprocess.Popen([str(exe), 'serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(30):
        time.sleep(1)
        try:
            requests.get(OLLAMA + '/api/tags', timeout=3)
            return
        except requests.RequestException:
            continue
    raise ChaptersError('Ollama не запустилась')


def _ask(model, prompt, keep_alive):
    r = requests.post(OLLAMA + '/api/chat', timeout=300, json={
        'model': model, 'stream': False, 'think': False, 'format': 'json', 'keep_alive': keep_alive,
        'options': {'temperature': 0.2, 'num_ctx': NUM_CTX},
        'messages': [{'role': 'user', 'content': prompt}]})
    r.raise_for_status()
    content = (r.json().get('message') or {}).get('content') or ''
    m = re.search(r'\{.*\}', content, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    return data.get('chapters') if isinstance(data.get('chapters'), list) else []


def _unload(model):
    try:
        requests.post(OLLAMA + '/api/generate', json={'model': model, 'keep_alive': 0}, timeout=15)
    except requests.RequestException:
        pass


PROMPT = ('Фрагмент расшифровки разговорного видео со стрима{who} (с {a} по {b}); в скобках — время от начала видео.\n'
          'Выдели 2–4 главы по смене темы разговора для оглавления YouTube. Время главы — строго время строки из фрагмента, '
          'где начинается тема. Названия по-русски, 3–8 слов, в третьем лице, конкретно о чём речь; можно вопросом. '
          'Примеры стиля: «{name} о любви к покеру и планах тренироваться», «Почему ему не хватает дисциплины за столом», '
          '«Подкрутка или совпадение?», «Две невероятные раздачи за один вечер». Формулируй по-разному: имя стримера — '
          'не больше чем в одной главе из трёх, не начинай главы одинаково. Без кавычек, мата и выдумок.\n'
          'Ответ только JSON: {{"chapters":[{{"time":"ЧЧ:ММ:СС","title":"..."}}]}}\n\n{text}')


PROMPT_EN = ('Transcript fragment of a talking-segment video from a stream{who} (from {a} to {b}); the time in brackets is '
             'from the start of the video.\n'
             'Pick 2–4 chapters where the topic of conversation changes, for a YouTube table of contents. The chapter time must be '
             'exactly the time of the transcript line where the topic starts. Titles in natural American English, 3–8 words, '
             'third person, concrete; questions are fine. Style examples: "{name} on his love for poker and training plans", '
             '"Why he lacks discipline at the table", "Rigged or coincidence?", "Two insane hands in one night". Vary the wording: '
             'use the streamer name in at most one chapter out of three, do not start chapters the same way. No quotes, '
             'profanity or made-up facts.\n'
             'Answer with JSON only: {{"chapters":[{{"time":"HH:MM:SS","title":"..."}}]}}\n\n{text}')


def _en():
    try:
        import lang
        return lang.is_en()
    except Exception:  # noqa: BLE001
        return False


def make_chapters(lines, who='', model=DEFAULT_MODEL, log=print):
    """[(секунда, название)] по правилам YouTube или [] если видео слишком короткое / ответ негодный."""
    if len(lines) < 5:
        return []
    duration = lines[-1][0]
    if duration < 120:
        return []
    chunks, cur, size = [], [], 0
    for sec, text in lines:
        row = f'[{_hms(sec, True).zfill(8)}] {text}'
        if cur and size + len(row) > CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((sec, row))
        size += len(row) + 1
    if cur:
        chunks.append(cur)
    _ensure_server(log)
    name = who or ('The streamer' if _en() else 'Стример')
    found = []
    try:
        for i, ch in enumerate(chunks):
            a, b = ch[0][0], ch[-1][0]
            prompt = (PROMPT_EN if _en() else PROMPT).format(who=(' ' + who) if who else '', a=_hms(a, True), b=_hms(b, True), name=name,
                                   text='\n'.join(r for _, r in ch))
            for item in _ask(model, prompt, '2m'):
                if not isinstance(item, dict):
                    continue
                t, title = _parse_time(item.get('time')), _clean_title(item.get('title'))
                if t is None or not title or not (a - 5 <= t <= b + 5):
                    continue
                # Привязка к ближайшей строке расшифровки внутри куска.
                t = min((s for s, _ in ch), key=lambda s: abs(s - t))
                found.append((t, title))
    finally:
        _unload(model)
    found.sort()
    out = []
    for t, title in found:
        if t > duration - 10:
            continue
        if out and (t - out[-1][0] < MIN_GAP or title.lower() == out[-1][1].lower()):
            continue
        out.append((t, title))
    if not out:
        return []
    out[0] = (0, out[0][1])
    while len(out) > MAX_CHAPTERS:
        gaps = [(out[i][0] - out[i - 1][0], i) for i in range(1, len(out))]
        out.pop(min(gaps)[1])
    return out if len(out) >= 3 else []


def format_block(chapters):
    long = any(t >= 3600 for t, _ in chapters)
    return ('⏱️Timestamps\n' if _en() else '⏱️Таймкоды\n') + '\n'.join(f'{_hms(t, long)} {title}' for t, title in chapters)


def descriptions_for(files, base, who='', settings=None, log=print):
    """Описание для каждой нарезки: у разговорных — base + таймкоды, у остальных — base."""
    settings = settings or {}
    if settings.get('enabled') is False:
        return [base] * len(files)
    model = settings.get('model') or DEFAULT_MODEL
    result = []
    for f in files:
        desc = base
        try:
            discussion, lines = read_clip_transcript(f)
            if discussion:
                t0 = time.time()
                chapters = make_chapters(lines, who=who, model=model, log=log)
                if chapters:
                    desc = (base.rstrip() + '\n\n' + format_block(chapters)).strip()
                    log(f'таймкоды: {Path(f).stem[:50]} — глав {len(chapters)} за {time.time() - t0:.0f} с')
                else:
                    log(f'таймкоды: {Path(f).stem[:50]} — слишком короткое или модель не дала глав, без таймкодов')
        except Exception as exc:  # noqa: BLE001 — таймкоды не должны мешать публикации
            log(f'таймкоды: {Path(f).stem[:50]} — пропущены ({type(exc).__name__}: {str(exc)[:120]})')
        result.append(desc)
    return result
