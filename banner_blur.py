"""Experimental, local visual detection of advertising overlays for video masking.

Samples cannot guarantee coverage between frames or semantic correctness. Never
pretend a missing model, invalid response or decoding error means no banners.
"""
from __future__ import annotations
import base64
import json
import math
import subprocess
import urllib.request
from urllib.error import URLError, HTTPError

MODEL='qwen3-vl:4b'
INTERVAL=5.0
PROMPT='''Find advertising or promotional overlays that a streamer has placed on top of the video: sponsor banners, promotional codes, gambling ads, or promotional stickers, including animated ad characters. Return one bounding box tightly surrounding EACH entire visible overlay, including its characters and text. Do not include the streamer webcam, the chat, subtitles, video player controls, game interface or content of the video itself. If there is no clear promotional overlay, return an empty list. Coordinates are integers [x1,y1,x2,y2] on a 0–1000 grid relative to the full image; origin top left. Return only JSON: {"banners":[[x1,y1,x2,y2]]}.'''
SCHEMA={'type':'object','properties':{'banners':{'type':'array','maxItems':8,'items':{'type':'array','minItems':4,'maxItems':4,'items':{'type':'integer','minimum':0,'maximum':1000}}}},'required':['banners'],'additionalProperties':False}
class BannerError(RuntimeError): pass
class BannerSetupError(BannerError): pass
class BannerResponseError(BannerError): pass

def parse_boxes(text):
    # Never publish response text from frames; only report structural problems.
    if not isinstance(text,str) or not text.strip():
        raise BannerResponseError('Модель вернула пустой ответ о баннерах')
    try: value=json.loads(text)
    except (ValueError,TypeError):
        raise BannerResponseError('Модель вернула неразборчивый JSON с областями баннеров') from None
    if not isinstance(value,dict) or 'banners' not in value:
        raise BannerResponseError('В ответе модели отсутствует список banners')
    boxes=value['banners']
    if not isinstance(boxes,list):raise BannerResponseError('Поле banners в ответе модели не является списком')
    if len(boxes)>8:raise BannerResponseError(f'Модель вернула слишком много областей: {len(boxes)}, максимум 8')
    result=[]
    for i,box in enumerate(boxes,1):
        if not isinstance(box,list) or len(box)!=4:
            raise BannerResponseError(f'Область {i}: ожидаются четыре координаты [x1,y1,x2,y2]')
        if any(isinstance(x,bool) or not isinstance(x,(int,float)) for x in box):
            raise BannerResponseError(f'Область {i}: координаты должны быть числами')
        if any(x<0 or x>1000 or (isinstance(x,float) and not math.isfinite(x)) for x in box):
            raise BannerResponseError(f'Область {i}: координаты вне шкалы 0–1000')
        # Integral and fractional coordinates use the same 0–1000 grid. Outward
        # rounding prevents exposing an edge. We never infer a 0–1 scale.
        x1,y1,x2,y2=box
        if x2<=x1 or y2<=y1:raise BannerResponseError(f'Область {i}: конец не больше начала')
        if (x2-x1)*(y2-y1)>400000:
            raise BannerResponseError(f'Область {i}: занимает более 40% кадра; автоматический блюр остановлен')
        x1=math.floor(x1);y1=math.floor(y1);x2=math.ceil(x2);y2=math.ceil(y2)
        px=max(8,round((x2-x1)*.12));py=max(8,round((y2-y1)*.12))
        result.append([max(0,x1-px),max(0,y1-py),min(1000,x2+px),min(1000,y2+py)])
    return result

def request_boxes(image, progress=None, *, _retry=False):
    req={'model':MODEL,'stream':False,'think':False,'format':SCHEMA,'options':{'temperature':0,'num_ctx':8192 if _retry else 4096,'num_predict':1024 if _retry else 512},
         'messages':[{'role':'user','content':PROMPT,'images':[base64.b64encode(image).decode('ascii')]}]}
    payload=json.dumps(req).encode('utf-8')
    request=urllib.request.Request('http://127.0.0.1:11434/api/chat',data=payload,headers={'Content-Type':'application/json'},method='POST')
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=180) as response:
            raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('oversize')
        data=json.loads(raw);text=data['message']['content']
    except HTTPError as exc:
        # Ollama error text is untrusted: match known conditions but do not log it.
        try:
            raw_error=exc.read(8192)
            data=json.loads(raw_error)
            reason=str(data.get('error','')).lower() if isinstance(data,dict) else ''
        except (OSError,ValueError,TypeError): reason=''
        if exc.code==404 and 'model' in reason and ('not found' in reason or 'not available' in reason):
            raise BannerSetupError('Модель qwen3-vl:4b не найдена в Ollama. После обновления проекта запустите INSTALL.bat для загрузки модели') from None
        if exc.code==404:
            raise BannerSetupError('Ollama вернула HTTP 404. Проверьте версию Ollama и наличие модели qwen3-vl:4b') from None
        if exc.code==500 and ('out of memory' in reason or 'requires more system memory' in reason or 'not enough memory' in reason):
            raise BannerSetupError('Ollama: недостаточно памяти для модели баннеров. Закройте тяжёлые программы и проверьте свободную ОЗУ и видеопамять') from None
        kind=BannerSetupError if 400<=exc.code<500 else BannerError
        raise kind(f'Ollama вернула HTTP {exc.code} при запуске модели баннеров; проверьте Ollama и модель qwen3-vl:4b') from None
    except URLError as exc:
        if isinstance(exc.reason,ConnectionRefusedError) or getattr(exc.reason,'winerror',None)==10061:
            raise BannerSetupError('Нет соединения с локальным Ollama (порт 11434). Запустите Ollama и повторите задание') from None
        if isinstance(exc.reason,TimeoutError):
            raise BannerError('Ollama не ответила за 180 с при поиске баннеров') from None
        raise BannerError('Не удалось связаться с локальным Ollama; проверьте, что он запущен') from None
    except TimeoutError:
        raise BannerError('Ollama не ответила за 180 с при поиске баннеров') from None
    except OSError:
        raise BannerError('Не удалось связаться с локальным Ollama; проверьте, что он запущен') from None
    except (ValueError,KeyError,TypeError):
        raise BannerError('Ollama вернула некорректный ответ модели баннеров; проверьте версию Ollama и модель qwen3-vl:4b') from None
    try:
        return parse_boxes(text)
    except BannerResponseError as exc:
        if data.get('done_reason')=='length':
            if not _retry:
                if progress:progress('Ответ модели обрезан; повторяю этот кадр с увеличенным контекстом и лимитом ответа')
                return request_boxes(image,progress,_retry=True)
            # Numeric counts help distinguish context pressure from runaway output,
            # without logging model text or anything from the frame.
            counts=[]
            for field,label in (('prompt_eval_count','вход'),('eval_count','ответ')):
                v=data.get(field)
                if type(v) is int and 0<=v<=10000000:counts.append(f'{label}: {v} токенов')
            details=f" ({', '.join(counts)})" if counts else ''
            raise BannerResponseError('Ответ модели о баннерах снова обрезан после повтора с увеличенным контекстом и лимитом ответа'+details+'; рендер остановлен') from None
        raise exc from None

