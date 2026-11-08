"""Durable queue; interruptions must be reconciled before retrying external work."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
import time
from pathlib import Path

class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY, vod_id TEXT UNIQUE NOT NULL, streamer TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'queued',
              stage TEXT NOT NULL DEFAULT 'Ожидает обработки', detail TEXT NOT NULL DEFAULT '',
              output_dir TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
              available_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS job_events (
              id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
              timestamp REAL NOT NULL, stage TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS ix_jobs_ready ON jobs(state,available_at,id);
            ''')
            # Existing installations retain all rows and gain the optional duration.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'duration_seconds' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN duration_seconds REAL')
            for name, kind in (('local_name','TEXT'),('local_size','INTEGER'),('local_mtime_ns','INTEGER')):
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
            db.commit()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        finally:
            db.close()

    def add(self, vod_id, streamer, title='', duration_seconds=None):
        vod_id, streamer = str(vod_id).strip(), str(streamer).strip()
        if not vod_id.isdecimal() or not streamer:
            raise ValueError('Требуются числовой ID записи и имя стримера')
        if duration_seconds is not None:
            import math
            duration_seconds = float(duration_seconds)
            if not math.isfinite(duration_seconds) or duration_seconds <= 0:
                raise ValueError('Некорректная длительность записи')
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute('INSERT OR IGNORE INTO jobs(vod_id,streamer,title,created_at,updated_at,duration_seconds) VALUES (?,?,?,?,?,?)',
                             (vod_id, streamer, str(title), now, now, duration_seconds))
            if not cur.rowcount and duration_seconds is not None:
                db.execute("UPDATE jobs SET duration_seconds=? WHERE vod_id=? AND duration_seconds IS NULL AND state IN ('queued','retry')", (duration_seconds, vod_id))
            row = db.execute('SELECT * FROM jobs WHERE vod_id=?', (vod_id,)).fetchone()
            if cur.rowcount: self._event(db, row['id'], now, 'Ожидает обработки')
            db.commit()
            return dict(row), bool(cur.rowcount)

    def add_local(self, name, size, mtime_ns):
        # Store a filename, never a path supplied by a queue row.
        import uuid
        name = str(name)
        if (not name or name in ('.','..') or '/' in name or '\\' in name or
                Path(name).name != name or int(size) <= 0 or int(mtime_ns) < 0):
            raise ValueError('Некорректный тестовый файл')
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE local_name=? AND local_size=? AND local_mtime_ns=? AND state IN ('queued','running','retry') ORDER BY id LIMIT 1", (name,int(size),int(mtime_ns))).fetchone()
            if row:
                db.commit(); return dict(row), False
            vod_id='test-'+uuid.uuid4().hex
            cur=db.execute("INSERT INTO jobs(vod_id,streamer,title,local_name,local_size,local_mtime_ns,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",(vod_id,'test',name,name,int(size),int(mtime_ns),now,now))
            row=db.execute('SELECT * FROM jobs WHERE id=?',(cur.lastrowid,)).fetchone()
            self._event(db,row['id'],now,'Ожидает обработки','Локальный тест: '+name)
            db.commit(); return dict(row), True

    @staticmethod
    def _event(db, job_id, now, stage, detail=''):
        db.execute('INSERT INTO job_events(job_id,timestamp,stage,detail) VALUES (?,?,?,?)', (job_id, now, stage, detail))

    def claim(self, now=None):
        now = time.time() if now is None else float(now)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE state IN ('queued','retry') AND available_at<=? ORDER BY id LIMIT 1", (now,)).fetchone()
            if not row:
                db.commit()
                return None
            job_id = row['id']
            db.execute("UPDATE jobs SET state='running',stage='Запуск',detail='',attempts=attempts+1,updated_at=? WHERE id=?", (now,job_id))
            self._event(db,job_id,now,'Запуск')
            result = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            db.commit()
            return result

    def update_stage(self, job_id, stage, detail='', output_dir=None):
        if not str(stage).strip(): raise ValueError('Название этапа не может быть пустым')
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute("UPDATE jobs SET stage=?,detail=?,output_dir=COALESCE(?,output_dir),updated_at=? WHERE id=? AND state='running'", (str(stage),str(detail),output_dir,now,job_id))
            if cur.rowcount != 1:
                db.rollback(); raise ValueError('Задание не выполняется')
            self._event(db,job_id,now,str(stage),str(detail)); db.commit()

    def finish(self, job_id, state, detail='', retry_after=0):
        if state not in ('done','failed','retry') or retry_after < 0: raise ValueError('Недопустимое состояние или задержка')
        now = time.time()
        label = {'done':'Готово','failed':'Ошибка','retry':'Ожидает повтора'}[state]
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute("UPDATE jobs SET state=?,stage=?,detail=?,available_at=?,updated_at=? WHERE id=? AND state='running'", (state,label,str(detail),now+retry_after if state == 'retry' else 0,now,job_id))
            if cur.rowcount != 1:
                db.rollback(); raise ValueError('Задание не выполняется')
            self._event(db,job_id,now,label,str(detail)); db.commit()

    def recover_interrupted(self, safe_stages=()):
        # Only call when no worker is running. Never replay an uncertain model request.
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = list(db.execute("SELECT id,stage FROM jobs WHERE state='running'"))
            for row in rows:
                job_id=row['id']
                if row['stage'] in safe_stages:
                    detail = 'Восстановлено после прерывания; ожидает повтора'
                    db.execute("UPDATE jobs SET state='retry',stage='Ожидает повтора',detail=?,available_at=0,updated_at=? WHERE id=?", (detail,now,job_id))
                    self._event(db,job_id,now,'Ожидает повтора',detail)
                else:
                    detail = 'Работа прервалась; проверьте результаты перед повтором'
                    db.execute("UPDATE jobs SET state='failed',stage='Прервано',detail=?,updated_at=? WHERE id=?", (detail,now,job_id))
                    self._event(db,job_id,now,'Прервано',detail)
            db.commit(); return len(rows)

    def retry(self, job_id):
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute("UPDATE jobs SET state='queued',stage='Ожидает обработки',detail='',available_at=0,updated_at=? WHERE id=? AND state='failed'", (now,job_id))
            if cur.rowcount != 1:
                db.rollback(); raise ValueError('Повторить можно только задание с ошибкой')
            self._event(db,job_id,now,'Ожидает обработки'); db.commit()

    def list_jobs(self, limit=100):
        with self._connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT ?', (max(1,min(int(limit),1000)),))]

    def events(self, job_id):
        with self._connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM job_events WHERE job_id=? ORDER BY id', (job_id,))]
