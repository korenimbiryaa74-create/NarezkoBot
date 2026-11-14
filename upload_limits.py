"""Дневной лимит загрузок YouTube по каналам — один общий таймер на канал.

Когда Studio показывает «Загрузка недоступна… можно будет через 24 часа», лимит действует на весь канал:
и на полные видео, и на Shorts. Поэтому канал ставится на паузу на 24 часа целиком, а повторное
срабатывание во время паузы таймер НЕ продлевает: пауза заканчивается через 24 часа после первого окна.
Как только пауза заканчивается, все накопившиеся нарезки и Shorts канала выкладываются сами (они стоят в очереди).
Таймеры хранятся в state/upload_limits.json и переживают перезапуск."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

HOURS = 24
_ROOT = Path(__file__).resolve().parent
_lock = threading.Lock()


def _path():
    return _ROOT / 'state' / 'upload_limits.json'


def _read():
    try:
        data = json.loads(_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _end(row, now):
    """Конец паузы канала. Старый формат {'video': t, 'short': t} → самый ранний действующий таймер."""
    if not isinstance(row, dict):
        return 0.0
    if 'until' in row:
        return float(row.get('until') or 0)
    active = [float(v or 0) for v in row.values() if isinstance(v, (int, float)) and float(v or 0) > now]
    return min(active) if active else 0.0


def _load():
    now = time.time()
    return {p: {'until': _end(r, now), 'since': float((r or {}).get('since') or 0) if isinstance(r, dict) else 0.0}
            for p, r in _read().items()}


def _save(data):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def _key(profile):
    try:
        from youtube_queue import profile_key
        return profile_key(profile)
    except Exception:  # noqa: BLE001
        return str(profile or '').strip().lower()


def until(profile, kind=None):
    """Конец паузы канала (timestamp) или 0. kind оставлен для совместимости: лимит общий."""
    with _lock:
        value = float((_load().get(_key(profile)) or {}).get('until') or 0)
    return value if value > time.time() else 0.0


def active(profile, kind=None):
    return until(profile) > 0


def hit(profile, kind=None, hours=HOURS, at=None):
    """YouTube показал лимит. Если пауза уже идёт — она не продлевается, возвращается её конец."""
    now = time.time() if at is None else float(at)
    with _lock:
        data = _load()
        row = data.get(_key(profile)) or {}
        if float(row.get('until') or 0) > time.time():
            return float(row['until'])
        end = now + hours * 3600
        data[_key(profile)] = {'until': end, 'since': now}
        _save(data)
    return end


def clear(profile, kind=None):
    with _lock:
        data = _load()
        data.pop(_key(profile), None)
        _save(data)


def fmt(ts):
    return time.strftime('%d.%m %H:%M', time.localtime(ts)) if ts else ''


def snapshot():
    """{профиль: конец паузы} — только действующие паузы."""
    now = time.time()
    with _lock:
        data = _load()
    return {p: r['until'] for p, r in data.items() if r['until'] > now}
