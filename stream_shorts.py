"""Шортсы со стримов (YouTube Shorts / TikTok).

1. По записи стрима собирается один документ блоками по 20 с: карта громкости (тихо / громко / всплески),
   активность чата и смех в чате, расшифровка речи.
2. Документ уходит в ChatGPT одним файлом — он размечает весь стрим на много самостоятельных моментов 12–59 с.
3. Моменты встают в очередь канала стримера и выкладываются по расписанию, пока не кончатся. Каждый момент
   скачивается с Twitch в хорошем качестве (только нужный отрезок) и становится вертикальным видео с субтитрами.
"""
from __future__ import annotations

import bisect
import collections
import json
import math
import re
import shutil
import statistics
import subprocess
import time
from pathlib import Path

BLOCK = 20
MARKER = 'shorts_moments.json'
MIN_LEN, MAX_LEN = 12, 59
MAX_MOMENTS = 70  # больше с одного стрима не берём — только лучшие
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
BARS = '▁▂▃▄▅▆▇█'
LAUGH = re.compile(r'(?:а?х[ах]{3,}|х[еи]{3,}|kekw|lul|омегалул|ору+\b|орнул|ржу|😂|🤣|\bxd+\b|\bлол\b)', re.I)
ROOT = Path(__file__).resolve().parent
FONT_FILE = ROOT / 'fonts' / 'Montserrat-Black.ttf'

PROMPT = """Ты — монтажёр вертикальных роликов (YouTube Shorts и TikTok) по стримам{who}.
В приложенном файле — весь стрим блоками по {block} секунд. В каждой строке: [время начала блока], полоска громкости
(▁ тихо … █ громче всего на этом стриме) и пометки «тишина / тихо / громко / очень громко / ВСПЛЕСК» (резкий крик,
смех, удар), активность чата (сообщений за блок; «↑» — чат взорвался; «смех×N» — в чате смеются; «пишут: …» — что
пишут в чате, ×N — сколько раз повторили), затем расшифровка речи (автоматическая, с ошибками); ‹ЧЧ:ММ:СС› перед
фразой — точное время её начала.

Отбери ЛУЧШИЕ самостоятельные моменты для шортсов — не больше {max_moments} (около {target}, если стрим короткий).
Самое ценное — по-настоящему смешное: панчлайны, абсурд, тупые и пошлые донаты с реакцией стримера, когда чат
подкалывает стримера и он отвечает, неловкость. Ещё подходит: неожиданное, конфликт, сильная эмоция, мощная фраза,
история с развязкой, «вау»-момент. Громкость и чат — подсказки, а не правило: хорошие моменты бывают и тихими
(история, откровение, панчлайн), а громкий шум без смысла — не момент.
Грубый и пошлый юмор стримера, зрителей и донатов тоже подходит, если это смешно, — не пропускай такие моменты.
Но в title, hook и why пиши без мата и без откровенных слов: нейтрально или намёком
(например «неожиданный донат про мотивацию», «чат придумал Лёве новое прозвище»).
Лучше всего заходят моменты 20–40 секунд; длиннее (до {max_len} с) — только если без этого история не понятна.
Минимум {min_len} с.

Требования:
- Момент понятен без контекста: начинается с завязки или сразу с самой цепляющей фразы, заканчивается развязкой
  или панчлайном, не обрывается на полуслове.
- start — ровно время ‹…› первой фразы завязки. Не захватывай перед ней чужой донат, другую тему или паузу.
  Если стример читает донат — момент начинается с первой фразы ЭТОГО доната.
- end — сразу после реакции на панчлайн (смех, ответ стримера); не растягивай момент ради длины.
- Моменты не пересекаются.
- title — заголовок шортса по-русски до 70 символов: цепляющий, без лжи, без эмодзи и хэштегов.
- hook — короткая надпись сверху кадра до 40 символов: интрига, без спойлера развязки.
- why — до 60 символов, коротко.
- score — от 1 до 10: насколько это залетит; самые смешные — выше всех. Ставь честно, не всем 8–10.
Ответ — только JSON, без пояснений:
{{"moments":[{{"start":"ЧЧ:ММ:СС","end":"ЧЧ:ММ:СС","title":"…","hook":"…","why":"до 60 символов","score":8}}]}}"""


