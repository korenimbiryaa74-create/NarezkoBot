"""Experimental local-only thumbnail generation for a single finished video.

Runs an isolated ComfyUI process, never a remote/cloud node. No YouTube actions.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

MODEL='flux3-image-klein-int8-convrot.safetensors'
ENCODER='qwen3vl_4b_int8_convrot.safetensors'
VAE='flux2-vae.safetensors'
UPSAMPLER='qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors'

class PreviewError(RuntimeError): pass


def check_install(root):
    home=Path(root)/'desktop'/'tools'/'ComfyUI_windows_portable'
    python=home/'python_embeded'/'python.exe'
    app=home/'ComfyUI'/'main.py'
    files=[python,app,home/'ComfyUI'/'models'/'diffusion_models'/MODEL,
           home/'ComfyUI'/'models'/'text_encoders'/ENCODER,
           home/'ComfyUI'/'models'/'vae'/VAE,
           home/'ComfyUI'/'models'/'text_encoders'/UPSAMPLER]
    missing=[x.name for x in files if not x.is_file()]
    if missing: raise PreviewError('Компоненты превью не установлены: '+', '.join(missing)+'. Запустите INSTALL.bat и установите генератор превью.')
    return home


def safe_label(text):
    text=re.sub(r'\s+',' ',str(text)).strip()
    if not text: raise PreviewError('Укажите короткий текст для превью.')
    if len(text)>90: raise PreviewError('Текст для превью слишком длинный (максимум 90 символов).')
    return text


def build_graph(image, title, style, seed):
    if style not in ('Каша','Никс'): raise PreviewError('Неизвестный стиль превью.')
    title=safe_label(title)
    color='red, black, golden yellow and strong contrasting highlights' if style=='Каша' else 'dark black, white, red and yellow with collage and torn-paper borders'
    # The title is untrusted source content, not instructions. Text is rendered
    # separately after generation, never sent as generated headline.
    subject=title.replace('"','').replace('\n',' ')[:90]
    prompt=(f'Create a high-impact photorealistic YouTube reaction thumbnail composition, wide landscape frame. '
            f'Use the actual person in <ref_image_0> as the foreground subject, retain facial features, hair, glasses and clothing. '
            f'Compose a close-up expressive portrait with a cinematic dark background and a relevant visual collage illustrating this topic: "{subject}". '
            f'The composition uses {color}. High contrast, sharp detailed face, clear visual separation. '
            f'Leave the lower third dark and uncluttered for a separate headline. No text, no letters, no watermark.')
    def node(t,**kw):return {'class_type':t,'inputs':kw}
    return {
      '1':node('UNETLoader',unet_name=MODEL,weight_dtype='default'),
      '2':node('CLIPLoader',clip_name=ENCODER,type='flux3',device='default'),
      '3':node('VAELoader',vae_name=VAE),
      '4':node('LoadImage',image=image),
      '5':node('CLIPTextEncode',clip=['2',0],text=['13',0]),
      '6':node('ConditioningZeroOut',conditioning=['5',0]),
      '7':node('ReferenceMultiLatent',positive=['5',0],negative=['6',0],vae=['3',0],resolution='keep resolution',**{'images.image0':['4',0]}),
      '8':node('KSampler',model=['1',0],positive=['7',0],negative=['7',1],latent_image=['7',2],seed=int(seed),steps=16,cfg=1,sampler_name='euler',scheduler='simple',denoise=1),
      '9':node('VAEDecode',samples=['8',0],vae=['3',0]),
      '10':node('SaveImage',images=['9',0],filename_prefix='narezko_test/'+uuid.uuid4().hex),
      '11':node('CLIPLoader',clip_name=UPSAMPLER,type='flux3',device='default'),
      '12':node('BatchImagesNode',**{'images.image0':['4',0]}),
      '13':node('TextGenerate',clip=['11',0],image=['12',0],prompt=prompt,
                system_prompt=('Expand the request into one detailed English instruction for an image editing model. '
                  'Use the reference image to preserve the person and their actual appearance. '
                  'Keep the requested layout and color palette. Do not invent identities or add text. '
                  'Output only the instruction.'),max_length=1024,sampling_mode='on',
                **{'sampling_mode.temperature':1,'sampling_mode.top_k':1,'sampling_mode.top_p':0.95,
                   'sampling_mode.min_p':0,'sampling_mode.repetition_penalty':1.05,'sampling_mode.seed':0,
                   'sampling_mode.presence_penalty':0,'thinking':False,'use_default_template':True,'mtp':'off'}),
    }


def ffmpeg_path():
    ffmpeg=shutil.which('ffmpeg');probe=shutil.which('ffprobe')
    if not ffmpeg or not probe: raise PreviewError('Не найден FFmpeg/ffprobe. Запустите INSTALL.bat.')
    return ffmpeg,probe


def choose_frame(video, destination, progress=lambda _:None):
    import cv2
    import numpy as np
    def read_image(path):
        try:return cv2.imdecode(np.fromfile(str(path),dtype=np.uint8),cv2.IMREAD_COLOR)
        except OSError:return None
    ffmpeg,probe=ffmpeg_path()
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    try:
        p=subprocess.run([probe,'-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(video)],capture_output=True,text=True,timeout=45,creationflags=flags,check=True)
        duration=float(p.stdout.strip())
        if not 0<duration<60*60*48: raise ValueError('duration')
    except (OSError,ValueError,subprocess.SubprocessError) as exc:raise PreviewError('Не удалось определить длительность видео.') from exc
    # OpenCV 5's base wheel may not contain the Haar detector or its XML.
    # Fall back to a representative frame; do not claim it contains a face.
    cascade=None
    try:
        classifier=getattr(cv2,'CascadeClassifier',None)
        cascade_dir=getattr(getattr(cv2,'data',None),'haarcascades',None)
        cascade_path=Path(cascade_dir)/'haarcascade_frontalface_default.xml' if cascade_dir else None
        if classifier and cascade_path and cascade_path.is_file():
            candidate=classifier(str(cascade_path))
            if not candidate.empty():cascade=candidate
    except (AttributeError,OSError,cv2.error):pass
    if cascade is None:progress('Распознавание лица недоступно в этой версии OpenCV; выбран кадр без поиска лица.')
    best=None;best_score=-1
    fractions=(.12,.27,.42,.57,.72,.87) if cascade is not None else (.42,)
    for i,fraction in enumerate(fractions):
        frame=Path(destination).with_name(f'frame-{i}.jpg')
        try:
            subprocess.run([ffmpeg,'-v','error','-ss',str(max(0,duration*fraction)),'-i',str(video),'-frames:v','1','-q:v','2','-y',str(frame)],capture_output=True,timeout=90,creationflags=flags,check=True)
            data=read_image(frame);
            if data is None:continue
            gray=cv2.cvtColor(data,cv2.COLOR_BGR2GRAY) if cascade is not None else None
            faces=() if cascade is None else cascade.detectMultiScale(gray,scaleFactor=1.1,minNeighbors=5,minSize=(40,40))
            score=max((int(w)*int(h) for x,y,w,h in faces),default=0)
            if score>best_score:best=frame;best_score=score
        except (OSError,subprocess.SubprocessError):continue
    if best is None:raise PreviewError('Не удалось извлечь кадр из видео.')
    # Resize the frame to a 16:9 canvas at low VRAM cost. Do not silently claim
    # a face was found; model quality is dependent on the actual frame.
    data=read_image(best); h,w=data.shape[:2]; target=16/9
    if w/h>target:
        nw=round(h*target);left=(w-nw)//2;data=data[:,left:left+nw]
    else:
        nh=round(w/target);top=(h-nh)//2;data=data[top:top+nh,:]
    data=cv2.resize(data,(960,544),interpolation=cv2.INTER_AREA)
    ok,encoded=cv2.imencode('.png',data)
    if not ok:raise PreviewError('Не удалось сохранить кадр для генерации.')
    encoded.tofile(str(destination))
    if cascade is not None and best_score<=0:progress('Лицо на выбранных кадрах не распознано; результат может быть неточным.')
    return best_score>0


def _port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():raise PreviewError('Генерация превью отменена.')

def _sleep(seconds,cancel):
    if cancel is None:time.sleep(seconds)
    elif cancel.wait(seconds):_cancelled(cancel)

def _ready(session,base,process,timeout=240,cancel=None):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        _cancelled(cancel)
        if process.poll() is not None:raise PreviewError('ComfyUI завершился при запуске; проверьте NVIDIA-драйвер и установку.')
        try:
            r=session.get(base+'/system_stats',timeout=2);r.raise_for_status();return
        except requests.RequestException:_sleep(1,cancel)
    raise PreviewError('ComfyUI не запустился за 4 минуты.')


def _save_caption(data,title,path):
    try:
        from PIL import Image,ImageDraw,ImageFont,ImageOps
        from io import BytesIO
        im=Image.open(BytesIO(data)).convert('RGB'); im=ImageOps.fit(im,(1280,720),method=Image.Resampling.LANCZOS)
        draw=ImageDraw.Draw(im,'RGBA');draw.rectangle((0,535,1280,720),fill=(0,0,0,190))
        font_path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'arialbd.ttf'
        if not font_path.is_file():raise PreviewError('Не найден шрифт Windows Arial Bold.')
        words=safe_label(title).upper().split()
        def wrap(font):
            result=[]; line=''
            for word in words:
                for part in [word]:
                    candidate=(line+' '+part).strip()
                    if line and draw.textbbox((0,0),candidate,font=font,stroke_width=3)[2]>1200:
                        result.append(line);line=part
                    else:line=candidate
            if line:result.append(line)
            return result
        for size in range(70,15,-2):
            font=ImageFont.truetype(str(font_path),size);lines=wrap(font)
            if len(lines)<=2 and all(draw.textbbox((0,0),line,font=font,stroke_width=3)[2]<=1200 for line in lines):break
        if len(lines)>2 or any(draw.textbbox((0,0),line,font=font,stroke_width=3)[2]>1200 for line in lines):
            raise PreviewError('Слишком длинная надпись для превью. Сократите её.')
        draw.multiline_text((35,547),'\n'.join(lines),font=font,fill=(255,240,55,255),stroke_width=3,stroke_fill=(0,0,0,255),spacing=0)
        im.save(path,'JPEG',quality=91,optimize=True)
    except PreviewError:raise
    except Exception as exc:raise PreviewError('Не удалось оформить готовое изображение: '+str(exc)[:150]) from exc


def generate_test(root,video,title,style,progress=lambda _:None,cancel=None):
    root=Path(root);video=Path(video)
    if not video.is_file():raise PreviewError('Файл нарезки не найден.')
    title=safe_label(title);home=check_install(root)
    dest=root/'preview_test';dest.mkdir(parents=True,exist_ok=True)
    result=dest/('preview_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]+'.jpg')
    port=_port();base=f'http://127.0.0.1:{port}';proc=None
    session=requests.Session();session.trust_env=False
    with tempfile.TemporaryDirectory(prefix='narezko_preview_') as tmp:
        frame=Path(tmp)/'ref.png';progress('Выбираю кадр из готовой нарезки')
        choose_frame(video,frame,progress)
        _cancelled(cancel)
        input_dir=home/'ComfyUI'/'input';input_dir.mkdir(parents=True,exist_ok=True)
        input_path=input_dir/('narezko_'+uuid.uuid4().hex+'.png');shutil.copyfile(frame,input_path)
        log_path=dest/'comfyui_test.log'
        try:
            progress('Запускаю локальную модель. Первый запуск может занять несколько минут.')
            with open(log_path,'ab') as log:
                proc=subprocess.Popen([str(home/'python_embeded'/'python.exe'),'-s',str(home/'ComfyUI'/'main.py'),'--listen','127.0.0.1','--port',str(port),'--disable-auto-launch','--disable-api-nodes','--disable-all-custom-nodes','--lowvram','--cache-none'],cwd=str(home/'ComfyUI'),stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                _ready(session,base,proc,cancel=cancel)
                graph=build_graph(input_path.name,title,style,int.from_bytes(os.urandom(6),'big'))
                try:
                    r=session.post(base+'/prompt',json={'prompt':graph,'client_id':uuid.uuid4().hex},timeout=30)
                    if not r.ok:
                        log.write(('\nОшибка задания ComfyUI: '+r.text[:4000]+'\n').encode('utf-8',errors='replace'));log.flush()
                    r.raise_for_status();job=r.json()['prompt_id']
                except (requests.RequestException,KeyError,ValueError) as exc:raise PreviewError('ComfyUI не принял задание; подробности в comfyui_test.log.') from exc
                progress('Генерирую превью локально на видеокарте')
                end=time.monotonic()+1800
                while time.monotonic()<end:
                    _cancelled(cancel)
                    if proc.poll() is not None:raise PreviewError('ComfyUI завершился во время генерации; проверьте comfyui_test.log.')
                    try:
                        r=session.get(base+'/history/'+quote(job,safe=''),timeout=10);r.raise_for_status();entry=r.json().get(job)
                    except (requests.RequestException,ValueError) as exc:raise PreviewError('Потеряна связь с ComfyUI.') from exc
                    if entry:
                        state=entry.get('status',{})
                        if state.get('status_str')=='error':raise PreviewError('Ошибка модели ComfyUI; проверьте comfyui_test.log (возможно, не хватает памяти).')
                        images=entry.get('outputs',{}).get('10',{}).get('images',[])
                        if len(images)!=1:raise PreviewError('ComfyUI не вернул одно готовое изображение.')
                        img=images[0]
                        try:
                            r=session.get(base+'/view',params={'filename':img['filename'],'subfolder':img.get('subfolder',''),'type':'output'},timeout=60);r.raise_for_status();_save_caption(r.content,title,result)
                        except (requests.RequestException,KeyError) as exc:raise PreviewError('Не удалось получить изображение из ComfyUI.') from exc
                        progress('Превью сохранено: '+str(result));return result
                    _sleep(2,cancel)
                raise PreviewError('Превышено время ожидания модели (30 минут).')
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
            input_path.unlink(missing_ok=True)
            session.close()


def build_reaction_graph(style_image, person_image, source_image, title, seed):
    """One local diffusion call: style, actual cutout, source video thumbnail."""
    title=safe_label(title); quoted_title=json.dumps(title,ensure_ascii=False)
    if any(ord(c)<32 for c in title):raise PreviewError('Недопустимые символы в тексте превью.')
    prompt=(f'Create one polished YouTube reaction thumbnail in a wide 16:9 format. '
        f'Use <ref_image_0> ONLY as a visual style and layout reference: strong thumbnail design, '
        f'large heavy italic yellow and white headline with dark outline, depth and shadow. '
        f'Use the person from <ref_image_1> as the recognizable streamer; preserve their real face, '
        f'hair, glasses and clothing. Put them large on the right. '
        f'Use <ref_image_2> as the video being watched, place it as a large clear framed image on the left; '
        f'preserve its actual content. In the lower-left, draw the following exact Russian Cyrillic headline: '
        f'{quoted_title}. Use large legible bold type, arrange it to fit the composition. '
        f'No other text, no duplicated faces, no extra people, no watermarks. '
        f'Do not copy any words from <ref_image_0>.')
    def node(t,**kw):return {'class_type':t,'inputs':kw}
    refs={'images.image0':['4',0], 'images.image1':['14',0], 'images.image2':['15',0]}
    return {
      '1':node('UNETLoader',unet_name=MODEL,weight_dtype='default'),
      '2':node('CLIPLoader',clip_name=ENCODER,type='flux3',device='default'),
      '3':node('VAELoader',vae_name=VAE),
      '4':node('LoadImage',image=style_image),
      '14':node('LoadImage',image=person_image),
      '15':node('LoadImage',image=source_image),
      '5':node('CLIPTextEncode',clip=['2',0],text=['13',0]),
      '6':node('ConditioningZeroOut',conditioning=['5',0]),
      '7':node('ReferenceMultiLatent',positive=['5',0],negative=['6',0],vae=['3',0],resolution='keep resolution',**refs),
      '8':node('KSampler',model=['1',0],positive=['7',0],negative=['7',1],latent_image=['7',2],seed=int(seed),steps=16,cfg=1,sampler_name='euler',scheduler='simple',denoise=1),
      '9':node('VAEDecode',samples=['8',0],vae=['3',0]),
      '10':node('SaveImage',images=['9',0],filename_prefix='narezko_test/'+uuid.uuid4().hex),
      '11':node('CLIPLoader',clip_name=UPSAMPLER,type='flux3',device='default'),
      '12':node('BatchImagesNode',**refs),
      '13':node('TextGenerate',clip=['11',0],image=['12',0],prompt=prompt,
         system_prompt=('Expand the request into a detailed visual description for an image editing model. '
           'Use all reference images for their specified roles. The quoted Cyrillic headline is exact: '
           'keep every character unchanged, never translate, correct, replace or omit it. '
           'Do not add any other text. Output only the expanded visual instruction.'),
         max_length=1024,sampling_mode='on',
         **{'sampling_mode.temperature':1,'sampling_mode.top_k':1,'sampling_mode.top_p':0.95,
            'sampling_mode.min_p':0,'sampling_mode.repetition_penalty':1.05,'sampling_mode.seed':0,
            'sampling_mode.presence_penalty':0,'thinking':False,'use_default_template':True,'mtp':'off'}),
    }


def _prepare_reference(image, destination):
    """Pad, not crop; consistent 16:9 input sizes for the ComfyUI batch node."""
    from PIL import Image, ImageOps
    try:
        if isinstance(image,(str,Path)):
            with Image.open(image) as source:
                if source.width*source.height>40_000_000:raise PreviewError('Изображение для модели слишком большое.')
                im=ImageOps.exif_transpose(source).convert('RGBA')
        else:
            if image.width*image.height>40_000_000:raise PreviewError('Изображение для модели слишком большое.')
            im=image.convert('RGBA')
        im=ImageOps.contain(im,(1024,576),Image.Resampling.LANCZOS)
        canvas=Image.new('RGBA',(1024,576),(28,6,8,255))
        canvas.alpha_composite(im,((1024-im.width)//2,(576-im.height)//2))
        canvas.convert('RGB').save(destination,'PNG')
    except PreviewError:raise
    except Exception as exc:raise PreviewError('Не удалось прочитать референс изображения: '+str(exc)[:150]) from exc


def _save_model_image(data,dest):
    from io import BytesIO
    from PIL import Image, ImageOps
    import tempfile
    dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width<320 or image.height<180 or image.width*image.height>40_000_000:raise PreviewError('Модель вернула изображение неподходящего размера.')
            im=ImageOps.fit(image.convert('RGB'),(1280,720),method=Image.Resampling.LANCZOS)
            fd,tmp=tempfile.mkstemp(prefix='.ai-preview-',suffix='.jpg',dir=dest.parent);os.close(fd)
            try:
                im.save(tmp,'JPEG',quality=93,optimize=True)
                if dest.exists():raise PreviewError('Превью уже существует; не перезаписываю.')
                if os.name=='nt':os.rename(tmp,dest)
                else:os.link(tmp,dest)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
    except PreviewError:raise
    except Exception as exc:raise PreviewError('Не удалось сохранить изображение модели: '+str(exc)[:150]) from exc


def generate_reaction(root, cutout, style_image, source_image, title, destination, progress=lambda _:None, cancel=None):
    """Run the installed model locally once. No GPT request and no caption overlay."""
    home=check_install(root);_cancelled(cancel)
    destination=Path(destination)
    if destination.exists():raise PreviewError('Превью уже существует; не перезаписываю.')
    dest=Path(root)/'preview_test';dest.mkdir(parents=True,exist_ok=True)
    input_dir=home/'ComfyUI'/'input';input_dir.mkdir(parents=True,exist_ok=True)
    input_paths=[];proc=None;session=requests.Session();session.trust_env=False
    port=_port();base=f'http://127.0.0.1:{port}'
    try:
        for image in (style_image,cutout,source_image):
            _cancelled(cancel)
            path=input_dir/('narezko_'+uuid.uuid4().hex+'.png');input_paths.append(path)
            _prepare_reference(image,path)
        log_path=dest/'comfyui_test.log'
        progress('Запускаю локальную модель FLUX 3. Первый запуск может занять несколько минут.')
        with open(log_path,'ab') as log:
            proc=subprocess.Popen([str(home/'python_embeded'/'python.exe'),'-s',str(home/'ComfyUI'/'main.py'),'--listen','127.0.0.1','--port',str(port),'--disable-auto-launch','--disable-api-nodes','--disable-all-custom-nodes','--lowvram','--cache-none'],cwd=str(home/'ComfyUI'),stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            _ready(session,base,proc,cancel=cancel)
            graph=build_reaction_graph(*(p.name for p in input_paths),title,int.from_bytes(os.urandom(6),'big'))
            try:
                r=session.post(base+'/prompt',json={'prompt':graph,'client_id':uuid.uuid4().hex},timeout=30)
                if not r.ok:log.write(('\\nОшибка задания ComfyUI: '+r.text[:4000]+'\\n').encode('utf8'));log.flush()
                r.raise_for_status();job=r.json()['prompt_id']
            except (requests.RequestException,KeyError,ValueError) as exc:raise PreviewError('ComfyUI не принял задание; см. preview_test/comfyui_test.log.') from exc
            progress('Модель рисует превью и текст локально; потребуется ручная проверка.')
            end=time.monotonic()+1800
            while time.monotonic()<end:
                _cancelled(cancel)
                if proc.poll() is not None:raise PreviewError('ComfyUI завершился во время генерации; проверьте preview_test/comfyui_test.log.')
                try:
                    r=session.get(base+'/history/'+quote(job,safe=''),timeout=10);r.raise_for_status();entry=r.json().get(job)
                except (requests.RequestException,ValueError) as exc:raise PreviewError('Потеряна связь с ComfyUI.') from exc
                if entry:
                    if entry.get('status',{}).get('status_str')=='error':raise PreviewError('Ошибка модели; проверьте preview_test/comfyui_test.log (возможно, не хватает памяти).')
                    images=entry.get('outputs',{}).get('10',{}).get('images',[])
                    if len(images)!=1:raise PreviewError('ComfyUI не вернул одно изображение.')
                    img=images[0]
                    try:
                        r=session.get(base+'/view',params={'filename':img['filename'],'subfolder':img.get('subfolder',''),'type':'output'},timeout=60);r.raise_for_status();_cancelled(cancel);_save_model_image(r.content,destination)
                    except (requests.RequestException,KeyError) as exc:raise PreviewError('Не удалось получить изображение из ComfyUI.') from exc
                    progress('Готово. Проверьте буквы, лицо и соответствие оригиналу.')
                    return destination
                _sleep(2,cancel)
            raise PreviewError('Превышено время ожидания модели (30 минут).')
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
        for path in input_paths:path.unlink(missing_ok=True)
        session.close()
