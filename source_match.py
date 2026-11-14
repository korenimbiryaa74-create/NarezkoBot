"""Identify likely YouTube source of a reaction.

When the finished clip is available, candidates are confirmed visually against
YouTube storyboards (visual_match.py). YouTube now answers 429 to caption
requests without PO tokens, so captions are only a fallback without a clip.

A match is evidence, not a guarantee of rights or original authorship. Never trust
model-suggested URLs without validation and independent content comparison.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

MAX_CANDIDATES=10
REUPLOAD=re.compile(r'\b(реакция|смотрит|реагирует|нарезка|нарезки|трейлер|трейлеры|reaction|reacts|watches|trailer)\b',re.I)
MIN_MATCHES=50
_rate_limited_until=0.0

class RateLimited(RuntimeError):
    pass


def tokens(text):
    return re.findall(r'[a-zа-я0-9]+',str(text).lower().replace('ё','е'))


def caption_evidence(segs, row, text):
    original=tokens(text)
    if len(original)<80:return {'matches':0,'sections':0}
    speech=[]
    for seg in segs:
        t=seg.get('start',-1)
        if row['start']<=t<row['end']:
            speech.extend(tokens(seg.get('text','')))
    if len(speech)<100:return {'matches':0,'sections':0}
    gram=6
    source={tuple(original[i:i+gram]) for i in range(len(original)-gram+1)}
    found=set(); sections=set()
    for i in range(len(speech)-gram+1):
        chunk=tuple(speech[i:i+gram])
        if chunk in source:
            found.add(chunk);sections.add(min(3,i*4//len(speech)))
    return {'matches':len(found),'sections':len(sections)}


def _candidate(video_id, segs, row):
    import requests
    cmd=[sys.executable,'-m','yt_dlp','--ignore-config','--skip-download','--no-playlist','--dump-single-json','--no-warnings','--extractor-args','youtube:lang=ru','--socket-timeout','10','--retries','0','https://www.youtube.com/watch?v='+video_id]
    try:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if p.returncode or len(p.stdout)>15_000_000:return None
        data=json.loads(p.stdout)
        if data.get('id')!=video_id or not isinstance(data.get('title'),str):return None
        subtitles=data.get('subtitles') or {};automatic=data.get('automatic_captions') or {}
        # Prefer original-language and human captions, then Russian auto captions.
        choices=[(subtitles,'ru'),(automatic,'ru-orig'),(automatic,'ru')]
        session=requests.Session();session.trust_env=False
        try:
            for collection,lang in choices:
                options=collection.get(lang) or []
                track=next((o for o in options if isinstance(o,dict) and o.get('ext')=='json3'),None)
                if not track:continue
                uri=track.get('url','');u=urlparse(uri)
                if u.scheme!='https' or u.hostname not in ('www.youtube.com','youtube.com') or u.path!='/api/timedtext' or u.username or u.password or u.port not in (None,443):continue
                with session.get(uri,timeout=(8,15),stream=True,allow_redirects=False) as r:
                    if r.status_code==429:raise RateLimited('YouTube ограничил запросы субтитров')
                    if r.status_code!=200:continue
                    body=bytearray()
                    for chunk in r.iter_content(65536):
                        body.extend(chunk)
                        if len(body)>5_000_000:raise ValueError('subtitles too large')
                cap=json.loads(body);events=cap.get('events') or []
                text=' '.join(seg.get('utf8','') for ev in events if isinstance(ev,dict) for seg in (ev.get('segs') or []) if isinstance(seg,dict) and isinstance(seg.get('utf8',''),str))
                proof=caption_evidence(segs,row,text)
                if proof['matches']>=MIN_MATCHES and proof['sections']>=2:
                    return {'id':video_id,'title':data['title'][:200],'channel':str(data.get('channel') or data.get('uploader') or '')[:120],
                            'evidence':{'method':'сопоставление субтитров','language':lang,**proof}}
        finally:session.close()
    except (ValueError,TypeError,KeyError,OSError,subprocess.SubprocessError,requests.RequestException):
        return None
    return None


def _resolve_visual(ids,video,cancel=None):
    """Confirm candidates by comparing the clip's frames with each video's storyboard."""
    from reaction_previews import SourceUncertain
    import visual_match as vm
    try:clip=vm.Clip(video,cancel=cancel)
    except vm.StoryboardUnavailable as exc:raise SourceUncertain(f'Сверка по кадрам невозможна: {exc}') from None
    session=vm._session();matches=[];checked=0
    try:
        for vid in ids[:MAX_CANDIDATES]:
            if cancel and cancel.is_set():raise SourceUncertain('Поиск отменён')
            try:
                info=vm.video_info(vid,session)
                # A reaction re-upload contains the same frames; never take it as the original.
                if REUPLOAD.search(info['title']):continue
                ev=vm.verify(info,clip,session)
            except vm.RateLimited:
                if matches:break
                raise SourceUncertain('YouTube ограничил запросы; повторите позже') from None
            except vm.StoryboardUnavailable:continue
            checked+=1
            if not vm.accepted(ev):continue
            matches.append({'id':vid,'title':info['title'],'channel':info['channel'],
                            'evidence':{'method':ev['method'],'matches':ev['matches'],'chance_level':ev['chance_level'],
                                        'coverage':ev['coverage'],'matched_frames':ev['matched_frames'],'storyboard_frames':ev['tiles'],
                                        'player_region':ev['region'],'video_span':ev['span']}})
            # A strong, clearly non-random match is enough; skip remaining downloads.
            if ev['coverage']>=0.3 and ev['matches']>=3*max(ev['chance_level'],1):break
    finally:session.close()
    if cancel and cancel.is_set():raise SourceUncertain('Поиск отменён')
    if not matches:raise SourceUncertain(f'Ни одно из проверенных видео ({checked}) не совпало по кадрам с нарезкой')
    # Stable sort: equal scores keep the order of the hint and search ranking.
    matches.sort(key=lambda c:c['evidence']['matches'],reverse=True)
    return matches


def resolve(row,segs,cancel=None,runner=None,verifier=None,video=None):
    """Find the original video. With video (the finished clip) candidates are checked by frames."""
    from reaction_previews import queries, youtube_video_id, SourceUncertain, PreviewError, words
    global _rate_limited_until
    visual=video is not None and verifier is None
    if not visual and time.monotonic()<_rate_limited_until:raise SourceUncertain('YouTube ограничил запросы; повторите позже')
    if cancel and cancel.is_set():raise SourceUncertain('Поиск отменён')
    if not visual and sum(len(tokens(s.get('text',''))) for s in segs if row['start']<=s.get('start',-1)<row['end'])<100:
        raise SourceUncertain('Недостаточно текста для проверки источника')
    runner=runner or subprocess.run;verifier=verifier or _candidate
    ids=[];hint=row.get('source_url_hint')
    if hint:
        try:ids.append(youtube_video_id(hint))
        except PreviewError:pass  # malformed hint is untrusted, never fetched directly
    query_list=[]
    hint_raw=str(row.get('source_search_hint',''))[:120]
    if re.search(r'@|https?://|\d{5,}|адрес|телефон|квартир|улиц|контакт',hint_raw,re.I):hint_raw=''
    hint_query=' '.join(words(hint_raw)[:12])
    # Names mentioned repeatedly within sentences can help discover videos whose
    # official title is in a different language. They are search hints only.
    from collections import Counter
    counts=Counter()
    for s in segs:
        if not row['start']<=s.get('start',-1)<row['end']:continue
        text=str(s.get('text',''))
        if re.search(r'адрес|телефон|квартир|улиц|контакт',text,re.I):continue
        counts.update(re.findall(r'(?<![.!?] )(?<!^)\b[А-ЯЁ][а-яё]{3,}\b',text))
    title_words=words(row.get('title',''))
    names=[w.lower().replace('ё','е') for w,n in counts.most_common() if n>=3 and w.lower().replace('ё','е') not in title_words and w.lower().replace('ё','е') in words(w)][:3]
    name_query=' '.join(title_words[:2]+names) if len(names)>=2 and len(title_words)>=2 else ''
    for q in ([hint_query] if hint_query else [])+([name_query] if name_query else [])+queries(row,segs):
        if len(q)>=8 and q not in query_list:query_list.append(q[:160])
    lists=[]
    for q in query_list[:6]:
        if cancel and cancel.is_set():raise SourceUncertain('Поиск отменён')
        cmd=[sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist','--flat-playlist','--dump-single-json','--extractor-args','youtube:lang=ru','--socket-timeout','10','ytsearch6:'+q]
        try:
            p=runner(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=35,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            data=json.loads(p.stdout) if p.returncode==0 else {}
            entries=[]
            for x in data.get('entries',[]):
                if not isinstance(x,dict):continue
                vid=x.get('id');title=x.get('title','')
                if not isinstance(vid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}',vid) or not isinstance(title,str):continue
                if re.search(r'\b(реакция|смотрит|реагирует|нарезка|трейлер|трейлеры|reaction|reacts|watches|trailer)\b',title,re.I):continue
                entries.append(vid)
            lists.append(entries)
        except (OSError,subprocess.SubprocessError,ValueError,TypeError):continue
    # Round-robin across queries ensures one noisy search does not exhaust budget.
    for rank in range(6):
        for entries in lists:
            if rank<len(entries) and entries[rank] not in ids:ids.append(entries[rank])
            if len(ids)>=MAX_CANDIDATES:break
        if len(ids)>=MAX_CANDIDATES:break
    if not ids:raise SourceUncertain('Подходящие кандидаты YouTube не найдены')
    if visual:return _resolve_visual(ids,video,cancel),query_list[:6]
    matches=[]
    for vid in ids[:MAX_CANDIDATES]:
        if cancel and cancel.is_set():raise SourceUncertain('Поиск отменён')
        try:
            result=verifier(vid,segs,row)
        except RateLimited:
            # Stop subsequent jobs in this process for 15 minutes; never retry a
            # partially compared set or turn an incomplete comparison into proof.
            _rate_limited_until=time.monotonic()+900
            raise SourceUncertain('YouTube ограничил запросы субтитров; проверка остановлена') from None
        if result:matches.append(result)
    if cancel and cancel.is_set():raise SourceUncertain('Поиск отменён')
    matches.sort(key=lambda c:c['evidence']['matches'],reverse=True)
    if not matches:raise SourceUncertain('Нет надёжного совпадения с доступными субтитрами')
    if len(matches)>1 and matches[1]['evidence']['matches']>=matches[0]['evidence']['matches']*.6:
        raise SourceUncertain('Найдено несколько похожих видео; невозможно уверенно выбрать оригинал')
    return matches,query_list[:6]
