"""Статистика YouTube-каналов для вкладки «Каналы»: просмотры обычных нарезок, Shorts и сумма.

Читает только публичные страницы YouTube (без входа и без Chrome-профилей бота), поэтому никак
не мешает загрузкам и публикации. Видны только уже опубликованные (публичные) ролики."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36',
           'Accept-Language': 'ru-RU,ru;q=0.9'}
FALLBACK_KEY = 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8'
FALLBACK_VERSION = '2.20261112.10.00'
MAX_PAGES = 25      # страниц списка на вкладку (примерно по 30 роликов)
HISTORY = 200       # сколько прошлых замеров хранить для прироста
STALE_HOURS = 6     # при запуске окна обновить, если данные старше
_VID = re.compile(r'^[\w-]{11}$')


class StatsError(RuntimeError):
    pass


def _walk(node, key):
    """Все значения под ключом key на любой глубине."""
    stack = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if k == key:
                    yield v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)


def parse_count(text):
    """«920 просмотров», «1,2 тыс. просмотров», «71 подписчик», «Нет просмотров» → число или None."""
    t = str(text or '').lower().replace('\xa0', ' ').replace('\u202f', ' ')
    if not t.strip():
        return None
    if re.search(r'(^|[\s,])нет просмотр', t):
        return 0
    m = None
    for m in re.finditer(r'(\d[\d ]*(?:[.,]\d+)?)\s*(тысяч[аи]?|тыс|миллиард[аов]*|миллион[аов]*|млрд|млн|k|m|b)?\.?\s*(?:просмотр|подписчик|views?|subscribers?)', t):
        pass
    if m is None:
        return None
    try:
        num = float(m.group(1).replace(' ', '').replace(',', '.'))
    except ValueError:
        return None
    unit = (m.group(2) or '')
    unit = 'тыс' if unit.startswith('тыс') else 'млрд' if unit.startswith('миллиард') else 'млн' if unit.startswith('миллион') else unit
    mult = {'тыс': 1e3, 'k': 1e3, 'млн': 1e6, 'm': 1e6, 'млрд': 1e9, 'b': 1e9}.get(unit, 1)
    return int(round(num * mult))


def _initial(html):
    if 'consent.youtube.com' in html[:2000]:
        raise StatsError('YouTube показал страницу согласия с cookies (consent) вместо канала')
    m = re.search(r'(?:var ytInitialData|window\["ytInitialData"\])\s*=\s*(\{.*?\});\s*</script>', html, re.S)
    if not m:
        raise StatsError('YouTube вернул страницу без данных канала')
    return json.loads(m.group(1))


def _views_label(node):
    for key in ('accessibilityLabel', 'content', 'simpleText'):
        for label in _walk(node, key):
            if isinstance(label, str) and re.search(r'просмотр|views?\b', label, re.I):
                return parse_count(label)
    return None


def _items(data, kind):
    """[(video_id, название, просмотры из списка)] со страницы или продолжения списка."""
    out = []
    if kind == 'clip':
        for v in _walk(data, 'lockupViewModel'):
            if not isinstance(v, dict) or v.get('contentType') not in (None, 'LOCKUP_CONTENT_TYPE_VIDEO'):
                continue
            meta = (v.get('metadata') or {}).get('lockupMetadataViewModel') or {}
            out.append((v.get('contentId'), ((meta.get('title') or {}).get('content')) or '', _views_label(meta)))
        for v in _walk(data, 'videoRenderer'):  # старая разметка
            if isinstance(v, dict):
                runs = (v.get('title') or {}).get('runs') or [{}]
                out.append((v.get('videoId'), runs[0].get('text', ''), parse_count((v.get('viewCountText') or {}).get('simpleText'))))
    else:
        for v in _walk(data, 'shortsLockupViewModel'):
            if not isinstance(v, dict):
                continue
            vid = next((x for x in _walk(v.get('onTap') or {}, 'videoId') if isinstance(x, str)), None)
            if not vid and str(v.get('entityId', '')).startswith('shorts-shelf-item-'):
                vid = str(v['entityId'])[len('shorts-shelf-item-'):]
            acc = str(v.get('accessibilityText') or '')
            over = v.get('overlayMetadata') or {}
            title = ((over.get('primaryText') or {}).get('content')) or (re.search(r'"(.*)"', acc).group(1).rsplit(',', 1)[0] if '"' in acc else '')
            views = parse_count(acc)
            if views is None:
                views = parse_count(((over.get('secondaryText') or {}).get('content') or '') + ' просмотров')
            out.append((vid, title, views))
        for v in _walk(data, 'reelItemRenderer'):  # старая разметка
            if isinstance(v, dict):
                out.append((v.get('videoId'), (v.get('headline') or {}).get('simpleText', ''), parse_count((v.get('viewCountText') or {}).get('simpleText'))))
    return [x for x in out if isinstance(x[0], str) and _VID.match(x[0])]


def _tokens(data):
    """Маркеры продолжения списка. На странице их несколько (часть — не от сетки роликов), пробуем по очереди."""
    out = []
    for cir in _walk(data, 'continuationItemRenderer'):
        for tok in _walk(cir, 'token'):
            if isinstance(tok, str) and tok and tok not in out:
                out.append(tok)
    return out


_local = threading.local()


def _session():
    s = getattr(_local, 'session', None)
    if s is None:
        s = _local.session = requests.Session()
        s.headers.update(HEADERS)
        # Без этих cookies YouTube (ЕС и др.) перенаправляет на consent.youtube.com — страницу
        # согласия без ytInitialData, и статистика падает с «страницей без данных канала».
        for name, value in (('SOCS', 'CAI'), ('CONSENT', 'YES+cb')):
            s.cookies.set(name, value, domain='.youtube.com', path='/')
    return s


def _request(method, url, **kw):
    # Короткий таймаут соединения и один повтор: иногда соединение с YouTube «висит» ~20 с.
    for attempt in range(2):
        try:
            r = _session().request(method, url, timeout=(5, 30), **kw)
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout):
            if attempt:
                raise


class _Client:
    def __init__(self):
        self.key, self.version = FALLBACK_KEY, FALLBACK_VERSION

    def page(self, url):
        html = _request('GET', url, params={'hl': 'ru', 'gl': 'RU'}).text
        m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        if m:
            self.key = m.group(1)
        m = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        if m:
            self.version = m.group(1)
        return html

    def api(self, endpoint, body):
        body = dict(body, context={'client': {'clientName': 'WEB', 'clientVersion': self.version, 'hl': 'ru', 'gl': 'RU'}})
        return _request('POST', f'https://www.youtube.com/youtubei/v1/{endpoint}', params={'key': self.key, 'prettyPrint': 'false'},
                        json=body).json()

    def tab(self, channel_id, tab, kind):
        html = self.page(f'https://www.youtube.com/channel/{channel_id}/{tab}')
        data = _initial(html)
        found = {}
        def add(items):
            for vid, title, views in items:
                found.setdefault(vid, {'id': vid, 'title': title or '', 'list_views': views})
        add(_items(data, kind))
        first, tokens, tried, pages = data, _tokens(data), set(), 1
        while tokens and pages < MAX_PAGES:
            nxt = None
            for tok in [t for t in tokens if t not in tried][:5]:
                tried.add(tok)
                part = self.api('browse', {'continuation': tok})
                before = len(found)
                add(_items(part, kind))
                if len(found) > before:
                    nxt = _tokens(part)
                    break
            if nxt is None:  # ни одно продолжение не дало новых роликов — список закончился
                break
            tokens, pages = nxt, pages + 1
        return list(found.values()), html, first

    def player(self, vid):
        try:
            j = self.api('player', {'videoId': vid})
        except Exception:  # noqa: BLE001 — тогда возьмём просмотры из списка
            return None
        vd = j.get('videoDetails') or {}
        mf = (j.get('microformat') or {}).get('playerMicroformatRenderer') or {}
        views, length = str(vd.get('viewCount', '')), str(vd.get('lengthSeconds', ''))
        return {'views': int(views) if views.isdigit() else None, 'length': int(length) if length.isdigit() else None,
                'published': mf.get('publishDate') or mf.get('uploadDate') or '', 'title': vd.get('title') or ''}


def collect_channel(client, profile, channel_id, progress=None):
    say = progress or (lambda text: None)
    say(f'{profile}: список видео')
    clips, html, first = client.tab(channel_id, 'videos', 'clip')
    say(f'{profile}: список Shorts')
    shorts, _, _ = client.tab(channel_id, 'shorts', 'short')
    short_ids = {v['id'] for v in shorts}
    videos = [dict(v, kind='clip') for v in clips if v['id'] not in short_ids] + [dict(v, kind='short') for v in shorts]
    say(f'{profile}: просмотры {len(videos)} видео')
    with ThreadPoolExecutor(6) as ex:
        details = list(ex.map(lambda v: client.player(v['id']), videos))
    out = []
    for v, d in zip(videos, details):
        d = d or {}
        views = d.get('views')
        out.append({'id': v['id'], 'kind': v['kind'], 'title': d.get('title') or v.get('title') or '',
                    'views': views if views is not None else v.get('list_views'), 'exact': views is not None,
                    'published': d.get('published') or '', 'length': d.get('length')})
    m = re.search(r'"content":"([^"]{0,40}подписчик[^"]{0,15})"', html)
    subs_text = m.group(1) if m else ''
    meta = next((x for x in _walk(first, 'channelMetadataRenderer') if isinstance(x, dict)), {})
    return {'profile': profile, 'channel_id': channel_id, 'title': meta.get('title') or '', 'subs': parse_count(subs_text) if subs_text else None,
            'subs_text': subs_text, 'videos': out, 'error': ''}


def totals(ch):
    t = {'clip': [0, 0], 'short': [0, 0]}
    for v in ch.get('videos') or []:
        k = 'short' if v.get('kind') == 'short' else 'clip'
        t[k][0] += 1
        t[k][1] += int(v.get('views') or 0)
    return {'clips': t['clip'][0], 'clip_views': t['clip'][1], 'shorts': t['short'][0], 'short_views': t['short'][1],
            'views': t['clip'][1] + t['short'][1]}


def growth(data, profile):
    """(прирост просмотров, время прошлого замера): с замером примерно суткой ранее, иначе с самым старым."""
    hist = [h for h in data.get('history') or [] if isinstance(h, dict) and profile in (h.get('p') or {})]
    if len(hist) < 2:
        return None
    cur, base = hist[-1], None
    for h in hist[:-1]:
        if h['t'] <= cur['t'] - 86400:
            base = h
    base = base or hist[0]
    return cur['p'][profile]['views'] - base['p'][profile]['views'], base['t']


class ChannelStats:
    """Хранит последний замер в state/channel_stats.json и обновляет его в фоновом потоке."""

    def __init__(self, root):
        self.path = Path(root) / 'state' / 'channel_stats.json'
        self.data = self._load()
        self.running = False
        self.progress = ''
        self.error = ''
        self.version = 0
        self._lock = threading.Lock()

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def stale(self, hours=STALE_HOURS):
        return time.time() - float(self.data.get('updated') or 0) > hours * 3600

    def start(self, channels, on_done=None):
        with self._lock:
            if self.running:
                return False
            self.running, self.error, self.progress = True, '', 'запуск…'
        threading.Thread(target=self._run, args=(list(channels), on_done), name='channel-stats', daemon=True).start()
        return True

    def _say(self, text):
        self.progress = text

    def _run(self, channels, on_done):
        try:
            client = _Client()
            old = {c.get('profile'): c for c in self.data.get('channels') or [] if isinstance(c, dict)}
            result, failed = [], []
            for c in channels:
                p = c['profile']
                try:
                    result.append(collect_channel(client, p, c['channel_id'], self._say))
                except Exception as exc:  # noqa: BLE001 — один канал не должен ломать остальные
                    failed.append(p)
                    prev = dict(old.get(p) or {'profile': p, 'channel_id': c['channel_id'], 'videos': []})
                    prev['error'] = f'{type(exc).__name__}: {str(exc)[:160]}'
                    result.append(prev)
            now = time.time()
            history = [h for h in self.data.get('history') or [] if isinstance(h, dict)]
            history.append({'t': now, 'p': {c['profile']: totals(c) for c in result if not c.get('error')}})
            data = {'updated': now, 'channels': result, 'history': history[-HISTORY:]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + '.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, self.path)
            self.data = data
            self.error = ('не удалось обновить: ' + ', '.join(failed)) if failed else ''
        except Exception as exc:  # noqa: BLE001
            self.error = f'{type(exc).__name__}: {str(exc)[:200]}'
        finally:
            self.progress = ''
            self.running = False
            self.version += 1
            if on_done:
                try:
                    on_done()
                except Exception:  # noqa: BLE001
                    pass