def scan(src, keeps, ffmpeg, progress=None, interval=INTERVAL):
    if not math.isfinite(interval) or interval<=0:raise BannerError('Неверный интервал анализа')
    total=sum(math.ceil((b-a)/interval) for a,b in keeps)
    results=[];index=0
    for start,end in keeps:
        t=start
        while t<end-0.0001:
            stop=min(end,t+interval); middle=(t+stop)/2; index+=1
            if progress and (index<=3 or index%10==0 or index==total):progress(f'Поиск баннеров: кадр {index}/{total} · время исходного фрагмента {int(middle//60):02}:{int(middle%60):02}')
            cmd=[ffmpeg,'-hide_banner','-loglevel','error','-ss',f'{middle:.3f}','-i',str(src),'-frames:v','1','-vf','scale=960:-2:force_original_aspect_ratio=decrease','-f','image2pipe','-vcodec','mjpeg','pipe:1']
            try:r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except (subprocess.SubprocessError,OSError) as exc: raise BannerError('Не удалось извлечь кадр для поиска баннеров') from exc
            if not r.stdout:raise BannerError('Не удалось извлечь кадр для поиска баннеров')
            boxes=request_boxes(r.stdout,progress)
            for box in boxes:results.append({'start':t,'end':stop,'box':box})
            t=stop
    return results

def _iou(a,b):
    l=max(a[0],b[0]);t=max(a[1],b[1]);r=min(a[2],b[2]);bot=min(a[3],b[3]);inter=max(0,r-l)*max(0,bot-t)
    return inter/((a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter) if inter else 0

def timed_regions(detections, start, end, width, height):
    """Group similar rectangles, map to current render chunk (relative times)."""
    clusters=[]
    for d in detections:
        a=max(start,d['start']);b=min(end,d['end'])
        if b<=a:continue
        box=d['box']; best=None
        for c in clusters:
            if _iou(c['box'],box)>=.7:
                union=[min(c['box'][0],box[0]),min(c['box'][1],box[1]),max(c['box'][2],box[2]),max(c['box'][3],box[3])]
                area=(union[2]-union[0])*(union[3]-union[1]); orig=(box[2]-box[0])*(box[3]-box[1])
                if area<=orig*1.6: best=c;break
        if best is None:
            best={'box':list(box),'times':[]};clusters.append(best)
        else:
            best['box']=union
        best['times'].append((a-start,b-start))
    if len(clusters)>64:raise BannerError('Слишком много разных областей баннеров для рендера; задача остановлена')
    result=[]
    for c in clusters:
        x1,y1,x2,y2=c['box'];x=math.floor(x1*width/1000);y=math.floor(y1*height/1000)
        x=x//2*2;y=y//2*2
        x2=min(width,math.ceil(x2*width/1000));y2=min(height,math.ceil(y2*height/1000))
        w=(x2-x)//2*2;h=(y2-y)//2*2
        if w<4 or h<4:raise BannerError('Область баннера слишком мала для размытия')
        times=[]
        for a,b in sorted(c['times']):
            if times and a<=times[-1][1]+.001:times[-1]=(times[-1][0],max(times[-1][1],b))
            else:times.append((a,b))
        result.append((x,y,w,h,times))
    return result

def filter_for_chunk(regions, strength=20):
    if not regions:return None
    # Each overlay is only active in its sampled interval. Output timestamps in
    # these precise single-range chunks start at zero.
    parts=[];prev='[bin]'
    for i,(x,y,w,h,times) in enumerate(regions):
        radius=max(1,min(strength,40,w//4,h//4))
        parts.append(f'{prev}split=2[b{i}][s{i}]')
        parts.append(f'[s{i}]crop={w}:{h}:{x}:{y},boxblur={radius}:2[r{i}]')
        enabled='+'.join(f'between(t,{a:.3f},{b:.3f})' for a,b in times)
        out=f'[o{i}]' if i<len(regions)-1 else '[bout]'
        parts.append(f"[b{i}][r{i}]overlay={x}:{y}:enable='{enabled}'{out}")
        prev=out
    # A filter chain uses anonymous in/out in -vf or after a filter graph label.
    return ';'.join(parts).replace('[bin]','',1).removesuffix('[bout]')
