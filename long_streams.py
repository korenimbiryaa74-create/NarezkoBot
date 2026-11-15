# -*- coding: utf-8 -*-
"""Длинные стримы — два режима для всех стримеров (настройка pipeline.long_stream).

normal (обычный, по умолчанию): стрим длиннее лимита -> скачиваются, транскрибируются и уходят в ChatGPT
        только первые limit_hours часов.
full   (запасной, например на выход GTA 6): весь стрим делится на ceil(длительность / лимит) почти равных частей
        (40 ч -> 4, 30 ч -> 3); части по очереди отправляются в ChatGPT, найденные моменты объединяются.
        Ответ каждой части сохраняется в _work/long_stream_parts.json: при повторе задания готовые части
        повторно в ChatGPT не отправляются.
"""
from __future__ import annotations

import json
import math
import os
import statistics
from pathlib import Path

MODES = {'normal': 'Обычный — только первые N часов', 'full': 'Запасной — весь стрим частями'}
DEFAULT = {'mode': 'normal', 'limit_hours': 10.0}
TOLERANCE = 600  # стрим на 10 ч 05 мин не обрезается и не делится
CACHE_NAME = 'long_stream_parts.json'

PART_NOTE = ('\nВАЖНО: это часть {i} из {n} длинного стрима (весь стрим — {total}). В файле только отрезок {a}–{b}; '
             'таймкоды в расшифровке указаны от начала всего стрима — так их и пиши в ответе. Ищи моменты только '
             'внутри этого отрезка. Overview и last_timestamp — только по этой части.\n')


def normalize(value):
    """Strict check for settings.py: returns {'mode','limit_hours'} or raises ValueError."""
    raw = value if isinstance(value, dict) else {}
    mode = raw.get('mode') if raw.get('mode') in MODES else 'normal'
    try:
        hours = float(str(raw.get('limit_hours', DEFAULT['limit_hours'])).replace(',', '.'))
    except (TypeError, ValueError):
        raise ValueError('Длинные стримы: лимит часов должен быть числом') from None
    if not math.isfinite(hours) or not 1 <= hours <= 48:
        raise ValueError('Длинные стримы: лимит от 1 до 48 часов')
    return {'mode': mode, 'limit_hours': hours}


def config(cfg):
    """(mode, limit_seconds) from the pipeline config; a broken value falls back to the default."""
    try:
        s = normalize((cfg or {}).get('long_stream'))
    except ValueError:
        s = dict(DEFAULT)
    return s['mode'], float(s['limit_hours']) * 3600.0


def _is_long(duration, limit):
    try:
        return bool(duration) and float(duration) > limit + TOLERANCE
    except (TypeError, ValueError):
        return False


def normal_limit(cfg, duration):
    """Seconds to keep in the normal mode, or None (short stream / full mode / unknown duration)."""
    mode, limit = config(cfg)
    return limit if mode == 'normal' and _is_long(duration, limit) else None


def full_parts(cfg, duration):
    """How many ChatGPT parts in the full mode (1 = send as one file, as before)."""
    mode, limit = config(cfg)
    if mode != 'full' or not _is_long(duration, limit):
        return 1
    return max(2, math.ceil((float(duration) - TOLERANCE) / limit))


def hms(sec):
    sec = int(round(float(sec)))
    return f'{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


def _hm(sec):
    sec = int(sec)
    return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}'


def part_bounds(segs, duration, n):
    """n почти равных частей; граница — на паузе в речи в пределах ±5 минут от равного деления."""
    duration = float(duration)
    points, prev_end = [], 0.0
    for s in sorted(segs, key=lambda x: float(x['start'])):
        a = float(s['start'])
        points.append((a, a - prev_end))
        prev_end = max(prev_end, float(s['end']))
    bounds = [0.0]
    for i in range(1, n):
        target = duration * i / n
        near = [p for p in points if abs(p[0] - target) <= 300 and bounds[-1] + 600 < p[0] < duration - 600]
        pool = [p for p in near if p[1] >= 0.8] or near
        bounds.append(round(min(pool, key=lambda p: abs(p[0] - target))[0] if pool else target, 3))
    bounds.append(round(duration, 3))
    return bounds


def part_segs(segs, a, b):
    """Фразы части с временем от начала части (find_reactions_whole сам прибавит offset в файле)."""
    out = []
    for s in segs:
        st = float(s['start'])
        if a <= st < b:
            out.append(dict(s, start=round(st - a, 3), end=round(max(st, min(float(s['end']), b)) - a, 3)))
    return out


def game_data(vod_id, work, cli, duration, progress=None):
    """Chapters + per-minute chat activity for the whole VOD (chat.json is cached in the work folder)."""
    import twitch_vod
    chaps = twitch_vod.chapters(vod_id, duration)
    path = twitch_vod.download_chat(vod_id, Path(work), cli, progress=progress)
    counts, words = twitch_vod.chat_activity(path, duration)
    if progress:
        progress(f'Чат: сообщений {sum(counts)}, игр по главам Twitch: {len(chaps)}')
    return chaps, counts, words


