"""Twitch VOD data for game clips: VOD info, game chapters and chat activity per minute.

Only used when «Игры» is enabled for a streamer: nothing here runs otherwise.
"""
from __future__ import annotations
import collections
import json
import os
import re
import statistics
import subprocess
from pathlib import Path
import requests

GQL='https://gql.twitch.tv/gql'
CLIENT_ID='kimne78kx3ncx6brgo4mv6wki5h1ko'
HEADER='=== ДАННЫЕ TWITCH ==='


def _gql(query, variables, timeout=20):
    s=requests.Session();s.trust_env=False
    r=s.post(GQL,json={'query':query,'variables':variables},headers={'Client-ID':CLIENT_ID},timeout=timeout)
    r.raise_for_status()
    data=r.json() or {}
    if data.get('errors'):raise RuntimeError('Twitch: '+str(data['errors'])[:200])
    return data.get('data') or {}


def parse_vod_id(text):
    """VOD id from a link (twitch.tv/videos/123, twitch.tv/login/video/123) or a bare number."""
    text=str(text or '').strip()
    m=re.search(r'twitch\.tv/(?:[A-Za-z0-9_]+/)?videos?/(\d{5,14})',text) or re.fullmatch(r'(\d{5,14})',text)
    return m.group(1) if m else ''


def vod_info(vod_id):
    v=(_gql('query($id:ID!){video(id:$id){id title lengthSeconds createdAt owner{login displayName} game{displayName}}}',
            {'id':str(vod_id)}).get('video'))
    if not v or not v.get('owner'):return None
    return {'id':str(v['id']),'title':str(v.get('title') or ''),'duration':float(v.get('lengthSeconds') or 0),
            'login':str(v['owner'].get('login') or '').lower(),'display':str(v['owner'].get('displayName') or ''),
            'game':str((v.get('game') or {}).get('displayName') or '')}


def chapters(vod_id, duration=None):
    """[{start,end,game}] from Twitch chapter markers; one chapter with the VOD game if there are none."""
    q=('query($id:ID!){video(id:$id){lengthSeconds game{displayName} moments(momentRequestType:VIDEO_CHAPTER_MARKERS){edges{node{'
       'positionMilliseconds durationMilliseconds description details{... on GameChangeMomentDetails{game{displayName}}}}}}}}')
    v=_gql(q,{'id':str(vod_id)}).get('video') or {}
    total=float(duration or v.get('lengthSeconds') or 0)
    out=[]
    for e in ((v.get('moments') or {}).get('edges') or []):
        n=(e or {}).get('node') or {}
        a=float(n.get('positionMilliseconds') or 0)/1000;d=float(n.get('durationMilliseconds') or 0)/1000
        game=((n.get('details') or {}).get('game') or {}).get('displayName') or n.get('description') or ''
        if game:out.append({'start':a,'end':min(total,a+d) if d>0 else total,'game':str(game)})
    if not out:
        game=(v.get('game') or {}).get('displayName') or ''
        if game:out=[{'start':0.0,'end':total,'game':str(game)}]
    return sorted(out,key=lambda x:x['start'])


# TwitchDownloaderCLI chatdownload sometimes hangs forever on a dead Twitch connection: the process
# stays alive with open sockets but prints nothing more. It normally prints a [STATUS] line several
# times a second, so silence means a hang: we kill it and retry instead of waiting for the timeout.
CHAT_STALL_SECONDS=150
CHAT_ATTEMPTS=3
# Total limit per attempt. Hangs are caught by CHAT_STALL_SECONDS; a 24/7 or 40-hour VOD with a busy chat
# legitimately needs more than an hour, so the old 1 h limit killed good downloads and retried them from zero.
CHAT_TIMEOUT_SECONDS=8*3600
_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)

import threading as _threading
_inflight={}            # chat.json path -> Event: one download per file inside this bot process
_inflight_lock=_threading.Lock()


def _flags():
    try:
        import low_impact
        return low_impact.creation_flags()
    except Exception:
        return _NO_WINDOW


def _threads():
    try:
        import low_impact
        return low_impact.chat_threads()
    except Exception:
        return 4


def _kill_tree(proc):
    try:
        if os.name=='nt':
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,creationflags=_NO_WINDOW,timeout=30)
        else:proc.kill()
    except Exception:pass
    try:proc.wait(timeout=30)
    except Exception:pass


