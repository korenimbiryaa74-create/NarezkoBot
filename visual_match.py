"""Visual source check: compare a reaction clip with a YouTube video's storyboard.

Storyboards are the public preview frames YouTube shows on the seek bar
(i.ytimg.com). Unlike captions they need no PO token or JS runtime, so the
check keeps working when YouTube answers 429 to /api/timedtext.

How it works: the clip is sampled densely (~0.5 s), the YouTube player is
located inside the stream frame by searching for the crop that best matches
the storyboard, and then the matched storyboard frames must appear in the
same order as in the video. Random matches are ruled out with a shuffle test.
A match is evidence, not proof of authorship or rights.
"""
from __future__ import annotations
import bisect
import json
import math
import random
import re
from urllib.parse import urlparse

UA=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/140.0 Safari/537.36')
DESC=(32,18)          # descriptor size
WORK_W=160            # clip frames are kept at this width (storyboard tiles are 160 px)
MATCH_THR=0.80        # correlation needed for a clip frame to count as a storyboard frame
MIN_ORDERED=10        # distinct storyboard frames that must appear in order
MIN_EXCESS=8          # ...and by how much they must beat shuffled (random) order
MIN_RATIO=2.0         # ...and how many times the shuffled order they must reach
MAX_PAGES=40
_cache={}

class StoryboardUnavailable(RuntimeError):
    pass

class RateLimited(RuntimeError):
    pass


def _session():
    import requests
    s=requests.Session();s.trust_env=False
    s.headers.update({'User-Agent':UA,'Accept-Language':'ru-RU,ru;q=0.9'})
    return s


def _player_response(html):
    i=html.find('ytInitialPlayerResponse')
    while i!=-1:
        j=html.find('{',i)
        if j==-1:break
        try:return json.JSONDecoder().raw_decode(html,j)[0]
        except ValueError:i=html.find('ytInitialPlayerResponse',i+1)
    raise StoryboardUnavailable('Нет данных плеера на странице видео')


def video_info(video_id,session=None):
    """Title, channel, duration and storyboard spec from the public watch page."""
    import requests
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',str(video_id)):raise StoryboardUnavailable('Неверный ID видео')
    own=session is None;session=session or _session()
    try:
        r=session.get('https://www.youtube.com/watch',params={'v':video_id,'hl':'ru'},timeout=(8,20),allow_redirects=False)
        if r.status_code==429:raise RateLimited('YouTube ограничил запросы страниц видео')
        if r.status_code!=200:raise StoryboardUnavailable(f'Страница видео: HTTP {r.status_code}')
        if len(r.content)>8_000_000:raise StoryboardUnavailable('Страница видео слишком большая')
        pr=_player_response(r.text)
    except requests.RequestException as exc:
        raise StoryboardUnavailable(f'Страница видео недоступна: {type(exc).__name__}') from None
    finally:
        if own:session.close()
    details=pr.get('videoDetails') or {}
    if details.get('videoId')!=video_id:
        status=(pr.get('playabilityStatus') or {}).get('status') or 'нет данных'
        raise StoryboardUnavailable(f'Видео недоступно ({status})')
    spec=((pr.get('storyboards') or {}).get('playerStoryboardSpecRenderer') or {}).get('spec')
    try:duration=float(details.get('lengthSeconds') or 0)
    except (TypeError,ValueError):duration=0.0
    return {'id':video_id,'title':str(details.get('title') or '')[:200],'channel':str(details.get('author') or '')[:120],
            'duration':duration,'spec':spec if isinstance(spec,str) else None}


def _levels(spec,duration):
    parts=spec.split('|');base=parts[0];out=[]
    for n,p in enumerate(parts[1:]):
        f=p.split('#')
        if len(f)<8:continue
        try:w,h,count,cols,rows,interval=(int(x) for x in f[:6])
        except ValueError:continue
        if f[6]=='default' or min(w,h,count,cols,rows)<=0:continue
        if interval<=0:interval=int(duration*1000/max(count,1))
        out.append({'level':n,'w':w,'h':h,'count':count,'cols':cols,'rows':rows,'interval':interval/1000.0,'name':f[6],'sigh':f[7]})
    return base,out


