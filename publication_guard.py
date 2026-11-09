"""Conservative publication flags; never a copyright or source clearance."""
import json
import re
from pathlib import Path

BLOCK = 'НЕ ПУБЛИКОВАТЬ: '
REVIEW = 'ПРОВЕРКА ИСТОЧНИКА И ПРАВ: '
SHOWS = ((re.compile(r'мужское\s*(?:[/|\\—–-]\s*)?женское', re.I), 'Мужское / Женское'),
         (re.compile(r'пусть\s+говорят', re.I), 'Пусть говорят'))

def assess(row, transcript, filename=''):
    text = str(filename) + '\n' + str(row.get('title', '')) + '\n' + str(transcript)
    for pattern, show in SHOWS:
        if pattern.search(text):
            return 'block', BLOCK + 'возможно «' + show + '»; проверьте вручную. Автоматическая публикация запрещена.'
    if row.get('type') == 'reaction':
        return 'review', REVIEW + 'реакция: источник и права требуют ручной проверки до публикации.'
    if row.get('type') not in ('discussion', 'interesting', 'interesting_moment'):
        return 'review', REVIEW + 'тип или данные нарезки не подтверждены; проверьте вручную.'
    return '', ''

def assess_file(video):
    video = Path(video)
    directory = video.parent
    job = directory.parent if directory.name in ('Готовые нарезки', 'Готовые без тишины') else directory
    try:
        meta_path = job / (video.stem + '.pipeline.json')
        text_path = directory / (video.stem + '.txt')
        if meta_path.is_symlink() or text_path.is_symlink() or meta_path.stat().st_size > 2_000_000 or text_path.stat().st_size > 20_000_000:
            raise ValueError('unsafe')
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
        row = meta['reaction']
        if not isinstance(row, dict): raise ValueError('invalid')
        transcript = text_path.read_text(encoding='utf-8-sig')
        return assess(row, transcript, video.stem)
    except (OSError, ValueError, TypeError, KeyError):
        return 'review', REVIEW + 'не удалось проверить метаданные или транскрипт; до проверки не публиковать.'