def game_document_range(chaps, counts, words, a, b):
    """Same block as twitch_vod.game_document, but only for the part a..b (times from the stream start)."""
    import twitch_vod
    m0, m1 = int(a // 60), min(len(counts), int(math.ceil(b / 60)))
    lines = [twitch_vod.HEADER,
             f'Это не расшифровка: данные Twitch для поиска игровых моментов (только эта часть стрима {_hm(a)}–{_hm(b)}).', '',
             'ИГРЫ ПО ВРЕМЕНИ (главы Twitch):']
    ch = [c for c in chaps if float(c['end']) > a and float(c['start']) < b]
    lines += [f"{_hm(max(float(c['start']), a))}–{_hm(min(float(c['end']), b))}  {c['game']}" for c in ch] or ['нет данных']
    active = [x for x in counts[m0:m1] if x > 0]
    base = statistics.median(active) if active else 0
    lines += ['', f'АКТИВНОСТЬ ЧАТА: ЧЧ:ММ — сообщений за минуту; обычный уровень ≈ {base:.0f}; ▲ — всплеск (вдвое выше обычного и больше); '
                  'в кавычках — самые частые сообщения минуты.']
    for m in range(m0, m1):
        n = counts[m]
        spike = base > 0 and n >= max(2 * base, base + 10)
        line = f'{_hm(m * 60)} {n}' + (' ▲' if spike else '')
        if spike:
            top = [f'«{w}»×{k}' for w, k in words[m].most_common(3) if k >= 3]
            if top:
                line += ' ' + ', '.join(top)
        lines.append(line)
    return '\n'.join(lines)


def _load_cache(path, key):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('key') == key and isinstance(data.get('parts'), dict):
            return data
    except Exception:
        pass
    return {'key': key, 'parts': {}}


def _save_cache(path, data):
    tmp = str(path) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def find_in_parts(video, segs, duration, settings, n, ask, who='', game=None, work=None, log=print):
    """Запасной режим: части по очереди в ChatGPT. Возвращает моменты с таймкодами от начала стрима."""
    import reactions
    bounds = part_bounds(segs, duration, n)
    log(f'Длинный стрим {hms(duration)}: режим «запасной» — делю на {n} частей и отправляю в ChatGPT по очереди: '
        + ', '.join(f'{hms(a)}–{hms(b)}' for a, b in zip(bounds, bounds[1:])))
    key = {'v': 1, 'duration': round(float(duration), 1), 'bounds': bounds,
           'settings': {k: v for k, v in settings.get('reactions', {}).items() if k != 'chatgpt_timeout'}}
    cache_path = Path(work) / CACHE_NAME if work else None
    cache = _load_cache(cache_path, key) if cache_path else {'key': key, 'parts': {}}
    stem, ext = os.path.splitext(video)
    rows = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
        done = cache['parts'].get(str(i))
        if isinstance(done, list):
            log(f'Часть {i}/{n} · {hms(a)}–{hms(b)}: ответ ChatGPT получен ранее — беру сохранённый ({len(done)} моментов)')
            rows += [dict(r, _grp=(i, r.get('_grp'))) for r in done]
            continue
        ps = part_segs(segs, a, b)
        if not ps:
            log(f'Часть {i}/{n} · {hms(a)}–{hms(b)}: речи нет — пропускаю')
            part_rows = []
        else:
            log(f'Часть {i}/{n} · {hms(a)}–{hms(b)}: отправляю в ChatGPT')
            extra = {'chatgpt_doc': ask, 'game_doc': game_document_range(*game, a, b) if game else ''}
            res = reactions.find_reactions_whole(video if i == 1 else f'{stem}.part{i}{ext}', ps, b - a, settings,
                                                 extra=extra, who=who, log=log, offset=a,
                                                 note=PART_NOTE.format(i=i, n=n, a=hms(a), b=hms(b), total=hms(duration)))
            part_rows = []
            for r in res['reactions']:
                r = dict(r, start=round(float(r['start']) + a, 3), end=round(float(r['end']) + a, 3), part=i)
                if isinstance(r.get('video'), list):
                    r['video'] = [round(float(x) + a, 3) for x in r['video']]
                part_rows.append(r)
            log(f'Часть {i}/{n}: найдено моментов — {len(part_rows)}')
        cache['parts'][str(i)] = part_rows
        if cache_path:
            try:
                _save_cache(cache_path, cache)
            except OSError as exc:
                log(f'Не удалось сохранить ответ части {i}: {exc}')
        rows += [dict(r, _grp=(i, r.get('_grp'))) for r in part_rows]
    rows.sort(key=lambda r: r['start'])
    groups = {}
    for r in rows:  # уникальные группы частей одного обсуждения во всём стриме
        r['_grp'] = groups.setdefault(json.dumps(r['_grp'], default=str), len(groups))
    log(f'Все {n} части обработаны: моментов всего — {len(rows)}')
    return rows
