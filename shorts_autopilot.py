"""YouTube Shorts autopilot: per-channel queue + interval; videos are made by ShortsFactory.

Rules:
* each linked channel has its own queue, interval (1-24 h) and mode; posts/facts are added by hand,
  and when the queue is empty the bot makes a science fact itself (mode «mixed»/«auto»);
* nothing is generated or uploaded while a stream is being cut (heavy GPU/CPU work);
  if cutting starts in the middle, generation is stopped and the item returns to the queue;
* only one Short is processed at a time; uploads share the YouTube Studio browser lock.
"""
from __future__ import annotations
import json
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import youtube_studio as ys
import stream_shorts as stsh
import lang as lang_mod
import upload_limits

DEFAULT_FACTORY = str(Path.home() / 'Desktop' / 'ShortsFactory')
MODES = [('stream', 'Моменты со стримов'), ('mixed', 'Мои посты, потом сам'), ('auto', 'Только сам: научные факты'), ('queue', 'Только мои посты и факты')]
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
QUEUE_LIMIT = 50  # max queued Shorts per streamer; extra ones from the oldest streams are deleted
FINISH_TIMEOUT = 1800  # max wait for YouTube to finish uploading/processing a Short before leaving Chrome open


class StreamBusy(Exception):
    """A stream started being cut: postpone the Short."""


class Deferred(Exception):
    """Not now (browser busy, etc.); try again on the next tick."""


class ShortsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channels (
              profile TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
              interval_hours INTEGER NOT NULL DEFAULT 3 CHECK(interval_hours BETWEEN 1 AND 24),
              mode TEXT NOT NULL DEFAULT 'queue', telegram TEXT NOT NULL DEFAULT '',
              next_due REAL NOT NULL DEFAULT 0, last_upload REAL NOT NULL DEFAULT 0,
              tg_fetched_at REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY, profile TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'queued', source_key TEXT, slug TEXT NOT NULL DEFAULT '',
              video_path TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
              video_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(profile, source_key));
            CREATE INDEX IF NOT EXISTS ix_items_queue ON items(profile, status, id);
            CREATE TABLE IF NOT EXISTS topics (profile TEXT NOT NULL, topic TEXT NOT NULL, created_at REAL NOT NULL);
            ''')
            # Telegram import was removed: channels that only used the queue now fall back to
            # self-made science facts when the queue is empty. Done once, user choices are kept after.
            if not db.execute("SELECT 1 FROM settings WHERE key='no_telegram_v1'").fetchone():
                db.execute("UPDATE channels SET mode='mixed' WHERE mode='queue'")
                db.execute("UPDATE channels SET note='' WHERE note LIKE 'Telegram%'")
                db.execute("INSERT INTO settings(key,value) VALUES('no_telegram_v1','1')")
            # Шортсы из моментов стримов заменили научные факты на каналах стримеров (тестовый канал не трогаем).
            if not db.execute("SELECT 1 FROM settings WHERE key='stream_moments_v1'").fetchone():
                db.execute("UPDATE channels SET mode='stream' WHERE profile!='__test__'")
                db.execute("INSERT INTO settings(key,value) VALUES('stream_moments_v1','1')")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    # settings
    def get(self, key, default=''):
        with self._db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row['value'] if row else default

    def set(self, key, value):
        with self._db() as db:
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    # channels
    def channel(self, profile):
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO channels(profile,mode,updated_at) VALUES(?,'stream',?)", (profile, time.time()))
            return dict(db.execute('SELECT * FROM channels WHERE profile=?', (profile,)).fetchone())

    def channels(self):
        with self._db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM channels ORDER BY profile')]

    def save_channel(self, profile, **fields):
        allowed = {'enabled', 'interval_hours', 'mode', 'telegram', 'next_due', 'last_upload', 'tg_fetched_at', 'note'}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields.get('note'):  # время сообщения: видно, что оно старое, а не свежая ошибка
            fields['note'] = time.strftime('%d.%m %H:%M') + ' — ' + str(fields['note'])
        if 'interval_hours' in fields:
            fields['interval_hours'] = max(1, min(24, int(fields['interval_hours'])))
        if 'mode' in fields and fields['mode'] not in dict(MODES):
            raise ValueError('Неизвестный режим')
        self.channel(profile)
        if fields:
            sets = ','.join(f'{k}=?' for k in fields)
            with self._db() as db:
                db.execute(f'UPDATE channels SET {sets},updated_at=? WHERE profile=?', (*fields.values(), time.time(), profile))
        return self.channel(profile)

    # items
    def add_item(self, profile, kind, text, source_key=None):
        now = time.time()
        with self._db() as db:
            cur = db.execute('INSERT OR IGNORE INTO items(profile,kind,text,source_key,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                             (profile, kind, text.strip(), source_key, now, now))
            return cur.rowcount > 0

    def items(self, profile=None, limit=100):
        with self._db() as db:
            q = 'SELECT * FROM items' + (' WHERE profile=?' if profile else '') + \
                " ORDER BY CASE status WHEN 'working' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, CASE WHEN status='queued' THEN id ELSE -updated_at END LIMIT ?"
            return [dict(r) for r in db.execute(q, ((profile, limit) if profile else (limit,)))]

    def counts(self):
        with self._db() as db:
            return {(r['profile'], r['status']): r['n'] for r in db.execute('SELECT profile,status,COUNT(*) n FROM items GROUP BY profile,status')}

    def next_item(self, profile, kinds):
        marks = ','.join('?' * len(kinds))
        with self._db() as db:
            row = db.execute(f"SELECT * FROM items WHERE profile=? AND status='queued' AND kind IN ({marks}) ORDER BY id LIMIT 1", (profile, *kinds)).fetchone()
            return dict(row) if row else None

    def item(self, item_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            return dict(row) if row else None

    def update_item(self, item_id, **fields):
        sets = ','.join(f'{k}=?' for k in fields)
        with self._db() as db:
            db.execute(f'UPDATE items SET {sets},updated_at=? WHERE id=?', (*fields.values(), time.time(), item_id))

    def trim_queue(self, limit=QUEUE_LIMIT):
        """Не больше limit шортсов в очереди на стримера. Лишние удаляются из самых старых стримов,
        внутри стрима — сначала самые слабые (моменты стрима добавляются от сильных к слабым)."""
        removed, files = {}, []
        with self._db() as db:
            over = db.execute("SELECT profile, COUNT(*) n FROM items WHERE status='queued' GROUP BY profile HAVING n>?", (limit,)).fetchall()
            for r in over:
                rows = db.execute("SELECT id, source_key, video_path FROM items WHERE profile=? AND status='queued' ORDER BY id", (r['profile'],)).fetchall()
                groups = {}
                for x in rows:  # группа = стрим (moment:<vod>:<start>), остальное — по одному
                    key = str(x['source_key'] or '')
                    g = key.split(':')[1] if key.startswith('moment:') and key.count(':') >= 2 else 'id%d' % x['id']
                    groups.setdefault(g, []).append(x)
                extra, drop = r['n'] - limit, []
                for g in groups.values():  # в порядке появления: старые стримы первыми
                    for x in reversed(g):
                        if len(drop) >= extra:
                            break
                        drop.append(x)
                    if len(drop) >= extra:
                        break
                db.executemany("DELETE FROM items WHERE id=? AND status='queued'", [(x['id'],) for x in drop])
                files += [x['video_path'] for x in drop if x['video_path']]
                removed[r['profile']] = len(drop)
        for f in files:
            try:
                Path(f).unlink()
            except OSError:
                pass
        return removed

    def delete_item(self, item_id):
        with self._db() as db:
            db.execute("DELETE FROM items WHERE id=? AND status!='working'", (item_id,))

    def set_title(self, profile, source_key, title):
        with self._db() as db:
            db.execute('UPDATE items SET title=? WHERE profile=? AND source_key=?', (title, profile, source_key))

    def retry_item(self, item_id):
        self.update_item(item_id, status='queued', detail='')

    def recover(self):
        with self._db() as db:
            db.execute("UPDATE items SET status='queued',detail='Работа прервана закрытием программы',updated_at=? WHERE status='working'", (time.time(),))

    def delete_profile(self, profile):
        """Полное удаление стримера: канал Shorts, вся его очередь/история и темы. Возвращает (кол-во, файлы роликов)."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                files = [r['video_path'] for r in db.execute("SELECT video_path FROM items WHERE profile=? AND video_path!=''", (profile,))]
                n = db.execute('DELETE FROM items WHERE profile=?', (profile,)).rowcount
                db.execute('DELETE FROM topics WHERE profile=?', (profile,))
                db.execute('DELETE FROM channels WHERE profile=?', (profile,))
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                raise
        return n, files

    def topics(self, profile, limit=80):
        with self._db() as db:
            return [r['topic'] for r in db.execute('SELECT topic FROM topics WHERE profile=? ORDER BY created_at DESC LIMIT ?', (profile, limit))][::-1]

    def add_topic(self, profile, topic):
        with self._db() as db:
            db.execute('INSERT INTO topics(profile,topic,created_at) VALUES(?,?,?)', (profile, topic, time.time()))


class ShortsAutopilot:
    def __init__(self, root, service):
        self.root = Path(root)
        self.service = service
        self.store = ShortsStore(self.root / 'state' / 'shorts.sqlite3')
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.proc = None
        self.status = 'Выключено'
        self.current = None  # {'profile','item_id','stage','detail'}
        self._log = []
        self.manual = []  # profiles requested with «Сделать сейчас»; run even when autopublish is off

    # ---------- public API (UI thread) ----------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.store.recover()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name='shorts-autopilot', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        self._kill()

    @property
    def enabled(self):
        return self.store.get('enabled', '0') == '1'

    def set_enabled(self, value):
        self.store.set('enabled', '1' if value else '0')
        self.wake.set()

    @property
    def factory(self):
        return Path(self.store.get('factory_dir', DEFAULT_FACTORY))

    def factory_problem(self):
        f = self.factory
        if not (f / '.venv' / 'Scripts' / 'python.exe').exists():
            return f'Не найден ShortsFactory в {f}'
        if not (f / 'tools' / 'write_script.py').exists():
            return 'В ShortsFactory нет tools\\write_script.py'
        return ''

    def run_now(self, profile):
        self.store.save_channel(profile, note='')
        if profile not in self.manual:
            self.manual.append(profile)
        self.wake.set()

    def busy_with(self, profile):
        cur = self.current
        return bool(cur and str(cur.get('profile') or '').lower() == str(profile).lower())

    def forget_profile(self, profile):
        """Стример удалён полностью: убираем его канал Shorts, очередь, историю и готовые ролики в state/stream_shorts."""
        profile = str(profile).strip().lower()
        if self.busy_with(profile):
            raise ValueError('Сейчас делается Shorts этого стримера — удалите после завершения')
        self.manual = [p for p in self.manual if p != profile]
        n, files = self.store.delete_profile(profile)
        folder = (self.root / 'state' / 'stream_shorts').resolve()
        own = re.compile(re.escape(profile) + r'_\d+\.mp4$', re.I)
        paths = {Path(f) for f in files}
        if folder.is_dir():
            paths |= {p for p in folder.iterdir() if own.fullmatch(p.name)}
        for p in paths:
            try:
                if p.resolve().parent == folder:  # только наши рендеры, чужие файлы не трогаем
                    p.unlink()
            except OSError:
                pass
        self.log(f'{profile}: стример удалён — убраны канал Shorts и {n} записей очереди/истории')
        self.wake.set()
        return n

    def profiles(self):
        return self.service.youtube_queue.list_profiles()

    def snapshot(self):
        counts = self.store.counts()
        stored = {c['profile']: c for c in self.store.channels()}
        rows = []
        for p in self.profiles():
            ch = stored.get(p['profile']) or self.store.channel(p['profile'])
            ch = {**ch, 'channel_id': p['channel_id'], 'audience': p.get('audience', ''),
                  'queued': counts.get((p['profile'], 'queued'), 0), 'done': counts.get((p['profile'], 'done'), 0)}
            rows.append(ch)
        return {'enabled': self.enabled, 'status': self.status, 'current': dict(self.current) if self.current else None,
                'channels': rows, 'items': self.store.items(limit=150), 'factory': str(self.factory),
                'factory_problem': self.factory_problem(), 'log': list(self._log[-30:])}

    # ---------- worker ----------
    def log(self, text):
        self._log.append((time.time(), str(text)))
        del self._log[:-200]
        try:
            self.service.log('Shorts: ' + str(text))
        except Exception:
            pass

    def stream_busy(self):
        """True while a stream recording is being processed (or is about to start)."""
        jobs = self.service.store.list_jobs(1000)
        if any(j.get('state') == 'running' for j in jobs):
            return True
        worker_alive = any(t.is_alive() and t.name == 'clip-worker' for t in getattr(self.service, '_threads', []))
        now = time.time()
        return worker_alive and any(j.get('state') in ('queued', 'retry') and float(j.get('available_at') or 0) <= now
                                    and not j.get('hidden') for j in jobs)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.status = f'Ошибка: {type(exc).__name__}: {exc}'
                self.log(self.status)
            self.wake.wait(15)
            self.wake.clear()

    def _tick(self):
        linked = {p['profile'] for p in self.profiles()}
        # ShortsFactory нужен только каналам с научными фактами; моментам со стримов он не нужен
        if any(c['mode'] != 'stream' for c in self.store.channels() if c['profile'] in linked and c['enabled']):
            problem = self.factory_problem()
            if problem:
                self.status = problem
                return
        if self.manual:  # «Сделать сейчас»: один ролик вне расписания, даже если автопубликация выключена
            profile = self.manual[0]
            if profile not in linked:
                self.manual.pop(0)
                return
            if self.stream_busy():
                self.status = f'{profile}: «Сделать сейчас» ждёт — идёт нарезка стрима'
                return
            self.manual.pop(0)
            self._process(self.store.channel(profile), manual=True)
            return
        if not self.enabled:
            self.status = 'Выключено'
            return
        self._trim_queue()
        channels = [c for c in self.store.channels() if c['enabled'] and c['profile'] in linked]
        now = time.time()
        if not channels:
            self.status = 'Нет включённых каналов'
            return
        if self.stream_busy():
            self.status = 'Пауза: идёт нарезка стрима'
            return
        if self._extract_moments(channels):
            return
        due = [c for c in channels if c['next_due'] <= now]
        if not due:
            nxt = min(channels, key=lambda c: c['next_due'])
            self.status = f"Ждёт расписания: следующий {nxt['profile']} в {time.strftime('%H:%M', time.localtime(nxt['next_due']))}"
            return
        self._process(min(due, key=lambda c: c['next_due']))

    _extract_failed = {}

    def _trim_queue(self):
        try:
            for profile, n in self.store.trim_queue().items():
                self.log(f'{profile}: в очереди шортсов больше {QUEUE_LIMIT} — удалено старых: {n}')
        except Exception as exc:
            self.log(f'Очередь шортсов: не удалось сократить — {type(exc).__name__}: {exc}')

    def _extract_moments(self, channels):
        """Нарезанный стрим стримера с каналом в режиме «Моменты со стримов» → ChatGPT размечает моменты → очередь.
        Один стрим за проход; при ошибке повтор не раньше чем через 2 часа."""
        by_login = {}
        for c in channels:
            if c['mode'] == 'stream':
                by_login.setdefault(c['profile'].lower(), []).append(c['profile'])
        if not by_login:
            return False
        now = time.time()
        todo = [s for s in stsh.pending_streams(self.root, set(by_login)) if now - self._extract_failed.get(s['vod_id'], 0) > 7200]
        if not todo:
            return False
        s = todo[0]
        self.status = f"{s['login']}: ChatGPT размечает стрим на моменты для шортсов"
        try:
            lang_mod.set_current(lang_mod.for_streamer(s['login']))
            moments = stsh.extract(s['work'], s['vod_id'], s['login'], self.service.finder, s['title'], log=self.log)
        except Exception as exc:
            self._extract_failed[s['vod_id']] = now
            self.log(f"{s['login']}: не удалось разметить стрим {s['vod_id']} на моменты — {exc}")
            return True
        for profile in by_login[s['login']]:
            added = 0
            for m in moments:  # уже отсортированы: самые сильные — первыми в очереди
                data = {k: m[k] for k in ('start', 'end', 'title', 'hook', 'why', 'score', 'segments')}
                data.update(vod_id=s['vod_id'], login=s['login'])
                key = f"moment:{s['vod_id']}:{int(m['start'])}"
                if self.store.add_item(profile, 'moment', json.dumps(data, ensure_ascii=False), key):
                    self.store.set_title(profile, key, f"{m['title']} · {stsh.hms(m['start'])} · {m['score']}/10")
                    added += 1
            self.log(f"{profile}: из стрима {s['vod_id']} в очередь шортсов добавлено моментов — {added}")
        self._trim_queue()
        self.wake.set()
        return True

    def _pick(self, ch, manual=False):
        if ch['mode'] == 'stream':
            return self.store.next_item(ch['profile'], ('moment',))
        kinds_queue = ('post', 'fact', 'telegram')
        item = None
        if ch['mode'] in ('queue', 'mixed'):
            item = self.store.next_item(ch['profile'], kinds_queue)
        # «Сделать сейчас» при пустой очереди: делаем ролик на научную тему, а не молчим
        if item is None and (ch['mode'] in ('auto', 'mixed') or manual):
            item = self.store.next_item(ch['profile'], ('auto',))
            if item is None:
                self.store.add_item(ch['profile'], 'auto', '', f'auto:{time.time()}')
                item = self.store.next_item(ch['profile'], ('auto',))
        return item

    def _process(self, ch, manual=False):
        lang_mod.set_current(lang_mod.for_streamer(ch['profile']))
        profile = ch['profile']
        limit = upload_limits.until(profile, 'short')
        if limit:  # дневной лимит канала: ждём конца общей паузы (она же у полных видео)
            self.status = f'{profile}: лимит загрузок канала до {upload_limits.fmt(limit)}'
            self.store.save_channel(profile, next_due=limit + 60, note=f'Лимит загрузок канала до {upload_limits.fmt(limit)}')
            return
        item = self._pick(ch, manual)
        if item is None:
            if ch['mode'] == 'stream':
                self.status = f'{profile}: моменты со стримов кончились — жду следующий стрим'
                self.store.save_channel(profile, next_due=time.time() + 1800, note='Моменты кончились — жду следующий стрим')
            else:
                self.status = f'{profile}: очередь пуста — добавьте пост или факт'
                self.store.save_channel(profile, next_due=time.time() + 600, note='Очередь пуста')
            return
        self.current = {'profile': profile, 'item_id': item['id'], 'stage': 'Подготовка', 'detail': ''}
        self.store.update_item(item['id'], status='working', detail='')
        try:
            if not item['video_path'] or not Path(item['video_path']).exists():
                item = self._generate(ch, item)
            self._upload(ch, item)
        except StreamBusy:
            self.store.update_item(item['id'], status='queued', detail='Отложено: началась нарезка стрима')
            self.log(f'{profile}: отложено — началась нарезка стрима')
        except Deferred as exc:
            self.store.update_item(item['id'], status='queued', detail=str(exc))
            self.status = str(exc)
            if not self.stop_event.is_set():
                self.store.save_channel(profile, next_due=time.time() + 300, note=str(exc)[:200])
        except ys.UploadLimitReached:
            until = upload_limits.hit(profile, 'short')
            self.store.update_item(item['id'], status='queued', detail=f'Дневной лимит загрузок канала; повтор после {upload_limits.fmt(until)}')
            self.store.save_channel(profile, next_due=until + 60, note=f'Лимит загрузок канала до {upload_limits.fmt(until)}')
            self._notify(f'Shorts {profile}: дневной лимит загрузок канала — пауза Shorts и нарезок до {upload_limits.fmt(until)}; '
                         f'после неё всё накопившееся выложится само. Удалите в Studio черновик, который YouTube не загрузил')
        except ys.VerificationRequired as exc:
            self.store.update_item(item['id'], status='queued', detail=str(exc)[:300])
            self.store.save_channel(profile, enabled=0, note='Нужна проверка канала в YouTube')
            self._notify(f'Shorts {profile}: канал выключен — {exc}')
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'[:400]
            self.store.update_item(item['id'], status='failed', detail=detail)
            self.store.save_channel(profile, next_due=time.time() + 1800, note=f'Ошибка: {detail[:150]}; следующая попытка через 30 мин')
            self._notify(f'Shorts {profile}: ошибка — {detail}')
        finally:
            self.current = None

    def _notify(self, text):
        self.log(text)
        try:
            self.service.store.notify(text)
        except Exception:
            pass

    def _stage(self, stage, detail=''):
        if self.current is not None:
            self.current.update(stage=stage, detail=str(detail)[:200])
        self.status = f"{self.current['profile'] if self.current else ''}: {stage}" + (f' — {detail}' if detail else '')

    # ---------- generation via ShortsFactory ----------
    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True, creationflags=NO_WINDOW)

    def _run(self, args, stage):
        fac = self.factory
        env = {**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8', 'HF_HUB_DISABLE_SYMLINKS_WARNING': '1'}
        self.proc = subprocess.Popen(args, cwd=fac, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
        lines = []

        def reader():
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    lines.append(line)
                    f = re.match(r'\[кадры\] (\d+)/(\d+)', line)
                    self._stage(stage, f'кадры {f[1]} из {f[2]}' if f else line[:120])

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        while self.proc.poll() is None:
            if self.stop_event.is_set():
                self._kill()
                raise Deferred('Остановлено')
            if self.stream_busy():
                self._kill()
                raise StreamBusy()
            time.sleep(2)
        th.join(5)
        return self.proc.returncode, lines

    def _render_moment(self, ch, item):
        """Момент стрима → отрезок с Twitch → вертикальное видео с надписью и субтитрами."""
        m = json.loads(item['text'])
        profile, login = ch['profile'], m.get('login') or ch['profile']
        cfg = self.service.settings.load()
        cli = cfg.get('cli_path') or str(self.root / 'desktop' / 'tools' / 'TwitchDownloaderCLI' / 'TwitchDownloaderCLI.exe')
        quality = (cfg.get('pipeline') or {}).get('quality') or '1080p60'
        out = self.root / 'state' / 'stream_shorts' / f"{profile}_{item['id']}.mp4"
        self._stage('Собираю шортс из стрима')
        stsh.render(m, m['vod_id'], out, cli, quality, progress=lambda x: self._stage('Собираю шортс из стрима', x))
        title = (m['title'][:88].rstrip(' ,.;:') + ' #shorts')[:100]
        if lang_mod.is_en():
            desc = f"{m['title']}\n\n{login} live on Twitch: https://twitch.tv/{login}\n\n#shorts #{login} #twitch #stream"
        else:
            desc = f"{m['title']}\n\nСтрим {login} на Twitch: https://twitch.tv/{login}\n\n#shorts #{login} #стрим #twitch"
        self.store.update_item(item['id'], video_path=str(out), title=title, description=desc)
        self.log(f'{profile}: шортс из стрима готов — {title}')
        return self.store.item(item['id'])

    def _generate(self, ch, item):
        if item['kind'] == 'moment':
            return self._render_moment(ch, item)
        fac, profile = self.factory, ch['profile']
        py = str(fac / '.venv' / 'Scripts' / 'python.exe')
        jobs = fac / 'jobs'
        jobs.mkdir(exist_ok=True)
        base = jobs / f"yt_{profile}_{item['id']}"
        text = item['text']
        if item['kind'] == 'auto' and not text:
            self._stage('Выбираю научную тему')
            hist = Path(f'{base}_history.json')
            hist.write_text(json.dumps(self.store.topics(profile), ensure_ascii=False), encoding='utf-8')
            code, out = self._run([py, '-u', 'tools\\auto_topic.py', '--history', str(hist), '--out', f'{base}_topic.json'], 'Выбираю тему')
            res = json.loads(Path(f'{base}_topic.json').read_text(encoding='utf-8'))
            if 'error' in res:
                raise RuntimeError('тема: ' + res['error'])
            text = res['post']
            self.store.add_topic(profile, res['topic'])
            self.store.update_item(item['id'], text=text, title=res['topic'])
        if item['kind'] == 'fact':
            text = 'Интересный факт для короткого ролика. Раскрой его просто и понятно: ' + text
        Path(f'{base}_post.txt').write_text(text, encoding='utf-8')
        self._stage('Пишу сценарий')
        code, out = self._run([py, '-u', 'tools\\write_script.py', '--post', f'{base}_post.txt', '--out', f'{base}_script.json'], 'Пишу сценарий')
        res = json.loads(Path(f'{base}_script.json').read_text(encoding='utf-8'))
        if 'error' in res:
            raise RuntimeError('сценарий: ' + res['error'])
        slug = f"yt_{profile}_{item['id']}"
        script = fac / 'scripts' / f'{slug}.json'
        script.write_text(json.dumps(res['scenario'], ensure_ascii=False, indent=2), encoding='utf-8')
        self._stage('Собираю видео')
        code, out = self._run([py, '-u', '-W', 'ignore', 'make_short.py', str(script), '--no-qc'], 'Собираю видео')
        video = fac / 'output' / f'{slug}.mp4'
        if code != 0 or not video.exists():
            raise RuntimeError('сборка видео: ' + (out[-1] if out else f'код {code}'))
        title, description = self._meta(ch, res['scenario'])
        self.store.update_item(item['id'], slug=slug, video_path=str(video), title=title, description=description)
        self.log(f'{profile}: видео готово — {title}')
        return self.store.item(item['id'])

    @staticmethod
    def _meta(ch, scenario):
        beats = scenario.get('beats', [])
        hook = next((b for b in beats if b.get('type') == 'hook'), beats[0] if beats else {})
        title = re.sub(r'\s+', ' ', str(hook.get('text') or hook.get('voice') or 'Короткий факт о здоровье').replace('*', '')).strip()
        title = (title[:88].rstrip(' ,.;:') + ' #shorts')[:100]
        lines = [str(b.get('voice', '')).replace('*', '').strip() for b in beats if b.get('type') != 'cta' and b.get('voice')]
        desc = ' '.join(lines)
        desc += '\n\n#shorts #здоровье #наука'
        return title, desc[:4900]

    # ---------- upload via the existing YouTube Studio browser ----------
    def _upload(self, ch, item):
        profile = ch['profile']
        if self.stream_busy():
            raise StreamBusy()
        binding = self.service.youtube_queue.get_profile(profile)
        if not binding:
            raise RuntimeError('канал больше не привязан во вкладке YouTube')
        audience = binding.get('audience') or ''
        if audience not in ('kids', 'not_kids'):
            raise RuntimeError('у канала не выбрана аудитория (YouTube → привязанный канал → Аудитория)')
        self._stage('Жду браузер YouTube')
        # Замок только этого канала и только на время работы окна загрузки: нарезки других каналов
        # и ожидание их обработки Shorts больше не задерживают.
        lock = ys.profile_lock(profile)
        if not lock.acquire(timeout=120):
            raise Deferred('Ждёт: в Chrome этого канала сейчас открыто окно загрузки нарезки')
        locked = True
        try:
            self._stage('Загружаю на YouTube')
            browser = ys.Browser(self.root, profile)
            try:
                with browser as tab:
                    row = {'id': item['id'], 'profile': profile, 'channel_id': binding['channel_id'], 'file_path': item['video_path'],
                           'title': item['title'] or 'Короткий факт о здоровье #shorts', 'description': item['description'],
                           'audience': audience, 'publish_at': None, 'clip_index': 1, 'section': 'short'}
                    try:
                        video_id = ys.YouTubeStudio(self.root).submit(tab, browser, row, progress=lambda m: self._stage('Загружаю на YouTube', m))
                    finally:
                        lock.release()
                        locked = False
                    # Studio has accepted the Short, but the file may still be transferring: closing Chrome
                    # now would abort it. Close only after the content list shows it uploaded, processed
                    # and public; otherwise leave Chrome open so the upload can finish on its own.
                    browser.keep_open = True
                    if video_id:
                        self._stage('Жду, пока YouTube загрузит и обработает ролик')
                        try:
                            ys.wait_for_receipt(tab, progress=lambda m: self._stage('Жду окончания загрузки на YouTube', m),
                                                timeout=FINISH_TIMEOUT, browser=browser,
                                                channel_id=binding['channel_id'], candidate_id=video_id, section='short')
                            browser.keep_open = False
                        except Exception as exc:
                            self.log(f'{profile}: ролик https://youtu.be/{video_id} отправлен, но YouTube ещё не показал его '
                                     f'готовым ({str(exc)[:150]}); Chrome оставлен открытым, чтобы загрузка не прервалась')
            except (ys.UploadLimitReached, ys.VerificationRequired):
                raise
            except Exception as exc:
                if not browser.attempt_may_upload:  # файл ещё не выбран — черновик не создан, можно повторить позже
                    raise Deferred(f'YouTube: {str(exc)[:200]} — повторю позже') from exc
                raise RuntimeError(f'загрузка не подтверждена ({str(exc)[:200]}); проверьте Studio перед повтором, чтобы не было дубля') from exc
        finally:
            if locked:
                lock.release()
        now = time.time()
        self.store.update_item(item['id'], status='done', video_id=str(video_id or ''), detail='Опубликовано')
        self.store.save_channel(profile, last_upload=now, next_due=now + int(ch['interval_hours']) * 3600, note='')
        self._notify(f"Shorts {profile}: опубликован «{item['title']}» https://youtu.be/{video_id}")
