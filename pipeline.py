"""Independent VOD processing pipeline, with bounded speech chunks and safe output.

The caller provides the reaction finder and progress callback. Real service
integration remains separate so that failure can never masquerade as success.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import full_cycle
import stream_cutter
import transcribe
import whisper_cpp
import groq_speech

class PipelineError(RuntimeError): pass
class IncompleteVod(PipelineError): pass

def safe_name(value):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')
    if not value or value.upper() in {'CON','PRN','AUX','NUL','COM1','LPT1'}: value='stream'
    return value[:100]

def timestamp(s):
    s=max(0,int(s)); return f'{s//3600:02}:{s%3600//60:02}:{s%60:02}'

def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.tmp-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(value,f,ensure_ascii=False,allow_nan=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def load_json(path):
    try:
        with open(path,encoding='utf8') as f: return json.load(f)
    except (OSError,ValueError): return None

def file_stamp(path):
    st=Path(path).stat(); return {'size':st.st_size,'mtime_ns':st.st_mtime_ns}

def validate_intervals(rows,duration,min_seconds=0):
    if not isinstance(rows,list): raise PipelineError('Ответ модели не содержит список реакций')
    clean=[]
    for item in rows:
        if not isinstance(item,dict): raise PipelineError('Некорректная реакция')
        a,b=item.get('start'),item.get('end')
        if isinstance(a,bool) or isinstance(b,bool): raise PipelineError('Некорректные границы реакции')
        try: a,b=float(a),float(b)
        except (ValueError,TypeError): raise PipelineError('Некорректные границы реакции')
        if not math.isfinite(a+b) or a<0 or b<=a or b>duration+.5:
            raise PipelineError('Модель вернула границы за пределами записи')
        if b-a<min_seconds: continue
        clean.append({'start':a,'end':min(b,duration),'title':str(item.get('title') or 'Реакция')[:100]})
    clean.sort(key=lambda x:x['start'])
    if any(b['start']<a['end'] for a,b in zip(clean,clean[1:])):
        raise PipelineError('Модель вернула пересекающиеся интервалы')
    return clean

def media_duration(path):
    if not Path(path).is_file() or Path(path).stat().st_size<1000: raise PipelineError('Видеофайл не создан или пуст')
    ffprobe=shutil.which('ffprobe')
    if not ffprobe: raise PipelineError('ffprobe не найден')
    data=stream_cutter.probe(str(path),ffprobe)
    duration=float(stream_cutter.duration_of(data))
    if not math.isfinite(duration) or duration<=0: raise PipelineError('Некорректная длительность видео')
    return duration

def default_download(vod_id, quality, output, begin=None, end=None, cli=None, threads=4):
    if not cli or not Path(cli).is_file(): raise PipelineError('Не найден TwitchDownloaderCLI')
    output=Path(output)
    if output.exists(): raise PipelineError('Временный файл уже существует; не перезаписываю')
    command=[str(cli),'videodownload','--id',str(vod_id),'-q',quality,'-t',str(threads),'--collision','Rename','-o',str(output)]
    if begin is not None: command.extend(['-b',timestamp(begin),'-e',timestamp(end)])
    # Download does not implicitly inherit proxies; service-specific proxy support must be explicit.
    env={k:v for k,v in os.environ.items() if not k.lower().endswith('_proxy')}
    env['NO_PROXY']='*'; env['no_proxy']='*'
    result=subprocess.run(command,cwd=str(Path(cli).parent),env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace')
    if result.returncode or not output.is_file(): raise PipelineError('TwitchDownloader не смог скачать VOD (код '+str(result.returncode)+')')

def _valid_speech_segments(segments, start, end):
    if not isinstance(segments, list): raise PipelineError('Распознаватель не вернул список сегментов')
    clean=[]
    for seg in segments:
        if not isinstance(seg,dict) or not isinstance(seg.get('text'),str): raise PipelineError('Повреждённый сегмент транскрипта')
        try:
            if isinstance(seg.get('start'),bool) or isinstance(seg.get('end'),bool): raise ValueError()
            a,b=float(seg['start']),float(seg['end'])
        except (ValueError,TypeError,KeyError): raise PipelineError('Повреждённые времена транскрипта') from None
        if not math.isfinite(a) or not math.isfinite(b) or a<start-.5 or b>end+1 or b<a:
            raise PipelineError('Времена транскрипта вне части')
        clean.append(dict(seg,start=a,end=b))
    return clean

def speech_chunks(video, cache_dir, cfg, progress, *, transcriber=None, duration=None, groq_transcriber=None):
    duration=media_duration(video) if duration is None else float(duration)
    chunk_seconds=600
    source=file_stamp(video)
    settings={'engine':'whisper.cpp-b5454','source':source,'model':cfg.get('model','large-v3-turbo'),'language':cfg.get('language','ru'),
              'device':cfg.get('device','auto'),'compute':cfg.get('compute','auto'),'beam':int(cfg.get('beam',1)), 'chunk_seconds':chunk_seconds}
    groq_settings={'engine':'groq-whisper-large-v3-turbo','source':source,'language':settings['language'],'chunk_seconds':chunk_seconds}
    def cache_key(params): return hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()
    local_key=cache_key(settings); cloud_key=cache_key(groq_settings)
    use_groq=bool(cfg.get('groq_enabled',True) and cfg.get('groq_api_key','').strip())
    if cfg.get('groq_enabled',True) and not use_groq:
        progress('Транскрибация','Ключ Groq не указан — используется локальный Whisper')
    total=math.ceil(duration/chunk_seconds); result=[]; bad_cloud_responses=0
    for i in range(total):
        start=i*chunk_seconds; end=min(duration,start+chunk_seconds)
        def read_cached(key):
            obj=load_json(Path(cache_dir)/key/f'{i:04}.json')
            if not isinstance(obj,dict) or obj.get('key')!=key: return None
            try: return _valid_speech_segments(obj.get('segments'),start,end)
            except PipelineError: return None
        segments=read_cached(cloud_key) if use_groq else None
        origin='Groq (кэш)' if segments is not None else ''
        if segments is None:
            segments=read_cached(local_key)
            if segments is not None: origin='локальный Whisper (кэш)'
        if segments is None and use_groq:
            progress('Транскрибация',f'Часть {i+1}/{total} · Groq · {timestamp(start)}–{timestamp(end)}')
            try:
                fn=groq_transcriber or groq_speech.transcribe_chunk
                segments=_valid_speech_segments(fn(str(video),start=start,end=end,api_key=cfg['groq_api_key'].strip(),
                    language=settings['language'],proxy_url=cfg.get('groq_proxy_url','')),start,end)
                atomic_json(Path(cache_dir)/cloud_key/f'{i:04}.json',{'key':cloud_key,'segments':segments})
                origin='Groq'; bad_cloud_responses=0
            except (groq_speech.GroqError,PipelineError) as exc:
                # Data errors may affect only this chunk; service errors disable cloud
                # for the rest of this job. Do not blindly retry the same paid request.
                reason=str(exc) if isinstance(exc,groq_speech.GroqError) else 'Некорректные сегменты Groq'
                if isinstance(exc,(groq_speech.GroqResponseError,PipelineError)):
                    bad_cloud_responses+=1
                    if bad_cloud_responses < 3:
                        progress('Транскрибация',f'{reason}; эта часть — локальный Whisper, следующая — Groq')
                    else:
                        progress('Транскрибация',f'{reason}; три ошибки ответа подряд, остаток — локальный Whisper')
                        use_groq=False
                else:
                    progress('Транскрибация',f'{reason}; оставшиеся части — локальный Whisper')
                    use_groq=False
        if segments is None:
            progress('Транскрибация',f'Часть {i+1}/{total} · локальный Whisper · {timestamp(start)}–{timestamp(end)}')
            fn=transcriber or whisper_cpp.transcribe_file
            def local_range(a,b):
                rows=fn(str(video),engine='local',model=settings['model'],lang=settings['language'],device=settings['device'],compute=settings['compute'],beam=settings['beam'],start=a,end=b,quiet=True)
                return _valid_speech_segments(rows,a,b)
            # A failed long segment can be retried as two independent shorter ranges.
            # Save each half separately, but promote the full chunk only when both pass.
            midpoint=(start+end)/2
            ranges=((start,midpoint),(midpoint,end))
            def cached_half(j,a,b):
                obj=load_json(Path(cache_dir)/local_key/f'{i:04}.half-{j}.json')
                if not isinstance(obj,dict) or obj.get('key')!=local_key or obj.get('start')!=a or obj.get('end')!=b: return None
                try: return _valid_speech_segments(obj.get('segments'),a,b)
                except PipelineError: return None
            halves=[cached_half(j,a,b) for j,(a,b) in enumerate(ranges)]
            try:
                segments=None if any(x is not None for x in halves) else local_range(start,end)
            except whisper_cpp.InvalidWhisperSegments as exc:
                if end-start < 2: raise
                progress('Транскрибация',f'{exc}; повторяю эту часть двумя половинами')
                segments=None
            if segments is None:
                segments=[]
                for j,(a,b) in enumerate(ranges):
                    part=halves[j]
                    if part is None:
                        progress('Транскрибация',f'Часть {i+1}/{total} · локальный Whisper, половина {j+1}/2 · {timestamp(a)}–{timestamp(b)}')
                        part=local_range(a,b)
                        atomic_json(Path(cache_dir)/local_key/f'{i:04}.half-{j}.json',{'key':local_key,'start':a,'end':b,'segments':part})
                    segments.extend(part)
            atomic_json(Path(cache_dir)/local_key/f'{i:04}.json',{'key':local_key,'segments':segments})
            origin='локальный Whisper'
        if 'кэш' in origin: progress('Транскрибация',f'Часть {i+1}/{total} · {origin}')
        result.extend(segments)
    if not result: raise PipelineError('В записи не обнаружена речь')
    return result,duration

def render_clip(src, output, row, source_offset, silence_cfg):
    ffmpeg,ffprobe=stream_cutter.require_tools()
    args=stream_cutter.parse_args([])
    args.out=str(output)
    # Keep the cutter's generated timeline away from the user's input file.
    args.timeline=str(Path(output).parent/'_work'/(Path(output).stem+'.timeline.json'))
    args.min_cut=float(silence_cfg.get('min_cut',1.5)); args.max_gap=float(silence_cfg.get('max_gap',2.0))
    args.onset_db=float(silence_cfg.get('onset_db',12.0))
    args.blur_regions=[]; args.precise=True; args.fast=False
    args.scale_height=int(silence_cfg.get('max_height',1080) or 0)
    args.maxrate=silence_cfg.get('maxrate','12M') or None
    stream_cutter.apply_speed(args,silence_cfg.get('speed','turbo'))
    args.vcodec=stream_cutter.pick_encoder(ffmpeg,args.vcodec)
    res=stream_cutter.analyse(str(src),args,ffmpeg,ffprobe)
    start=float(row['start'])-source_offset; end=float(row['end'])-source_offset
    if float(res['total'])+.3<end: raise PipelineError('Скачанный фрагмент короче найденного интервала')
    keeps=full_cycle.trim_keeps(res['keep'] if silence_cfg.get('enabled',True) else [(0.,float(res['total']))],start,end)
    if sum(b-a for a,b in keeps)<.5: raise PipelineError('После удаления тишины не осталось видео')
    res['keep']=keeps
    stream_cutter.render_file(res,args,ffmpeg,ffprobe)
    duration=media_duration(output)
    return keeps,duration

class Pipeline:
    def __init__(self, root, finder, download=default_download, renderer=render_clip, speech=speech_chunks):
        self.root=Path(root).resolve(); self.finder=finder; self.download=download; self.renderer=renderer; self.speech=speech

    def run(self, job, cfg, progress=lambda stage,detail='':None):
        vod_id=str(job['vod_id']); login=str(job['streamer'])
        local_name=job.get('local_name')
        if local_name:
            if not re.fullmatch(r'test-[a-f0-9]{32}',vod_id): raise PipelineError('Некорректный ID теста')
            if local_name in ('.','..') or '/' in local_name or '\\' in local_name or Path(local_name).name != local_name:
                raise PipelineError('Некорректное имя тестового файла')
            folder=self.root/'recordings'/f'Тест {safe_name(Path(local_name).stem)} {vod_id}'
        else:
            if not vod_id.isdecimal(): raise PipelineError('Некорректный ID VOD')
            folder=self.root/'recordings'/f'{safe_name(login)} VOD {vod_id}'
        folder.mkdir(parents=True,exist_ok=True)
        work=folder/'_work'; work.mkdir(exist_ok=True)
        cli=cfg.get('cli_path') or str(self.root/'desktop'/'tools'/'TwitchDownloaderCLI'/'TwitchDownloaderCLI.exe')
        threads=int(cfg.get('download_threads',4)); quality=cfg.get('quality','1080p60')
        def check_local():
            if local_name:
                path=self.root/'test_input'/local_name
                try:
                    if path.is_symlink() or not path.is_file(): raise PipelineError('Тестовый файл отсутствует или является ссылкой')
                    stamp=file_stamp(path)
                except OSError as exc:
                    raise PipelineError('Не удалось прочитать тестовый файл') from exc
                if stamp != {'size':job.get('local_size'),'mtime_ns':job.get('local_mtime_ns')}:
                    raise PipelineError('Тестовый файл изменился после постановки в очередь; добавьте его заново')
        if local_name:
            progress('Скачивание','Проверка локального тестового файла: '+local_name)
            check_local()
            src=self.root/'test_input'/local_name
            duration=media_duration(src)
        else:
            src=work/'full-160p.mp4'
            progress('Скачивание','Загружается запись для транскрибации')
            expected=job.get('duration_seconds')
            def check_duration(path):
                actual=media_duration(path)
                if expected and actual < float(expected)-max(5.,float(expected)*.005):
                    raise IncompleteVod('Запись короче заявленной Twitch; не обрабатываю неполный VOD')
                return actual
            if src.exists():
                # Preserve old downloads for inspection but do not reuse a known short file.
                try:
                    duration=check_duration(src)
                except IncompleteVod:
                    import uuid
                    os.replace(src,work/('full-160p.incomplete-'+uuid.uuid4().hex+'.mp4'))
                    raise
            else:
                temp=work/'full-160p.downloading.mp4'
                if temp.exists(): temp.unlink() # only our own explicitly named temporary file
                self.download(vod_id,'160p30',temp,cli=cli,threads=threads)
                duration=check_duration(temp) # do not promote a partial VOD into the cache
                os.replace(temp,src)
        segs,duration=self.speech(src,work/'transcripts',cfg.get('speech',{}),progress,duration=duration)
        check_local()
        progress('Поиск реакций','Анализируется полный транскрипт')
        rows=validate_intervals(self.finder(str(src),segs,duration,login,cfg,progress),duration,float(cfg.get('min_reaction_minutes',10))*60)
        check_local()
        atomic_json(folder/'reactions.json',{'vod_id':vod_id,'reactions':rows})
        outputs=[]
        for i,row in enumerate(rows,1):
            progress('Нарезка',f'{i}/{len(rows)} · {timestamp(row["start"])}–{timestamp(row["end"])}')
            stem=f'{i:02d} {safe_name(row["title"])}'; output=folder/(stem+'.mp4'); meta=folder/(stem+'.pipeline.json'); txt=folder/(stem+'.txt')
            config_hash=hashlib.sha256(json.dumps({'row':row,'quality':quality,'silence':cfg.get('silence',{})},sort_keys=True).encode()).hexdigest()
            previous=load_json(meta)
            if output.exists() and txt.exists() and isinstance(previous,dict) and previous.get('key')==config_hash:
                media_duration(output); outputs.append(str(output)); continue
            if output.exists() or meta.exists() or txt.exists(): raise PipelineError('Найдены ранее созданные файлы с другими настройками; не перезаписываю')
            begin,end=full_cycle.download_bounds(row['start'],row['end'])
            raw=work/(f'clip-{i:04}.mp4'); temp=work/(f'clip-{i:04}.download.mp4'); rendered=folder/(stem+'.rendering.mp4')
            if rendered.exists():rendered.unlink()
            if local_name:
                check_local()
                render_source=src; offset=0
            else:
                if temp.exists():temp.unlink()
                if raw.exists():raw.unlink()
                self.download(vod_id,quality,temp,begin=begin,end=end,cli=cli,threads=threads)
                media_duration(temp); os.replace(temp,raw)
                render_source=raw; offset=begin
            progress('Рендер',f'Нарезка {i}/{len(rows)}')
            keeps,actual=self.renderer(render_source,rendered,row,offset,cfg.get('silence',{}))
            check_local()
            mapped,planned=full_cycle.remap_segments(segs,offset,keeps)
            if abs(actual-planned)>.5:
                raise PipelineError('Длительность видео не совпала с монтажной картой; таймкоды не сохранены')
            mapped=full_cycle.clip_segments(mapped,actual)
            # A partial render is never presented as completed.
            os.replace(rendered,output)
            txt.write_text('Транскрипт готовой нарезки; времена от начала видео.\n\n'+transcribe.to_txt(mapped),encoding='utf-8-sig')
            atomic_json(meta,{'key':config_hash,'vod_id':vod_id,'reaction':row,'keep':keeps,'duration':actual,'transcript':txt.name})
            if not local_name and not cfg.get('keep_original',False): raw.unlink(missing_ok=True)
            outputs.append(str(output))
        progress('Готово',f'Найдено реакций: {len(rows)}; сохранено видео: {len(outputs)}')
        return {'folder':str(folder),'outputs':outputs,'reactions':len(rows)}
