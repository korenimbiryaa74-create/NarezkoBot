"""Experimental reaction previews, source matching and local compositing.

Caption matches are evidence, not proof of authorship. No upload is made.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse, parse_qs

class PreviewError(RuntimeError): pass
class SourceUncertain(PreviewError): pass

GENERIC=set('муж жена мужем женой мужа жену супруг супруги семья семейный семейные семьями семейных развод разводе партнеры партнерами эксперимент отношения жизни история женщина мужчина люди конфликт выезд полиция полицейские вызов'.split())
STOP=set('это этот эта эти что как где кто когда какой такая такой очень просто после перед будет были можно надо который ролик видео реакция смотрит смотрю стример стрим стриме каша nix nikel test они она его еще если так уже вот там тут они для вас есть или ещё все нет всё это тот the and for with that this from you your on in is to of a'.split())
def words(s):
    return [x for x in re.findall(r'[a-zа-яё0-9]+',str(s).lower().replace('ё','е')) if len(x)>2 and x not in STOP]

def queries(row,segs):
    # Only short, locally selected text is transmitted to YouTube search.
    title=' '.join(words(row.get('title',''))[:12]); snippets=[]
    a=float(row['start']);b=float(row['end'])
    candidates=[s for s in segs if a<=float(s.get('start',-1))<b and isinstance(s.get('text'),str)]
    for s in candidates[:15]:
        w=words(s['text'])
        if len(w)>=4:
            snippets.append(' '.join(w[:8]))
    # A spoken title can occur minutes into the reaction, not only at its start.
    # Short number + immediately following short utterance is a useful hint;
    # keep this generic and bounded rather than hard-coding a particular show.
    anchors=[]
    for i,s in enumerate(candidates):
        if len(anchors)>=2: break
        nums=re.findall(r'(?<!\d)\d{3,4}(?!\d)',s['text'])
        if not nums or i+1>=len(candidates) or len(words(s['text']))>4: continue
        nxt=candidates[i+1]
        delta=float(nxt.get('start',0))-float(s.get('start',0))
        following=words(nxt['text'])
        private={'адрес','улица','дом','корпус','квартира','квартиры','телефон','номер'}
        if 0<=delta<=5 and 1<=len(following)<=4 and not private.intersection(words(s['text'])+following):
            anchors.append(nums[0]+' '+' '.join(following))
    out=[]
    for q in anchors+[title]+snippets[:2]:
        if len(q)>=8 and q not in out:out.append(q)
    return out

def find_candidates(row,segs,runner=None,cancel=None):
    runner=runner or subprocess.run; collected={}; qs=queries(row,segs)
    if not qs: raise PreviewError('Недостаточно текста для поиска исходного видео')
    for q in qs:
        if cancel and cancel.is_set():raise PreviewError('Тест превью отменён')
        cmd=[sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist','--flat-playlist','--dump-single-json','--extractor-args','youtube:lang=ru','--socket-timeout','15','ytsearch6:'+q]
        try:
            r=runner(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=80,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if r.returncode: continue
            data=json.loads(r.stdout)
            for e in data.get('entries',[]):
                if not isinstance(e,dict): continue
                vid=e.get('id'); title=e.get('title')
                if not isinstance(vid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}',vid) or not isinstance(title,str):continue
                if re.search(r'\b(реакция|смотрит|реагирует|нарезка|трейлер|трейлеры|reaction|reacts|watches|trailer)\b',title,re.I):continue
                if vid not in collected:collected[vid]={'id':vid,'title':title[:200],'channel':str(e.get('channel') or e.get('uploader') or '')[:120]}
        except (OSError,subprocess.SubprocessError,ValueError,TypeError): continue
    if not collected: raise SourceUncertain('YouTube не вернул подходящих кандидатов; источник не определён')
    title_words=set(words(row.get('title','')))
    transcript_words=Counter(w for s in segs if row['start']<=s.get('start',-1)<row['end'] for w in words(s.get('text','')))
    for c in collected.values():
        cw=set(words(c['title'])); hits=title_words & cw
        # Reward exact, multiword spoken title hints with a shared number.
        # Ordinary generic transcript words must not outweigh a strong title hint.
        hint_hits=[]
        for q in qs:
            if not re.match(r'^\d{3,4}\s',q): continue
            qw=set(words(q)); overlap=qw & cw
            if len(overlap)>=3 and any(w.isdigit() for w in overlap):
                if len(overlap)>len(hint_hits):hint_hits=sorted(overlap)
        c['score']=round(len(hits)*3+sum(min(transcript_words[w],3) for w in cw)*.2-max(0,len(cw-hits))*0.1+12*len(hint_hits),2)
        c['title_hint_words']=hint_hits
        c['shared_words']=sorted(hits)
        c['transcript_words']=sorted(cw & set(transcript_words))
    result=sorted(collected.values(),key=lambda c:(-c['score'],c['id']))
    # Topic-level word overlap is not proof: the old threshold picked unrelated
    # videos (e.g. any YouTube video about views). Require an ordered spoken
    # title phrase or the more specific number+title anchor, and still mark the
    # resulting candidate as unverified.
    spoken=[w for s in segs if row['start']<=s.get('start',-1)<row['end'] for w in words(s.get('text',''))]
    spoken_phrases={tuple(spoken[i:i+4]) for i in range(max(0,len(spoken)-3))}
    strong=[]
    for c in result:
        cw=words(c['title'])
        phrases=[cw[i:i+4] for i in range(max(0,len(cw)-3)) if tuple(cw[i:i+4]) in spoken_phrases]
        c['spoken_title_phrase']=' '.join(phrases[0]) if phrases else ''
        if c['title_hint_words'] or phrases:strong.append(c)
    if len(strong)!=1:
        raise SourceUncertain('Оригинал не подтверждён: совпадения по теме недостаточно. Можно указать ссылку вручную.')
    result=[strong[0]]+[c for c in result if c is not strong[0]]
    return result,qs

def download_thumbnail(video_id, session=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):raise PreviewError('Некорректный ID YouTube')
    import requests
    from io import BytesIO
    from PIL import Image,ImageOps,UnidentifiedImageError
    own=session is None
    session=session or requests.Session();session.trust_env=False
    try:
        # hq720 is the current YouTube display thumbnail; maxresdefault can be
        # a stale auto-selected video frame even when a custom thumbnail exists.
        for quality in ('hq720','maxresdefault','sddefault','hqdefault'):
            try:
                url=f'https://i.ytimg.com/vi/{video_id}/{quality}.jpg'
                with session.get(url,timeout=(10,30),stream=True,allow_redirects=False) as r:
                    if r.status_code!=200:continue
                    body=bytearray()
                    for chunk in r.iter_content(65536):
                        body.extend(chunk)
                        if len(body)>12_000_000:raise PreviewError('Изображение источника слишком большое')
                with Image.open(BytesIO(body)) as im:
                    if im.width<320 or im.height<180 or im.width*im.height>40_000_000:continue
                    image=ImageOps.exif_transpose(im).convert('RGB'); image.load()
                    return image,url
            except (requests.RequestException,OSError,UnidentifiedImageError):continue
    finally:
        if own:session.close()
    raise PreviewError('Не удалось загрузить изображение найденного видео')

def youtube_video_id(url):
    try:
        parsed=urlparse(str(url).strip())
        if parsed.scheme!='https' or parsed.username is not None or parsed.password is not None or parsed.port not in (None,443):raise ValueError()
        host=(parsed.hostname or '').lower()
        if host in ('youtube.com','www.youtube.com','m.youtube.com') and parsed.path=='/watch':
            values=parse_qs(parsed.query,keep_blank_values=True).get('v',[])
            if len(values)!=1:raise ValueError()
            video_id=values[0]
        elif host=='youtu.be' and re.fullmatch(r'/[A-Za-z0-9_-]{11}',parsed.path):video_id=parsed.path[1:]
        else:raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):raise ValueError()
        return video_id
    except (ValueError,TypeError):raise PreviewError('Нужна корректная ссылка на YouTube-видео (https://www.youtube.com/watch?v=... или youtu.be/...)') from None

def font(size):
    from PIL import ImageFont
    for p in (Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'arialbd.ttf',Path('/usr/share/fonts/msttcore/arialbd.ttf'),Path('/usr/share/fonts/liberation/LiberationSans-Bold.ttf')):
        if p.is_file():return ImageFont.truetype(str(p),size)
    raise PreviewError('Не найден жирный шрифт Arial; нужен шрифт с кириллицей')

def choose_cutout(root,login,row):
    if not re.fullmatch(r'[a-z0-9_]{1,25}',str(login)):raise PreviewError('Укажите Twitch-логин стримера для превью')
    folder=Path(root)/'thumbnail_assets'/login
    if not folder.is_dir() or folder.is_symlink():raise PreviewError(f'Нет папки вырезов thumbnail_assets/{login}')
    files=sorted(p for p in folder.glob('*.png') if p.is_file() and not p.is_symlink())
    if not files:raise PreviewError(f'Нет PNG-вырезов в thumbnail_assets/{login}')
    key=f"{row.get('title','')}|{row.get('start')}".encode('utf8')
    return files[int.from_bytes(hashlib.sha256(key).digest()[:4],'big')%len(files)]

def render_preview(background,cutout,label,destination):
    from PIL import Image,ImageDraw,ImageFilter,ImageOps
    label=re.sub(r'\s+',' ',str(label)).strip().upper()
    if not label or len(label)>80:raise PreviewError('Нет короткой надписи ChatGPT для превью')
    if background.width<320 or background.height<180:raise PreviewError('Изображение источника слишком маленькое')
    with Image.open(cutout) as src:
        if src.width*src.height>40_000_000:raise PreviewError('Вырез слишком большой')
        person=src.convert('RGBA');person.load()
    if person.getchannel('A').getextrema()==(255,255):raise PreviewError('Вырез должен быть PNG с прозрачным фоном')
    W,H=1280,720
    bg=ImageOps.fit(background.convert('RGB'),(W,H),method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(24)).convert('RGBA')
    bg=Image.alpha_composite(bg,Image.new('RGBA',(W,H),(45,0,0,185)))
    image=ImageOps.fit(background.convert('RGB'),(790,444),method=Image.Resampling.LANCZOS)
    bg.paste(image,(10,10)); d=ImageDraw.Draw(bg);d.rectangle((8,8,802,456),outline='white',width=4)
    scale=min(720/person.height,740/person.width);person=person.resize((max(1,round(person.width*scale)),max(1,round(person.height*scale))),Image.Resampling.LANCZOS)
    bg.alpha_composite(person,(W-person.width,H-person.height))
    d=ImageDraw.Draw(bg);available=735
    for size in range(118,29,-2):
        f=font(size); lines=[];line=''
        for word in label.split():
            new=(line+' '+word).strip()
            if line and d.textbbox((0,0),new,font=f,stroke_width=4)[2]>available:lines.append(line);line=word
            else:line=new
        if line:lines.append(line)
        if len(lines)<=2 and all(d.textbbox((0,0),x,font=f,stroke_width=4)[2]<=available for x in lines) and len(lines)*(size+8)<=236:break
    else:raise PreviewError('Надпись не помещается: сократите текст')
    for i,line in enumerate(lines):
        d.text((24,470+i*(size+8)),line,font=f,fill=('#ffe100' if i==0 else '#ffffff'),stroke_width=5,stroke_fill='#180000')
    dest=Path(destination);dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.preview-',suffix='.jpg',dir=dest.parent);os.close(fd)
    try:
        bg.convert('RGB').save(tmp,'JPEG',quality=92,optimize=True)
        if dest.exists():raise PreviewError('Превью уже существует; не перезаписываю')
        if os.name=='nt':os.rename(tmp,dest)
        else:os.link(tmp,dest)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return dest

def choose_style(root,login):
    if not re.fullmatch(r'[a-z0-9_]{1,25}',str(login)):raise PreviewError('Укажите Twitch-логин стримера для превью')
    folder=Path(root)/'thumbnail_styles'
    files=[folder/(login+ext) for ext in ('.png','.jpg','.jpeg') if (folder/(login+ext)).is_file() and not (folder/(login+ext)).is_symlink()]
    if len(files)!=1:raise PreviewError(f'Нужен один пример стиля: thumbnail_styles/{login}.jpg или .png')
    return files[0]


def resolve_source(row,segs,cancel=None,video=None):
    import source_match
    return source_match.resolve(row,segs,cancel=cancel,video=video)


def generate(root,login,row,segs,output,cancel=None,status=None,source_url=None,video=None):
    if row.get('type')!='reaction':raise PreviewError('Превью пока только для реакций')
    label=row.get('preview_text') or ''
    if not isinstance(label,str) or not label.strip():raise PreviewError('Нет надписи для превью в ответе ChatGPT; проверьте ответ вручную')
    import preview_local, preview_render
    cutout=choose_cutout(root,login,row)
    # The style example only sets background colours now; it is optional.
    try:style=choose_style(root,login)
    except PreviewError:style=None
    warnings=['Проверьте найденный источник и композицию. Надпись и вырез стримера наложены программой, без модели.']
    if len(label)>32 or len(label.split())>4:
        warnings.append('Надпись длиннее 4 слов или 32 символов: проверьте и сократите вручную')
    synthetic=False
    if source_url:
        source={'id':youtube_video_id(source_url),'title':'Ссылка указана пользователем','channel':''}
        candidates=[source];queries_used=[]
    else:
        try:
            if row.get('_source_checked'):
                candidates=row.get('_resolved_candidates') or []
                queries_used=row.get('_source_queries') or []
                if not candidates:raise SourceUncertain('Источник не подтверждён')
            else:
                if status:status('Ищу исходное видео и сверяю кадры нарезки с раскадровкой YouTube')
                candidates,queries_used=resolve_source(row,segs,cancel=cancel,video=video)
            source=candidates[0]
        except SourceUncertain:
            if not isinstance(row.get('title'),str) or not row['title'].strip():raise
            synthetic=True;source=None;candidates=[];queries_used=[]
            warnings[0]='Проверьте иллюстрацию и композицию. Надпись и вырез стримера наложены программой.'
            warnings.append('Источник не определён. В рамке сгенерированная иллюстрация по теме, не оригинальное превью. Проверьте её содержание.')
            if status:status('Источник не определён; генерирую иллюстрацию по теме, не оригинал')
    if cancel and cancel.is_set():raise PreviewError('Тест превью отменён')
    if synthetic:
        # The model only paints the illustration inside the frame: no people, no text.
        preview_local.check_install(root)
        from PIL import Image
        tmp=Path(output).with_name('.illustration-'+os.urandom(6).hex()+'.jpg');url=None
        try:
            ref=style or Image.new('RGB',(1024,576),(70,4,10))
            preview_local.generate_reaction(root,ref,ref,None,label,tmp,progress=status or (lambda _:None),cancel=cancel,context=row['title'],raw=True)
            with Image.open(tmp) as im:image=im.convert('RGB');image.load()
        finally:tmp.unlink(missing_ok=True)
    else:
        image,url=download_thumbnail(source['id'])
    if cancel and cancel.is_set():raise PreviewError('Тест превью отменён')
    if status:status('Собираю превью: кадр, вырез стримера и надпись')
    try:preview_render.render(image,cutout,label,output,style=style)
    except preview_render.RenderError as exc:raise PreviewError(str(exc)) from None
    return {'status':'требует проверки','source_url':None if synthetic else 'https://www.youtube.com/watch?v='+source['id'],
            'image_url':url,'source_title':'Иллюстрация по теме' if synthetic else source['title'],'source_channel':'' if synthetic else source['channel'],
            'search_queries':queries_used,'candidates':candidates[:6],
            'cutout':cutout.name,'style_reference':style.name if style else None,'preview_text':label,'warnings':warnings,
            'generator':('FLUX 3 (иллюстрация) + ' if synthetic else '')+'программная сборка: кадр, вырез, надпись', 'text_unverified':False, 'source_manually_selected':bool(source_url), 'source_generated':synthetic,
            'source_evidence':None if synthetic or source_url else source.get('evidence')}


def _cached_label(root, row):
    """Recover old rows only from one unambiguous cache match; never call GPT."""
    import reactions
    matches=[]
    for path in (Path(root)/'gpt_uploads'/'response_cache').glob('*.json'):
        try:
            if path.is_symlink() or path.stat().st_size>5_000_000:continue
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            answer=reactions.parse_reaction_answer(data['text'])
            for r in answer['reactions']:
                if not isinstance(r,dict) or r.get('type')!='reaction':continue
                a,b=reactions.reaction_time(r.get('start')),reactions.reaction_time(r.get('end'))
                if a is None or b is None:continue
                if abs(a-float(row['start']))>.005 or abs(b-float(row['end']))>.005:continue
                if reactions.tc.clean_title(r.get('title'))!=row.get('title'):continue
                label=r.get('preview_text')
                if isinstance(label,str) and label.strip():matches.append(label.strip())
        except (OSError,ValueError,TypeError,KeyError,reactions.ReactionsError):continue
    if len(matches)==1:return matches[0]
    if len(matches)>1:raise PreviewError('Найдено несколько совпадающих ответов ChatGPT; нельзя надёжно выбрать надпись')
    raise PreviewError('Для этой нарезки не найдена сохранённая надпись preview_text. Используйте нарезку, созданную с новой версией бота')


def prepare_saved_clip(root, video, test_login=''):
    """Resolve one original, unmodified pipeline clip and its saved transcript."""
    root=Path(root).resolve(); video=Path(video)
    if video.is_symlink() or not video.is_file() or video.suffix.lower()!='.mp4':
        raise PreviewError('Выберите готовую MP4-нарезку бота')
    video=video.resolve()
    try:video.relative_to(root/'recordings')
    except ValueError:raise PreviewError('Выберите исходный файл нарезки в папке recordings, не перемещённую копию') from None
    directory=video.parent
    job=directory.parent if directory.name in ('Готовые нарезки','Готовые без тишины') else directory
    meta_path=job/(video.stem+'.pipeline.json'); index_path=job/'reactions.json'
    try:
        meta=json.loads(meta_path.read_text(encoding='utf-8-sig'))
        index=json.loads(index_path.read_text(encoding='utf-8-sig'))
        row=meta['reaction']; rows=index['reactions']
        if not isinstance(row,dict) or not isinstance(rows,list) or rows.count(row)!=1 or meta['vod_id']!=index['vod_id']:
            raise ValueError('mismatch')
        if row.get('type')!='reaction':raise PreviewError('Эта нарезка не отмечена как реакция; превью пока только для реакций')
        a,b=float(row['start']),float(row['end']); duration=float(meta['duration'])
        if not all(math.isfinite(x) for x in (a,b,duration)) or b<=a or duration<=0:raise ValueError('time')
        if not isinstance(row.get('title'),str) or not row['title']:raise ValueError('title')
    except PreviewError:raise
    except (OSError,ValueError,TypeError,KeyError):
        raise PreviewError('Метаданные этой нарезки не найдены или не совпадают. Выберите файл в исходной папке recordings') from None
    label=row.get('preview_text')
    if not isinstance(label,str) or not label.strip():label=_cached_label(root,row)
    login_match=re.fullmatch(r'([a-z0-9_]{1,25}) VOD (\d+)',job.name)
    if login_match and login_match.group(2)==str(meta['vod_id']):login=login_match.group(1)
    else:login=str(test_login or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{1,25}',login):
        raise PreviewError('Для тестового видео укажите логин стримера в настройках «Превью реакций»')
    text_path=directory/(video.stem+'.txt')
    try:
        if text_path.is_symlink() or text_path.stat().st_size>20_000_000:raise ValueError()
        text=text_path.read_text(encoding='utf-8-sig')
    except (OSError,ValueError):raise PreviewError('Рядом с нарезкой не найден её транскрипт .txt') from None
    segs=[]
    for line in text.splitlines():
        match=re.match(r'^\[(\d{2,}):(\d{2}):(\d{2})\]\s*(.+)$',line)
        if match:
            h,m,s=map(int,match.group(1,2,3));t=h*3600+m*60+s
            if m<60 and s<60 and t<=duration:segs.append({'start':t,'text':match.group(4)})
    if not segs:raise PreviewError('В транскрипте нарезки не найдены реплики с таймкодами')
    local_row=dict(row,start=0.0,end=duration+1,preview_text=label.strip())
    target=video.with_name(video.stem+' — ИИ-превью.jpg')
    return {'video':video,'login':login,'row':local_row,'segs':segs,'target':target,'report':target.with_suffix('.json')}


def generate_saved_clip(root, video, test_login='', cancel=None, status=None, source_url=None, new_copy=False):
    """Generate an on-demand preview without retranscription or ChatGPT."""
    status=status or (lambda _:None)
    data=prepare_saved_clip(root,video,test_login); target=data['target'];report=data['report']
    if new_copy and (target.exists() or report.exists()):
        original=target
        for n in range(2,10000):
            candidate=original.with_name(original.stem+f' ({n})'+original.suffix)
            if not candidate.exists() and not candidate.with_suffix('.json').exists():
                target=candidate;report=candidate.with_suffix('.json');break
        else:raise PreviewError('Слишком много файлов превью, не найдено свободное имя')
    if target.exists():
        status('Ранее созданное превью найдено; не перезаписываю')
        label=''
        try:
            saved=json.loads(report.read_text(encoding='utf-8-sig'))
            if isinstance(saved,dict) and isinstance(saved.get('preview_text'),str):label=saved['preview_text']
        except (OSError,ValueError,TypeError):pass
        return {'path':str(target),'preview_text':label,'existing':True}
    if report.exists():raise PreviewError('Найден отчёт без превью; не перезаписываю существующий файл')
    if cancel and cancel.is_set():raise PreviewError('Тест превью отменён')
    status('Собираю превью по указанной ссылке' if source_url else 'Ищу исходное видео и собираю превью; надпись взята из сохранённого ответа')
    kwargs={'cancel':cancel,'status':status,'video':data['video']}
    if source_url:kwargs['source_url']=source_url
    info=generate(root,data['login'],data['row'],data['segs'],target,**kwargs)
    fd,tmp=tempfile.mkstemp(prefix='.preview-report-',suffix='.json',dir=report.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:json.dump(info,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        if os.name=='nt':os.rename(tmp,report)
        else:os.link(tmp,report)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return {'path':str(target),'preview_text':data['row']['preview_text'],'existing':False,'source_url':info['source_url'],'warnings':info['warnings']}
