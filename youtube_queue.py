"""Persistent, fail-closed planning for YouTube uploads.

This module does not upload anything. A future uploader must verify the active
Studio channel, record 'uploading' BEFORE handing a file to the browser, and
record a receipt only after YouTube confirms the final visibility/schedule.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

_LOGIN = re.compile(r'^[a-z0-9_]{1,25}$')
_CHANNEL = re.compile(r'^UC[A-Za-z0-9_-]{22}$')
_VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')


def profile_key(value):
    value = str(value).strip().lower()
    if value == '__test__' or _LOGIN.fullmatch(value):
        return value
    raise ValueError('Некорректный профиль YouTube')


class YouTubeQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS youtube_profiles (
                profile TEXT PRIMARY KEY, channel_id TEXT NOT NULL UNIQUE,
                interval_hours INTEGER NOT NULL CHECK(interval_hours IN (1,16)),
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS youtube_uploads (
                id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
                clip_index INTEGER NOT NULL CHECK(clip_index > 0),
                profile TEXT NOT NULL, channel_id TEXT NOT NULL,
                file_path TEXT NOT NULL, file_size INTEGER NOT NULL,
                file_mtime_ns INTEGER NOT NULL, title TEXT NOT NULL,
                publish_at REAL, state TEXT NOT NULL DEFAULT 'queued'
                    CHECK(state IN ('queued','uploading','confirmed','needs_review','cancelled')),
                video_id TEXT, detail TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(job_id, clip_index)
            );
            CREATE INDEX IF NOT EXISTS ix_youtube_uploads_ready
                ON youtube_uploads(state,id);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(youtube_profiles)')}
            if 'audience' not in columns:
                db.execute("ALTER TABLE youtube_profiles ADD COLUMN audience TEXT NOT NULL DEFAULT ''")
            columns = {r['name'] for r in db.execute('PRAGMA table_info(youtube_uploads)')}
            if 'audience' not in columns:
                db.execute("ALTER TABLE youtube_uploads ADD COLUMN audience TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def bind(self, profile, channel_id, interval_hours=1, now=None, audience=''):
        """Bind only an independently verified Studio channel ID; never a login name."""
        profile = profile_key(profile)
        if not _CHANNEL.fullmatch(str(channel_id)):
            raise ValueError('Нужен проверенный ID канала YouTube')
        if isinstance(interval_hours, bool) or interval_hours not in (1, 16):
            raise ValueError('Интервал публикаций должен быть 1 или 16 часов')
        if audience not in ('', 'kids', 'not_kids'):
            raise ValueError('Укажите аудиторию YouTube')
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError('Некорректное время')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT channel_id FROM youtube_profiles WHERE profile=?', (profile,)).fetchone()
            if existing and existing['channel_id'] != channel_id:
                pending = db.execute("SELECT 1 FROM youtube_uploads WHERE profile=? AND state IN ('queued','uploading','needs_review') LIMIT 1", (profile,)).fetchone()
                if pending:
                    db.rollback()
                    raise ValueError('Нельзя менять канал, пока есть незавершённые публикации')
            try:
                db.execute('''INSERT INTO youtube_profiles(profile,channel_id,interval_hours,updated_at,audience)
                    VALUES (?,?,?,?,?) ON CONFLICT(profile) DO UPDATE SET
                    channel_id=excluded.channel_id,interval_hours=excluded.interval_hours,updated_at=excluded.updated_at,audience=excluded.audience''',
                    (profile, channel_id, interval_hours, now, audience))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                raise ValueError('Этот канал уже привязан к другому профилю') from None

    def enqueue(self, job_id, streamer, files, *, is_test=False, now=None, titles=None):
        """Atomically plan one job; no files are uploaded or made public here.

        The first clip is immediate; following clips get 1h/16h increments.
        For local tests every clip is immediate on the dedicated test channel.
        Replaying exactly the same batch is idempotent; a mismatch is rejected.
        """
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise ValueError('Некорректный номер задания')
        profile = profile_key('__test__' if is_test else streamer)
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError('Некорректное время')
        files=list(files)
        if titles is not None and (not isinstance(titles,list) or len(titles)!=len(files)):
            raise ValueError('Число названий не совпадает с числом файлов')
        prepared = []
        for index,item in enumerate(files):
            p = Path(item).resolve(strict=True)
            if not p.is_file() or p.suffix.lower() != '.mp4':
                raise ValueError('Для YouTube нужен готовый MP4-файл')
            stat = p.stat()
            if stat.st_size <= 0:
                raise ValueError('Пустой MP4-файл')
            title = titles[index] if titles is not None else p.stem.strip()[:100]
            if not isinstance(title,str) or not title.strip() or '<' in title or '>' in title or len(title.encode('utf-16-le'))//2>100:
                raise ValueError('Некорректное название YouTube')
            title=title.strip()
            if not title:
                raise ValueError('Пустое название нарезки')
            prepared.append((str(p), stat.st_size, stat.st_mtime_ns, title))
        if len({row[0] for row in prepared}) != len(prepared):
            raise ValueError('Один файл указан несколько раз')
        if not prepared:
            return 0
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = db.execute('SELECT * FROM youtube_profiles WHERE profile=?', (profile,)).fetchone()
            if not binding:
                db.rollback()
                raise ValueError('Сначала привяжите канал YouTube к профилю')
            if binding['audience'] not in ('kids', 'not_kids'):
                db.rollback()
                raise ValueError('Укажите аудиторию канала YouTube в настройках')
            existing = db.execute('SELECT * FROM youtube_uploads WHERE job_id=? ORDER BY clip_index', (job_id,)).fetchall()
            if existing:
                same = len(existing) == len(prepared) and all(
                    r['clip_index'] == i and r['profile'] == profile and
                    r['channel_id'] == binding['channel_id'] and
                    (r['file_path'], r['file_size'], r['file_mtime_ns'], r['title']) == row
                    for i, (r, row) in enumerate(zip(existing, prepared), 1))
                if not same:
                    db.rollback()
                    raise ValueError('Для задания уже существует другая очередь YouTube; проверьте её вручную')
                db.commit()
                return 0
            for index, (path, size, mtime, title) in enumerate(prepared, 1):
                publish_at = None if is_test or index == 1 else now + (index - 1) * binding['interval_hours'] * 3600
                db.execute('''INSERT INTO youtube_uploads
                    (job_id,clip_index,profile,channel_id,file_path,file_size,file_mtime_ns,title,publish_at,created_at,updated_at,audience)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (job_id,index,profile,binding['channel_id'],path,size,mtime,title,publish_at,now,now,binding['audience']))
            db.commit()
            return len(prepared)

    def claim(self, now=None, *, finished_only=False):
        """Claim next clip only when its batch and browser profile are safe to use."""
        now = time.time() if now is None else float(now)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ready = " AND EXISTS (SELECT 1 FROM jobs j WHERE j.id=u.job_id AND j.state='done')" if finished_only else ''
            row = db.execute('''SELECT u.* FROM youtube_uploads u WHERE u.state='queued' ''' + ready + '''
                AND NOT EXISTS (SELECT 1 FROM youtube_uploads prior
                    WHERE prior.job_id=u.job_id AND prior.clip_index<u.clip_index
                    AND prior.state NOT IN ('confirmed','cancelled'))
                AND NOT EXISTS (SELECT 1 FROM youtube_uploads blocking
                    WHERE blocking.profile=u.profile AND blocking.state IN ('uploading','needs_review'))
                ORDER BY u.id LIMIT 1''').fetchone()
            if row is None:
                db.commit()
                return None
            p = Path(row['file_path'])
            try:
                st = p.stat()
                valid = p.is_file() and st.st_size == row['file_size'] and st.st_mtime_ns == row['file_mtime_ns']
            except OSError:
                valid = False
            if not valid:
                db.execute("UPDATE youtube_uploads SET state='needs_review',detail=?,updated_at=? WHERE id=?",
                           ('Файл отсутствует или изменился; проверьте перед публикацией',now,row['id']))
                db.commit()
                return None
            # Conservative: even a crash before upload requires manual verification.
            db.execute("UPDATE youtube_uploads SET state='uploading',updated_at=? WHERE id=?", (now,row['id']))
            result = dict(db.execute('SELECT * FROM youtube_uploads WHERE id=?', (row['id'],)).fetchone())
            db.commit()
            return result

    def confirm(self, upload_id, channel_id, video_id, *, now=None):
        if not _CHANNEL.fullmatch(str(channel_id)) or not _VIDEO_ID.fullmatch(str(video_id)):
            raise ValueError('Некорректное подтверждение YouTube')
        now = time.time() if now is None else float(now)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT job_id,clip_index FROM youtube_uploads WHERE id=? AND state='uploading' AND channel_id=?", (upload_id,channel_id)).fetchone()
            if row is None:
                db.rollback()
                raise ValueError('Публикация не выполняется или канал не совпадает')
            if db.execute("SELECT 1 FROM youtube_uploads WHERE state='confirmed' AND video_id=? AND id<>?",(video_id,upload_id)).fetchone():
                db.rollback();raise ValueError('Это видео уже привязано к другой загрузке')
            db.execute("UPDATE youtube_uploads SET state='confirmed',video_id=?,detail='',updated_at=? WHERE id=?", (video_id,now,upload_id))
            if row['clip_index'] == 1:
                # Anchor delays to the first accepted publication, not render completion.
                db.execute("UPDATE youtube_uploads SET publish_at=?+(publish_at-created_at),updated_at=? WHERE job_id=? AND clip_index>1 AND state='queued' AND publish_at IS NOT NULL", (now,now,row['job_id']))
            db.commit()

    def mark_uncertain(self, upload_id, detail='', *, now=None):
        now = time.time() if now is None else float(now)
        with self._connect() as db:
            cur = db.execute("UPDATE youtube_uploads SET state='needs_review',detail=?,updated_at=? WHERE id=? AND state='uploading'",
                             (str(detail)[:500],now,upload_id))
            if cur.rowcount != 1:
                raise ValueError('Публикация не выполняется')

    def recover_interrupted(self, *, now=None):
        now = time.time() if now is None else float(now)
        with self._connect() as db:
            cur = db.execute("UPDATE youtube_uploads SET state='needs_review',detail=?,updated_at=? WHERE state='uploading'",
                             ('Прервано: проверьте YouTube Studio перед повтором',now))
            return cur.rowcount

    def list_profiles(self):
        """Return locally saved channel bindings; this does not validate browser login."""
        with self._connect() as db:
            rows = db.execute("""SELECT profile,channel_id,interval_hours,audience,updated_at
                FROM youtube_profiles ORDER BY CASE WHEN profile='__test__' THEN 0 ELSE 1 END,profile""").fetchall()
            return [dict(row) for row in rows]

    def get_profile(self, profile):
        with self._connect() as db:
            row = db.execute('SELECT * FROM youtube_profiles WHERE profile=?', (profile_key(profile),)).fetchone()
            return dict(row) if row else None

    def confirm_reviewed(self, upload_id, video_id):
        # This is a human attestation after checking channel, video and visibility.
        if not _VIDEO_ID.fullmatch(str(video_id)):
            raise ValueError('Нужен корректный ID видео YouTube')
        now=time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT job_id,clip_index FROM youtube_uploads WHERE id=? AND state='needs_review'",(upload_id,)).fetchone()
            if row is None:
                db.rollback();raise ValueError('Запись не ожидает проверки')
            if db.execute("SELECT 1 FROM youtube_uploads WHERE state='confirmed' AND video_id=?",(video_id,)).fetchone():
                db.rollback();raise ValueError('Это видео уже привязано к другой загрузке')
            db.execute("UPDATE youtube_uploads SET state='confirmed',video_id=?,detail=?,updated_at=? WHERE id=?",(video_id,'Подтверждено пользователем после проверки Studio',now,upload_id))
            if row['clip_index']==1:
                # Conservative: remaining delay begins at user confirmation.
                db.execute("UPDATE youtube_uploads SET publish_at=?+(publish_at-created_at),updated_at=? WHERE job_id=? AND clip_index>1 AND state='queued' AND publish_at IS NOT NULL",(now,now,row['job_id']))
            db.commit()

    def retry_reviewed(self, upload_id):
        # Caller must explicitly confirm no upload/draft exists on YouTube.
        with self._connect() as db:
            cur = db.execute("UPDATE youtube_uploads SET state='queued',detail='',updated_at=? WHERE id=? AND state='needs_review'", (time.time(),upload_id))
            if cur.rowcount != 1:
                raise ValueError('Можно повторить только запись, требующую проверки')

    def list_uploads(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM youtube_uploads ORDER BY id')]