def _kill_orphans(target):
    """Stop a chatdownload left over from a previous bot run that still writes to this file."""
    if os.name!='nt':return
    script=("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-CimInstance Win32_Process -Filter \"Name='TwitchDownloaderCLI.exe'\" | % { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:out=subprocess.run(['powershell','-NoProfile','-Command',script],capture_output=True,text=True,encoding='utf8',
                           errors='replace',creationflags=_NO_WINDOW,timeout=60).stdout
    except Exception:return
    for line in (out or '').splitlines():
        pid,_,cmd=line.partition('\t')
        if pid.strip().isdigit() and str(target).lower() in cmd.lower():
            subprocess.run(['taskkill','/PID',pid.strip(),'/T','/F'],stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,creationflags=_NO_WINDOW,timeout=30)


def _error_reason(lines):
    for l in lines:
        if 'Exception' in l and ':' in l:return l.strip()[:200]
    tail=[l.strip() for l in lines if l.strip() and not l.lstrip().startswith('at ')]
    return (tail[-1] if tail else '')[:200]


def _run_chat_once(cmd, cwd, progress, stall, timeout):
    """(returncode, output lines, stalled). Kills the process after `stall` s with no output."""
    import threading,time
    proc=subprocess.Popen(cmd,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=_flags())
    lines=[];state={'last':time.monotonic()}
    def reader():
        buf=b''
        while True:
            chunk=proc.stdout.read1(4096) if hasattr(proc.stdout,'read1') else proc.stdout.read(1)
            if not chunk:break
            state['last']=time.monotonic();buf+=chunk
            parts=re.split(rb'[\r\n]+',buf);buf=parts.pop()
            for p in parts:
                t=p.decode('utf-8','replace').strip()
                if t:lines.append(t);del lines[:-200]
        if buf.strip():lines.append(buf.decode('utf-8','replace').strip())
    th=threading.Thread(target=reader,daemon=True);th.start()
    started=time.monotonic();shown='';stalled=False
    try:
        while proc.poll() is None:
            time.sleep(2)
            last=next((l for l in reversed(lines) if l.startswith('[STATUS]')),'')
            m=re.search(r'\[STATUS\]\s*-\s*(.+?)\s+(\d+)%',last) if last else None
            if progress and m:
                stage='загрузка' if m.group(1).lower().startswith('downloading') else 'обработка'
                pct=int(m.group(2));key=(stage,pct//10)
                if key!=shown:
                    shown=key
                    progress(f'Скачиваю чат VOD для поиска игровых моментов · {stage} {pct}%')
            now=time.monotonic()
            if now-state['last']>stall or now-started>timeout:
                stalled=True;_kill_tree(proc);break
    except BaseException:
        _kill_tree(proc);raise
    th.join(timeout=10)
    return (-1 if stalled else proc.returncode),lines,stalled


def download_chat(vod_id, folder, cli, progress=None, timeout=None, stall=None, attempts=None):
    """chat.json in the job's work folder (cached; a partial file is never kept)."""
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    path=folder/'chat.json'
    if path.is_file() and path.stat().st_size>0:return path
    key=str(path).lower()
    while True:
        with _inflight_lock:
            ev=_inflight.get(key);owner=ev is None
            if owner:ev=_inflight[key]=_threading.Event()
        if owner:break
        # The same chat is already downloading in the background (started together with the video).
        if progress:progress('Чат VOD уже скачивается в фоне — дожидаюсь')
        ev.wait()
        if path.is_file() and path.stat().st_size>0:return path
    try:
        return _download_chat(vod_id,folder,path,cli,progress,timeout or CHAT_TIMEOUT_SECONDS,stall,attempts)
    finally:
        with _inflight_lock:_inflight.pop(key,None)
        ev.set()


def prefetch_chat(vod_id, folder, cli, log=None):
    """Start downloading chat.json in a background thread (no-op if it is cached or already running)."""
    folder=Path(folder);path=folder/'chat.json'
    if path.is_file() and path.stat().st_size>0:return None
    with _inflight_lock:
        if str(path).lower() in _inflight:return None
    def run():
        import time
        started=time.monotonic()
        try:
            download_chat(vod_id,folder,cli)
            if log:log(f'Чат VOD скачан в фоне параллельно с видео за {int(time.monotonic()-started)//60} мин')
        except Exception as exc:
            if log:log(f'Фоновая загрузка чата не удалась ({str(exc)[:150]}) — повторю при поиске моментов')
    th=_threading.Thread(target=run,name=f'chat-prefetch-{vod_id}',daemon=True);th.start()
    if log:log(f'Чат VOD качается в фоне параллельно с видео ({_threads()} потоков)')
    return th


def _download_chat(vod_id, folder, path, cli, progress, timeout, stall, attempts):
    tmp=folder/'chat.downloading.json'
    _kill_orphans(tmp)
    if tmp.exists():tmp.unlink()
    stall=stall or CHAT_STALL_SECONDS;attempts=attempts or CHAT_ATTEMPTS
    if progress:progress('Скачиваю чат VOD для поиска игровых моментов')
    cmd=[str(cli),'chatdownload','-u',str(vod_id),'-o',str(tmp),'--collision','Overwrite','-t',str(_threads())]
    reason=''
    for attempt in range(1,attempts+1):
        code,lines,stalled=_run_chat_once(cmd,str(Path(cli).parent),progress,stall,timeout)
        if code==0 and tmp.is_file() and tmp.stat().st_size>0:
            os.replace(tmp,path)
            return path
        try:
            if tmp.exists():tmp.unlink()
        except OSError:pass
        reason=('зависла без новых данных '+str(stall)+' с' if stalled else _error_reason(lines) or 'код '+str(code))
        if attempt<attempts and progress:
            progress(f'Загрузка чата {reason}; повтор {attempt+1}/{attempts}')
    raise RuntimeError('чат не скачан: '+reason)


def _norm_msg(text):
    text=re.sub(r'\s+',' ',str(text or '')).strip().lower()
    return text[:30]


def chat_activity(chat_path, duration):
    """Messages per minute plus the most repeated messages of each minute."""
    data=json.loads(Path(chat_path).read_text(encoding='utf-8'))
    minutes=max(1,int(float(duration)//60)+1)
    counts=[0]*minutes;words=[collections.Counter() for _ in range(minutes)]
    for c in data.get('comments') or []:
        try:m=int(float(c.get('content_offset_seconds') or 0)//60)
        except (TypeError,ValueError):continue
        if 0<=m<minutes:
            counts[m]+=1
            body=_norm_msg(((c.get('message') or {}).get('body')))
            if body:words[m][body]+=1
    return counts,words


def _hm(sec):
    sec=int(sec);return f'{sec//3600:02d}:{sec%3600//60:02d}'


def game_document(chaps, counts, words):
    """Compact text block appended to the transcript file for the moments request."""
    lines=[HEADER,'Это не расшифровка: данные Twitch для поиска игровых моментов.','',
           'ИГРЫ ПО ВРЕМЕНИ (главы Twitch):']
    lines+=[f"{_hm(c['start'])}–{_hm(c['end'])}  {c['game']}" for c in chaps] or ['нет данных']
    active=[x for x in counts if x>0]
    base=statistics.median(active) if active else 0
    lines+=['',f'АКТИВНОСТЬ ЧАТА: ЧЧ:ММ — сообщений за минуту; обычный уровень ≈ {base:.0f}; ▲ — всплеск (вдвое выше обычного и больше); '
            'в кавычках — самые частые сообщения минуты.']
    for m,n in enumerate(counts):
        spike=base>0 and n>=max(2*base,base+10)
        line=f'{_hm(m*60)} {n}'+(' ▲' if spike else '')
        if spike:
            top=[f'«{w}»×{k}' for w,k in words[m].most_common(3) if k>=3]
            if top:line+=' '+', '.join(top)
        lines.append(line)
    return '\n'.join(lines)


def game_context(vod_id, work, cli, duration, progress=None):
    """Chapters + chat activity text, or raises (the caller continues without it)."""
    chaps=chapters(vod_id,duration)
    path=download_chat(vod_id,Path(work),cli,progress=progress)
    counts,words=chat_activity(path,duration)
    if progress:progress(f'Чат: сообщений {sum(counts)}, игр по главам Twitch: {len(chaps)}')
    return game_document(chaps,counts,words)
