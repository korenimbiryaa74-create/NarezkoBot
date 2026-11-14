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


def download_chat(vod_id, folder, cli, progress=None, timeout=3600):
    """chat.json in the job's work folder (cached; a partial file is never kept)."""
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    path=folder/'chat.json'
    if path.is_file() and path.stat().st_size>0:return path
    tmp=folder/'chat.downloading.json'
    if tmp.exists():tmp.unlink()
    if progress:progress('Скачиваю чат VOD для поиска игровых моментов')
    proc=subprocess.run([str(cli),'chatdownload','-u',str(vod_id),'-o',str(tmp),'--collision','Overwrite'],
                        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if proc.returncode!=0 or not tmp.is_file() or tmp.stat().st_size==0:
        raise RuntimeError('чат не скачан: '+((proc.stdout or '')+(proc.stderr or '')).strip()[-200:])
    os.replace(tmp,path)
    return path


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
