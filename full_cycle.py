"""Pure timeline helpers for VOD -> reaction clips -> final-video chapters."""
from __future__ import annotations
import json
import math


def download_bounds(start, end):
    start, end = float(start), float(end)
    if not math.isfinite(start+end) or start < 0 or end <= start:
        raise ValueError('Некорректные границы реакции')
    return math.floor(start), math.ceil(end)


def trim_keeps(keeps, start, end):
    """Clip the silence detector's source-local keeps to exact reaction bounds."""
    out=[]
    for a,b in sorted((float(a),float(b)) for a,b in keeps):
        a,b=max(a,float(start)),min(b,float(end))
        if b <= a:
            continue
        if out and a < out[-1][1]-1e-6:
            raise ValueError('Перекрывающиеся участки монтажной карты')
        out.append((a,b))
    return out


def remap_segments(segments, source_offset, keeps):
    """Project absolute VOD speech timestamps to concatenated kept audio.

    One speech segment spanning several keeps retains its text only once.
    Word positions are not invented: coarse ASR segments remain coarse.
    """
    timeline=[];cursor=0.
    for a,b in keeps:
        if b <= a:
            raise ValueError('Пустой участок монтажной карты')
        timeline.append((float(a)+source_offset,float(b)+source_offset,cursor))
        cursor+=b-a
    result=[]
    for segment in segments:
        a,b=float(segment['start']),float(segment['end'])
        matches=[]
        for ka,kb,out_start in timeline:
            lo,hi=max(a,ka),min(b,kb)
            if hi > lo:
                matches.append((out_start+lo-ka,out_start+hi-ka))
        text=str(segment.get('text') or '').strip()
        if matches and text:
            row={'start':round(matches[0][0],6),'end':round(matches[-1][1],6),'text':text}
            if segment.get('timing_warning')=='end_clipped': row['timing_warning']='end_clipped'
            result.append(row)
    result.sort(key=lambda x:(x['start'],x['end']))
    return result,cursor


def clip_segments(segments, duration):
    return [dict(s,start=max(0.,float(s['start'])),end=min(float(duration),float(s['end'])))
            for s in segments if float(s['start']) < duration and float(s['end']) > 0 and
            min(float(duration),float(s['end']))>max(0.,float(s['start']))]


def chapters_ready(text):
    text=(text or '').strip()
    positions=[i for i,c in enumerate(text) if c in '{[']
    if not positions:
        return False
    try:
        obj,_=json.JSONDecoder().raw_decode(text[positions[0]:])
    except ValueError:
        return False
    return isinstance(obj,dict) and isinstance(obj.get('chapters'),list) and bool(obj['chapters'])


def atomic_json(path, data):
    import os,tempfile
    folder=os.path.dirname(os.path.abspath(path))
    fd,tmp=tempfile.mkstemp(prefix='.pipeline_',suffix='.tmp',dir=folder)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.remove(tmp)


def response_key(prompt, transcript, filename):
    import hashlib
    payload=json.dumps(['gpt-text-v29',prompt,transcript,filename],ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def cached_response(path, key, predicate=None):
    try:
        with open(path,encoding='utf-8') as f:obj=json.load(f)
        text=obj.get('text')
        if obj.get('key')!=key or not isinstance(text,str) or not text.strip():return None
        if predicate is not None and not predicate(text):return None
        return text
    except (OSError,ValueError,TypeError,AttributeError):return None


class Lookahead:
    """One ordered worker; at most one future media preparation ahead of current processing.

    Closing cancels work that has not started. A running download may finish and
    save its source, but cannot schedule more work or issue LLM requests.
    """
    def __init__(self,loader,total,enabled=True):
        self.loader=loader;self.total=total;self.index=0;self.future=None
        self.pool=None;self.enabled=enabled
    def __enter__(self):
        if self.enabled and self.total:
            from concurrent.futures import ThreadPoolExecutor
            self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='vod-prefetch')
            self.future=self.pool.submit(self.loader,0)
        return self
    def get(self,index):
        if index!=self.index:raise ValueError('Нарушен порядок очереди скачивания')
        if index>=self.total:raise IndexError(index)
        self.index+=1
        if self.pool is None:return self.loader(index)
        try:return self.future.result()
        finally:
            self.future=self.pool.submit(self.loader,self.index) if self.index<self.total else None
    def __exit__(self,*exc):
        if self.future:self.future.cancel()
        if self.pool:self.pool.shutdown(wait=False,cancel_futures=True)
        return False