def storyboard(info,session=None,max_w=160):
    """Return (tiles, times): grayscale tiles in video order and their timestamps."""
    import cv2
    import numpy as np
    import requests
    key=(info['id'],max_w)
    if key in _cache:return _cache[key]
    if not info.get('spec'):raise StoryboardUnavailable('У видео нет раскадровки')
    base,levels=_levels(info['spec'],info.get('duration') or 0)
    levels=[l for l in levels if l['w']<=max_w] or levels
    if not levels:raise StoryboardUnavailable('Раскадровка в неизвестном формате')
    lv=max(levels,key=lambda l:l['w'])
    per=lv['cols']*lv['rows'];pages=min(MAX_PAGES,math.ceil(lv['count']/per))
    own=session is None;session=session or _session();tiles=[];times=[]
    try:
        for m in range(pages):
            url=base.replace('$L',str(lv['level'])).replace('$N',lv['name'].replace('$M',str(m)))
            url+=('&' if '?' in url else '?')+'sigh='+lv['sigh']
            u=urlparse(url)
            if u.scheme!='https' or not (u.hostname or '').endswith('.ytimg.com'):raise StoryboardUnavailable('Недоверенный адрес раскадровки')
            try:r=session.get(url,timeout=(8,15),allow_redirects=False)
            except requests.RequestException as exc:raise StoryboardUnavailable(f'Раскадровка недоступна: {type(exc).__name__}') from None
            if r.status_code==429:raise RateLimited('YouTube ограничил запросы раскадровки')
            if r.status_code!=200 or len(r.content)>5_000_000:break
            img=cv2.imdecode(np.frombuffer(r.content,np.uint8),cv2.IMREAD_GRAYSCALE)
            if img is None:break
            for k in range(per):
                idx=m*per+k
                if idx>=lv['count']:break
                y,x=(k//lv['cols'])*lv['h'],(k%lv['cols'])*lv['w']
                tile=img[y:y+lv['h'],x:x+lv['w']]
                if tile.shape[0]<lv['h']//2 or tile.shape[1]<lv['w']//2:break
                tiles.append(tile);times.append(idx*lv['interval'])
    finally:
        if own:session.close()
    if len(tiles)<5:raise StoryboardUnavailable('Раскадровка слишком короткая')
    _cache[key]=(tiles,times)
    return tiles,times


def _desc(img):
    import cv2
    import numpy as np
    v=cv2.resize(img,DESC,interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    v-=v.mean();n=float(np.linalg.norm(v))
    # Flat frames (black screens, fades) carry no evidence.
    return v/n if n>v.size**.5*3 else v*0


def _descs(frames,x,y,w,h):
    import numpy as np
    return np.stack([_desc(f[y:y+h,x:x+w]) for f in frames])


class Clip:
    """Densely sampled grayscale frames of a finished clip (shared by all candidates)."""
    def __init__(self,video,step=0.5,max_frames=6000,cancel=None):
        import cv2
        cap=cv2.VideoCapture(str(video))
        try:
            if not cap.isOpened():raise StoryboardUnavailable('Не удалось открыть видео нарезки')
            fps=cap.get(cv2.CAP_PROP_FPS) or 30.0;count=cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            duration=count/fps if fps>0 else 0
            if duration<10:raise StoryboardUnavailable('Нарезка слишком короткая для сверки')
            step=max(step,duration/max_frames);every=max(1,int(round(step*fps)));frames=[];n=0
            # Sequential grab is much faster than seeking: only sampled frames are decoded to images.
            while True:
                if n%600==0 and cancel and cancel.is_set():raise StoryboardUnavailable('Поиск отменён')
                if not cap.grab():break
                if n%every==every//2:
                    ok,frame=cap.retrieve()
                    if ok and frame is not None:
                        g=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
                        frames.append(cv2.resize(g,(WORK_W,max(1,round(g.shape[0]*WORK_W/g.shape[1]))),interpolation=cv2.INTER_AREA))
                n+=1
        finally:cap.release()
        if len(frames)<20:raise StoryboardUnavailable('Не удалось прочитать кадры нарезки')
        self.frames=frames;self.step=step
        self.H,self.W=frames[0].shape[:2]
        self.sample=frames[::max(1,len(frames)//240)][:240]
        self._coarse={}

    def coarse(self,aspect):
        """Candidate player rectangles on a coarse grid, with descriptors of the sample frames."""
        import numpy as np
        key=round(aspect,3)
        if key in self._coarse:return self._coarse[key]
        out=[]
        for frac in np.arange(1.0,0.33,-0.07):
            w=int(round(self.W*frac));h=int(round(w*aspect))
            if h>self.H:h=self.H;w=int(round(h/aspect))
            if w<40:continue
            sx=max(3,w//8);sy=max(3,h//8)
            for y in sorted(set(list(range(0,self.H-h+1,sy))+[self.H-h])):
                for x in sorted(set(list(range(0,self.W-w+1,sx))+[self.W-w])):
                    out.append(((x,y,w,h),_descs(self.sample,x,y,w,h).astype(np.float16)))
        self._coarse[key]=out
        return out


def _hits(D,T):
    s=(D.astype('float32')@T.T).max(1)
    return float((s>=MATCH_THR).sum())+float(s.mean())


def _locate(clip,T,aspect):
    """Find where the YouTube player sits in the stream frame (x,y,w,h)."""
    coarse=clip.coarse(aspect)
    ranked=sorted(((_hits(D,T),r) for r,D in coarse),reverse=True)[:4]
    best=ranked[0]
    for _,(x,y,w,h) in ranked:
        for dw in (-6,-3,0,3,6):
            ww=w+dw;hh=int(round(ww*aspect))
            if ww<40 or ww>clip.W or hh>clip.H:continue
            for dy in range(-6,7,2):
                for dx in range(-6,7,2):
                    xx=min(max(0,x+dx-dw//2),clip.W-ww);yy=min(max(0,y+dy-(hh-h)//2),clip.H-hh)
                    sc=_hits(_descs(clip.sample,xx,yy,ww,hh),T)
                    if sc>best[0]:best=(sc,(xx,yy,ww,hh))
    return best[1]


def _ordered(seq):
    """Distinct storyboard frames in the longest chain that never goes back in video time."""
    tails=[];idx=[];prev=[None]*len(seq)
    for i,tile in enumerate(seq):
        j=bisect.bisect_right(tails,tile)
        if j==len(tails):tails.append(tile);idx.append(i)
        else:tails[j]=tile;idx[j]=i
        prev[i]=idx[j-1] if j else None
    chain=[];k=idx[-1] if idx else None
    while k is not None:chain.append(seq[k]);k=prev[k]
    return chain[::-1]


def compare(clip,tiles,times):
    import numpy as np
    th,tw=tiles[0].shape[:2];aspect=th/tw
    T=np.stack([_desc(t) for t in tiles])
    x,y,w,h=_locate(clip,T,aspect)
    S=None
    for dx,dy in ((0,0),(-1,0),(1,0),(0,-1),(0,1)):
        xx=min(max(0,x+dx),clip.W-w);yy=min(max(0,y+dy),clip.H-h)
        s=_descs(clip.frames,xx,yy,w,h)@T.T
        S=s if S is None else np.maximum(S,s)
    best=S.max(1);tile=S.argmax(1)
    seq=[int(tile[i]) for i in range(len(best)) if best[i]>=MATCH_THR]
    chain=_ordered(seq);ordered=len(set(chain))
    # Shuffle test: the same matches in random order give the chance level.
    rnd=random.Random(1234);null=[]
    for _ in range(30):
        sh=seq[:];rnd.shuffle(sh);null.append(len(set(_ordered(sh))))
    null.sort();chance=null[int(len(null)*.95)-1] if null else 0
    return {'matches':ordered,'chance_level':chance,'matched_frames':len(seq),'frames':len(clip.frames),'tiles':len(tiles),
            'coverage':round(ordered/len(tiles),3),'region':[int(x),int(y),int(w),int(h)],'frame_size':[clip.W,clip.H],
            'span':[round(times[chain[0]],1),round(times[chain[-1]],1)] if chain else None}


def verify(info,clip,session=None):
    tiles,times=storyboard(info,session)
    ev=compare(clip,tiles,times)
    ev['method']='сверка кадров нарезки с раскадровкой YouTube'
    return ev


def accepted(ev):
    m=ev.get('matches',0);c=ev.get('chance_level',0)
    return m>=MIN_ORDERED and m-c>=MIN_EXCESS and m>=MIN_RATIO*max(c,1)
