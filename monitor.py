"""Persistent Twitch stream-to-VOD monitor; independent from any UI.

A failed API call is never interpreted as an offline status. Each observed
stream is matched to its own VOD using Twitch's stream_id, never 'latest VOD'.
"""
from __future__ import annotations
import re
import time
from contextlib import contextmanager
from job_store import JobStore

LOGIN = re.compile(r'^[a-z0-9_]{1,25}$')

def normalize_login(value):
    value = str(value).strip().lower()
    if not LOGIN.fullmatch(value): raise ValueError('Некорректный логин Twitch')
    return value

class TwitchClient:
    def __init__(self, client_id, client_secret, proxy_url=''):
        import requests
        if not client_id.strip() or not client_secret.strip(): raise ValueError('Нужны Twitch Client ID и Client Secret')
        self.requests = requests
        self.client_id, self.client_secret = client_id.strip(), client_secret.strip()
        self.session = requests.Session()
        # Do not inherit environment proxies; use only explicit UI settings.
        self.session.trust_env = False
        self.session.proxies = {'http':proxy_url,'https':proxy_url} if proxy_url else {}
        self.token = None
        self.deadline = 0

    def _token(self):
        response = self.session.post('https://id.twitch.tv/oauth2/token', data={
            'client_id':self.client_id,'client_secret':self.client_secret,'grant_type':'client_credentials'}, timeout=30)
        response.raise_for_status()
        body=response.json()
        self.token=body['access_token']
        self.deadline=time.time()+max(1,int(body.get('expires_in',3600))-60)

    def get(self, endpoint, params):
        for attempt in range(2):
            if not self.token or time.time() >= self.deadline: self._token()
            response=self.session.get('https://api.twitch.tv/helix/'+endpoint, params=params,
                headers={'Client-Id':self.client_id,'Authorization':'Bearer '+self.token}, timeout=30)
            if response.status_code == 401 and attempt == 0:
                self.token=None
                continue
            response.raise_for_status()
            data=response.json()
            if not isinstance(data.get('data'),list): raise ValueError('Некорректный ответ Twitch')
            return data['data']
        raise RuntimeError('Twitch не принял токен')

    def stream(self, login):
        rows=self.get('streams',{'user_login':login})
        return rows[0] if rows else None

    def videos(self, user_id):
        return self.get('videos',{'user_id':user_id,'type':'archive','first':100})

class Monitor:
    def __init__(self, store: JobStore, api, stability_seconds=120, offline_checks=2, now=time.time):
        self.store, self.api, self.now = store, api, now
        self.stability_seconds=max(0,int(stability_seconds)); self.offline_checks=max(1,int(offline_checks))
        with self.store._connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS observed_streams (
              stream_id TEXT PRIMARY KEY, login TEXT NOT NULL, user_id TEXT NOT NULL,
              title TEXT NOT NULL, started_at TEXT NOT NULL, state TEXT NOT NULL,
              offline_count INTEGER NOT NULL DEFAULT 0, ended_at REAL,
              candidate_id TEXT, candidate_duration TEXT, stable_since REAL,
              queued_vod_id TEXT, last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_streams_login ON observed_streams(login,state);''')

    def tick(self, login):
        login=normalize_login(login)
        # Neither errors nor malformed live objects should advance the state.
        live=self.api.stream(login)
        if live is not None:
            if not isinstance(live,dict) or not all(live.get(k) for k in ('id','user_id','started_at')):
                raise ValueError('Неполные данные текущего стрима Twitch')
            stream_id=str(live['id']); user_id=str(live['user_id'])
        else: stream_id=None
        now=self.now()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Two successful polls required before transitioning an observed live stream offline.
            active=db.execute("SELECT * FROM observed_streams WHERE login=? AND state='live'",(login,)).fetchall()
            for row in active:
                if row['stream_id'] == stream_id:
                    db.execute("UPDATE observed_streams SET offline_count=0,updated_at=? WHERE stream_id=?",(now,row['stream_id']))
                else:
                    count=row['offline_count']+1
                    if count>=self.offline_checks:
                        db.execute("UPDATE observed_streams SET state='waiting_vod',offline_count=?,ended_at=?,updated_at=? WHERE stream_id=?",(count,now,now,row['stream_id']))
                    else:
                        db.execute('UPDATE observed_streams SET offline_count=?,updated_at=? WHERE stream_id=?',(count,now,row['stream_id']))
            if live is not None:
                db.execute('''INSERT OR IGNORE INTO observed_streams
                    (stream_id,login,user_id,title,started_at,state,updated_at) VALUES (?,?,?,?,?,'live',?)''',
                    (stream_id,login,user_id,str(live.get('title') or ''),str(live['started_at']),now))
                # If a stream reappears before its VOD was queued, continue observing it.
                db.execute("UPDATE observed_streams SET state='live',offline_count=0,ended_at=NULL,candidate_id=NULL,candidate_duration=NULL,stable_since=NULL,updated_at=? WHERE stream_id=? AND state='waiting_vod'",(now,stream_id))
            db.commit()
        self.poll_pending(login)

    def poll_pending(self, login):
        login=normalize_login(login)
        with self.store._connect() as db:
            pending=[dict(r) for r in db.execute("SELECT * FROM observed_streams WHERE login=? AND state='waiting_vod'",(login,))]
        for row in pending:
            now=self.now()
            try:
                videos=self.api.videos(row['user_id'])
                match=next((v for v in videos if str(v.get('stream_id') or '')==row['stream_id'] and v.get('id') and v.get('duration')),None)
                if not match: continue
                vod_id=str(match['id']); duration=str(match['duration'])
                if not vod_id.isdecimal(): raise ValueError('Некорректный ID VOD')
                # Candidate must remain unchanged across independent polls.
                same=row['candidate_id']==vod_id and row['candidate_duration']==duration and row['stable_since'] is not None
                stable_since=row['stable_since'] if same else now
                with self.store._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE observed_streams SET candidate_id=?,candidate_duration=?,stable_since=?,last_error='',updated_at=? WHERE stream_id=? AND state='waiting_vod'",(vod_id,duration,stable_since,now,row['stream_id']))
                    db.commit()
                if same and now-stable_since>=self.stability_seconds:
                    # Queue insertion and state are separately committed; UNIQUE VOD makes recovery idempotent.
                    self.store.add(vod_id,login,str(match.get('title') or row['title']))
                    with self.store._connect() as db:
                        db.execute("UPDATE observed_streams SET state='queued',queued_vod_id=?,updated_at=? WHERE stream_id=? AND state='waiting_vod'",(vod_id,now,row['stream_id']))
            except Exception as exc:
                # Avoid logging URL, credentials, tokens or response content.
                with self.store._connect() as db:
                    db.execute('UPDATE observed_streams SET last_error=?,updated_at=? WHERE stream_id=?',
                               (type(exc).__name__,now,row['stream_id']))
                raise

    def status(self):
        with self.store._connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM observed_streams ORDER BY updated_at DESC')]
