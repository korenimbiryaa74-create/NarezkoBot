"""Очистка диска: находит и удаляет только то, что боту больше точно не нужно.

Никогда не трогает: стримы в очереди/нарезке, нарезки в очереди загрузки, в загрузке и на проверке,
мелкие текстовые файлы с названиями и описаниями.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

ACTIVE_JOB = ('queued', 'running', 'retry')
READY = 'Готовые без тишины'
VIDEO = ('.mp4', '.mkv', '.mov', '.webm', '.ts')


@dataclass
class Category:
    key: str
    title: str
    hint: str
    default: bool
    paths: list = field(default_factory=list)
    size: int = 0


def _size(p):
    p = Path(p)
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
    except OSError:
        return 0


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p)))


def fmt(n):
    return f'{n / 1024 ** 3:.1f} ГБ' if n >= 100 * 1024 ** 2 else f'{n / 1024 ** 2:.0f} МБ'


def _rows(path, sql):
    if not Path(path).exists():
        return []
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql)]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def scan(root, min_age_days=3):
    root = Path(root)
    rec = root / 'recordings'
    jobs = _rows(root / 'state' / 'jobs.sqlite3', 'SELECT id,vod_id,streamer,state,updated_at FROM jobs')
    uploads = _rows(root / 'state' / 'jobs.sqlite3', 'SELECT job_id,file_path,state FROM youtube_uploads')
    folders = [d for d in rec.iterdir() if d.is_dir()] if rec.exists() else []

    def folders_of(job):
        vod = str(job['vod_id'] or '')
        return [d for d in folders if vod and d.name.endswith(vod)]

    active = {_norm(d) for j in jobs if j['state'] in ACTIVE_JOB for d in folders_of(j)}
    finished = [j for j in jobs if j['state'] not in ACTIVE_JOB]
    states, original = {}, {}
    for u in uploads:
        states.setdefault(_norm(u['file_path']), set()).add(u['state'])
        original.setdefault(_norm(u['file_path']), u['file_path'])

    test_dirs = [d for j in finished if j['streamer'] == 'test' for d in folders_of(j) if _norm(d) not in active]
    test_set = {_norm(d) for d in test_dirs}
    published = [Path(original[p]) for p, st in states.items() if 'confirmed' in st and st <= {'confirmed', 'cancelled'}
                 and Path(original[p]).is_file() and _norm(Path(p).parent.parent) not in active and _norm(Path(p).parent.parent) not in test_set]
    # _work нужен для разметки моментов шортсов: пока моменты не размечены (нет shorts_moments.json) — неделю не трогаем
    work = [d / '_work' for j in finished for d in folders_of(j)
            if (d / '_work').exists() and _norm(d) not in active and _norm(d) not in test_set
            and ((d / 'shorts_moments.json').exists() or time.time() - float(j['updated_at'] or 0) > 7 * 86400)]
    old = time.time() - min_age_days * 86400
    unpublished = []
    for j in finished:
        if j['streamer'] == 'test' or j['state'] != 'done' or float(j['updated_at'] or 0) > old:
            continue
        for d in folders_of(j):
            if _norm(d) in active:
                continue
            for f in (d / READY).glob('*') if (d / READY).exists() else []:
                if f.suffix.lower() in VIDEO and f.is_file() and not (states.get(_norm(f), set()) - {'cancelled'}):
                    unpublished.append(f)

    # Shorts, уже опубликованные: видео, рабочая папка сборки, превью и временные файлы
    shorts = []
    factory = None
    for r in _rows(root / 'state' / 'shorts.sqlite3', "SELECT value FROM settings WHERE key='factory_dir'"):
        factory = Path(r['value'])
    factory = factory or Path.home() / 'Desktop' / 'ShortsFactory'
    for it in _rows(root / 'state' / 'shorts.sqlite3', "SELECT id,profile,slug,video_path FROM items WHERE status='done'"):
        slug = it['slug'] or ''
        cand = [Path(it['video_path'])] if it['video_path'] else []
        if slug:
            cand += [factory / 'build' / slug, factory / 'output' / f'{slug}_preview.jpg', factory / 'output' / f'{slug}_qc.txt']
            cand += list((factory / 'jobs').glob(f'{slug}_*')) if (factory / 'jobs').exists() else []
        shorts += [p for p in cand if p.exists()]

    tests_in = [f for f in (root / 'test_input').glob('*') if f.is_file() and f.suffix.lower() in VIDEO] if (root / 'test_input').exists() else []

    cats = [
        Category('published', 'Нарезки, уже выложенные на YouTube', 'Видео, которые YouTube подтвердил как опубликованные. Само видео остаётся на YouTube.', True, published),
        Category('work', 'Рабочие файлы нарезанных стримов', 'Уменьшенная копия стрима и временные файлы анализа (папки _work) у уже законченных заданий.', True, work),
        Category('tests', 'Тестовые прогоны', 'Папки «Тест …» в recordings — результаты тестов из папки, вместе с их нарезками.', True, test_dirs),
        Category('shorts', 'Опубликованные Shorts', 'Видео и рабочие файлы ShortsFactory для Shorts, которые уже выложены.', True, shorts),
        Category('unpublished', f'Нарезки, которые не выкладывались (старше {min_age_days} дн.)', 'Готовые нарезки, которых нет в очереди загрузки на YouTube. Удаляйте, только если они точно не нужны.', False, unpublished),
        Category('test_input', 'Тестовые видео в test_input', 'Исходные видео для «Тест из папки». После удаления тест по ним не запустить.', False, tests_in),
    ]
    for c in cats:
        c.size = sum(_size(p) for p in c.paths)
    return cats


def clean(root, keys, min_age_days=3):
    """Пересканирует (чтобы не удалить то, что успело стать нужным) и удаляет выбранные категории.
    Возвращает (освобождено байт, удалено объектов, список ошибок)."""
    freed, count, errors = 0, 0, []
    for c in scan(root, min_age_days):
        if c.key not in keys:
            continue
        for p in c.paths:
            size = _size(p)
            try:
                if Path(p).is_dir():
                    shutil.rmtree(p)
                else:
                    Path(p).unlink()
                freed += size
                count += 1
            except OSError as exc:
                errors.append(f'{Path(p).name}: {exc.strerror or exc}')
    return freed, count, errors