def hms(t):
    t = int(max(0, t))
    return f'{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}'


def parse_hms(v):
    if isinstance(v, (int, float)):
        return float(v)
    total = 0.0
    for part in str(v).strip().replace(',', '.').split(':'):
        total = total * 60 + float(part)
    return total


def ffmpeg_path():
    return shutil.which('ffmpeg') or 'ffmpeg'


def loudness(video, ffmpeg=None):
    """Громкость по секундам, дБ (RMS)."""
    import numpy as np
    p = subprocess.Popen([ffmpeg or ffmpeg_path(), '-v', 'error', '-i', str(video), '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'],
                         stdout=subprocess.PIPE, creationflags=NO_WINDOW)
    out = []
    try:
        while True:
            buf = p.stdout.read(16000)
            if not buf:
                break
            x = np.frombuffer(buf[:len(buf) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768
            rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            out.append(20 * math.log10(max(rms, 1e-5)))
    finally:
        p.stdout.close()
        p.wait()
    if len(out) < 60:
        raise RuntimeError('не удалось прочитать звук записи')
    return out


def transcript(work):
    """Все сегменты расшифровки стрима (самая полная папка в _work/transcripts)."""
    base = Path(work) / 'transcripts'
    dirs = [d for d in base.iterdir() if d.is_dir()] if base.exists() else []
    if not dirs:
        return []
    best = max(dirs, key=lambda d: len(list(d.glob('*.json'))))
    segs = []
    for f in sorted(best.glob('*.json')):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        for s in data.get('segments') or []:
            try:
                a, b, t = float(s['start']), float(s['end']), str(s.get('text') or '').strip()
            except (KeyError, TypeError, ValueError):
                continue
            if t and b >= a:
                segs.append({'start': a, 'end': b, 'text': t})
    segs.sort(key=lambda s: s['start'])
    out = []
    for s in segs:
        if out and s['text'] == out[-1]['text'] and s['start'] < out[-1]['end']:
            continue
        out.append(s)
    return out


def chat_blocks(work, n):
    counts, laughs = [0] * n, [0] * n
    msgs = [collections.Counter() for _ in range(n)]
    path = Path(work) / 'chat.json'
    if not path.is_file():
        return counts, laughs, False, msgs
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return counts, laughs, False, msgs
    for c in data.get('comments') or []:
        try:
            i = int(float(c.get('content_offset_seconds')) // BLOCK)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n:
            counts[i] += 1
            body = str((c.get('message') or {}).get('body') or '')
            if LAUGH.search(body):
                laughs[i] += 1
            text = re.sub(r'\s+', ' ', body).strip()
            if 2 <= len(text) <= 60 and re.search(r'[A-Za-zА-Яа-яЁё]', text):
                msgs[i][text.lower()] += 1
    return counts, laughs, True, msgs


def chat_sample(counter, k=3):
    """Что пишет чат в блоке: сначала повторяющиеся сообщения, затем короткие характерные."""
    rep = [f'{t}×{c}' if c > 1 else t for t, c in counter.most_common(k) if c > 1]
    if len(rep) < k:
        rest = sorted((t for t, c in counter.items() if c == 1), key=lambda t: (-len(t.split()) if len(t) < 45 else 0, t))
        rep += rest[:k - len(rep)]
    return '; '.join(f'«{t}»' for t in rep)


def build_document(work, login='', title='', ffmpeg=None):
    """Возвращает (текст документа, длительность стрима в секундах)."""
    work = Path(work)
    db = loudness(work / 'full-160p.mp4', ffmpeg)
    duration = len(db)
    n = math.ceil(duration / BLOCK)
    mean, peak = [], []
    for i in range(n):
        seg = sorted(db[i * BLOCK:(i + 1) * BLOCK]) or [-100.0]
        mean.append(statistics.fmean(seg[len(seg) // 2:]))  # громкость речи/звука блока, без пауз
        peak.append(seg[-1])
    order = sorted(mean)
    q = lambda p: order[min(len(order) - 1, int(p * (len(order) - 1)))]
    p8, p80 = q(0.08), q(0.8)
    counts, laughs, has_chat, msgs = chat_blocks(work, n)
    active = [c for c in counts if c > 0]
    chat_med = statistics.median(active) if active else 0
    texts = [''] * n
    for s in transcript(work):  # у каждой фразы — своё время начала: по нему ChatGPT ставит точное начало момента
        i = int(s['start'] // BLOCK)
        if 0 <= i < n:
            texts[i] = (texts[i] + f" ‹{hms(s['start'])}›" + s['text']).strip()
    head = [f'СТРИМ{(" " + login) if login else ""}: {title}'.strip(), f'Длительность: {hms(duration)}. Блоки по {BLOCK} с.',
            'Формат строки: [начало блока] громкость пометки | чат (сообщений, что пишут) | речь; ‹ЧЧ:ММ:СС› перед фразой — точное время её начала', '']
    lines = []
    for i in range(n):
        rank = bisect.bisect_left(order, mean[i]) / max(1, len(order) - 1)
        marks = []
        if mean[i] <= p8:
            marks.append('тишина')
        elif rank < 0.25:
            marks.append('тихо')
        elif rank >= 0.95:
            marks.append('очень громко')
        elif rank >= 0.8:
            marks.append('громко')
        if peak[i] - mean[i] >= 9 and peak[i] >= p80 + 3:
            marks.append('ВСПЛЕСК')
        chat = ''
        if has_chat:
            chat = f'чат {counts[i]}'
            if chat_med and counts[i] >= 2.5 * chat_med and counts[i] >= 5:
                chat += ' ↑'
            if laughs[i] >= 3:
                chat += f' смех×{laughs[i]}'
            sample = chat_sample(msgs[i])
            if sample:
                chat += f' пишут: {sample}'
        bar = BARS[min(7, int(rank * 8))]
        lines.append(f'[{hms(i * BLOCK)}] {bar} {" ".join(marks)} | {chat} | {texts[i][:900] or "—"}'.replace('  ', ' '))
    return '\n'.join(head + lines), duration


def _json_block(text):
    """JSON из ответа ChatGPT. Если ответ оборвался посередине — спасаем все целые объекты моментов."""
    text = str(text or '')
    a, b = text.find('{'), text.rfind('}')
    if a >= 0 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except ValueError:
            pass
    items = []
    for m in re.finditer(r'\{[^{}]*"start"[^{}]*\}', text):
        try:
            items.append(json.loads(m.group(0)))
        except ValueError:
            continue
    return {'moments': items} if items else None


def parse_moments(text, duration):
    data = _json_block(text)
    raw = data.get('moments') if isinstance(data, dict) else None
    out = []
    for m in raw or []:
        try:
            a, b = parse_hms(m.get('start')), parse_hms(m.get('end'))
        except (TypeError, ValueError, AttributeError):
            continue
        a, b = max(0.0, a), min(float(duration), b)
        if b - a > MAX_LEN:
            b = a + MAX_LEN
        if b - a < MIN_LEN:
            continue
        title = re.sub(r'\s+', ' ', str(m.get('title') or '')).strip()[:90]
        if not title:
            continue
        try:
            score = max(1, min(10, int(float(m.get('score') or 5))))
        except (TypeError, ValueError):
            score = 5
        out.append({'start': a, 'end': b, 'title': title, 'hook': re.sub(r'\s+', ' ', str(m.get('hook') or '')).strip()[:50],
                    'why': str(m.get('why') or '').strip()[:120], 'score': score})
    kept = []
    for m in sorted(out, key=lambda m: (-m['score'], m['start'])):  # без пересечений: оставляем более сильный
        if all(min(m['end'], k['end']) - max(m['start'], k['start']) < 0.3 * (m['end'] - m['start']) for k in kept):
            kept.append(m)
    return kept


def snap(m, segs):
    """Начало и конец — по границам фраз расшифровки (не на полуслове)."""
    near = [x for x in segs if abs(x['start'] - m['start']) <= 2.5]
    if near:
        m['start'] = max(0.0, min(near, key=lambda x: abs(x['start'] - m['start']))['start'] - 0.15)
    near = [x for x in segs if abs(x['end'] - m['end']) <= 2.5 and x['end'] > m['start'] + MIN_LEN - 1]
    if near:
        m['end'] = min(near, key=lambda x: abs(x['end'] - m['end']))['end'] + 0.6
    return m


def extract(work, vod_id, login, finder, video_title='', log=print, timeout=1500):
    """Карта громкости + чат + транскрипт → ChatGPT → моменты. Результат кэшируется в папке стрима (MARKER)."""
    work = Path(work)
    folder = work.parent
    log('Шортсы: собираю карту громкости, чат и расшифровку')
    doc, duration = build_document(work, login, video_title)
    target = max(10, min(60, int(duration / 60 / 3)))
    prompt = PROMPT.format(who=f' стримера {login}' if login else '', block=BLOCK, min_len=MIN_LEN, max_len=MAX_LEN,
                           target=min(target, MAX_MOMENTS), max_moments=MAX_MOMENTS)
    log('Шортсы: отправляю весь стрим в ChatGPT за моментами')
    text = finder._ask_document(prompt, doc, 'stream.txt', timeout, log=log,
                                predicate=lambda t: len(parse_moments(t, duration)) >= 3)
    moments = parse_moments(text, duration)[:MAX_MOMENTS]  # уже отсортированы по оценке: оставляем лучшие
    segs = transcript(work)
    for m in moments:
        snap(m, segs)
        m['segments'] = [s for s in segs if s['end'] > m['start'] and s['start'] < m['end']]
    data = {'vod_id': str(vod_id), 'login': login, 'created': time.time(), 'duration': duration, 'moments': moments}
    tmp = folder / (MARKER + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(folder / MARKER)
    log(f'Шортсы: ChatGPT нашёл моментов — {len(moments)}')
    return moments


# ---------- рендер вертикального ролика ----------
def _ass_text(s):
    return re.sub(r'[{}\\]', '', s)


def _font_name():
    try:
        from PIL import ImageFont
        return ImageFont.truetype(str(FONT_FILE), 20).getname()[0]
    except Exception:  # noqa: BLE001
        return 'Arial'


def _ts(t):
    t = max(0.0, t)
    return f'{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}'


def write_ass(path, moment, t0, duration):
    font = _font_name()
    lines = ['[Script Info]', 'ScriptType: v4.00+', 'PlayResX: 1080', 'PlayResY: 1920', 'ScaledBorderAndShadow: yes', 'WrapStyle: 0', '',
             '[V4+ Styles]',
             'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, '
             'ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
             f'Style: Cap,{font},86,&H0000D7FF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,7,3,2,70,70,500,1',
             f'Style: Hook,{font},60,&H00FFFFFF,&H00FFFFFF,&H2A0B0B0B,&H00000000,0,0,0,0,100,100,0,0,3,18,0,8,90,90,210,1',
             '', '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    if moment.get('hook'):
        lines.append(f'Dialogue: 1,{_ts(0)},{_ts(duration)},Hook,,0,0,0,,{_ass_text(moment["hook"].upper())}')
    for s in moment.get('segments') or []:
        a, b = max(s['start'], moment['start']) - t0, min(s['end'], moment['end'] + 0.3) - t0
        words = [w for w in re.sub(r'\s+', ' ', s['text']).split(' ') if w]
        if b - a < 0.3 or not words:
            continue
        # группы до 3 слов (короче — на знаке препинания), время делится пропорционально длине слов
        groups, cur = [], []
        for w in words:
            cur.append(w)
            if len(cur) == 3 or re.search(r'[.,!?…:;]$', w):
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        total = sum(len(w) + 1 for w in words)
        t = a
        for g in groups:
            gl = sum(len(w) + 1 for w in g)
            d = (b - a) * gl / total
            parts = []
            for w in g:
                cs = max(1, int(round(100 * d * (len(w) + 1) / gl)))
                parts.append('{\\k%d}%s' % (cs, _ass_text(w.strip('.,;:').upper())))
            lines.append(f'Dialogue: 0,{_ts(t)},{_ts(t + d)},Cap,,0,0,0,,{" ".join(parts)}')
            t += d
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')


def render(moment, vod_id, out_path, cli, quality='1080p60', threads=4, progress=print, ffmpeg=None):
    """Скачивает отрезок с Twitch и собирает вертикальное видео 1080x1920 с надписью и субтитрами."""
    import pipeline
    ffmpeg = ffmpeg or ffmpeg_path()
    out_path = Path(out_path)
    tmp = out_path.parent / (out_path.stem + '_work')
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        a, b = max(0.0, moment['start'] - 0.3), moment['end'] + 0.4
        progress('скачиваю отрезок стрима с Twitch')
        pipeline.default_download(vod_id, quality, tmp / 'src.mp4', a, b, cli, threads)
        dur = pipeline.media_duration(tmp / 'src.mp4')
        write_ass(tmp / 'subs.ass', moment, a, dur)
        (tmp / 'fonts').mkdir()
        if FONT_FILE.is_file():
            shutil.copy(FONT_FILE, tmp / 'fonts' / FONT_FILE.name)
        vf = ('[0:v]split[s1][s2];'
              '[s1]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28,eq=brightness=-0.12:saturation=1.15[bg];'
              '[s2]scale=1400:-2,crop=1080:ih[fg];'
              '[bg][fg]overlay=(W-w)/2:(H-h)/2-110,subtitles=subs.ass:fontsdir=fonts,format=yuv420p[v]')
        progress('собираю вертикальное видео')
        cmd = [ffmpeg, '-y', '-v', 'error', '-i', 'src.mp4', '-filter_complex', vf, '-map', '[v]', '-map', '0:a?',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-r', '30', '-c:a', 'aac', '-b:a', '160k',
               '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11', '-movflags', '+faststart', 'out.mp4']
        p = subprocess.run(cmd, cwd=str(tmp), capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
        if p.returncode != 0 or not (tmp / 'out.mp4').is_file():
            raise RuntimeError('ffmpeg не собрал шортс: ' + (p.stderr or '').strip()[-300:])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp / 'out.mp4'), str(out_path))
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pending_streams(root, logins):
    """Нарезанные стримы этих стримеров, для которых моменты ещё не размечены (есть _work с расшифровкой, нет MARKER)."""
    import sqlite3
    root = Path(root)
    db = sqlite3.connect(root / 'state' / 'jobs.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    try:
        jobs = [dict(r) for r in db.execute("SELECT vod_id,streamer,title,updated_at FROM jobs WHERE state='done' ORDER BY updated_at DESC")]
    finally:
        db.close()
    out = []
    for j in jobs:
        login = str(j['streamer'] or '').lower()
        if login not in logins:
            continue
        for d in (root / 'recordings').glob(f"{j['streamer']} VOD {j['vod_id']}"):
            if (d / MARKER).exists() or not (d / '_work' / 'full-160p.mp4').is_file() or not (d / '_work' / 'transcripts').is_dir():
                continue
            out.append({'login': login, 'vod_id': str(j['vod_id']), 'title': j['title'] or '', 'work': d / '_work', 'folder': d})
    return out
