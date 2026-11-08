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
import time
import threading
from contextlib import contextmanager
from pathlib import Path

import full_cycle
import banner_blur
import stream_cutter
import transcribe
import whisper_cpp
import groq_speech
from job_store import JobStore

class PipelineError(RuntimeError): pass
class IncompleteVod(PipelineError): pass

class RenderedKeeps(list):
    def __init__(self, keeps, timeline):
        super().__init__(keeps); self.rendered_timeline=timeline

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

@contextmanager
def report_wait(callback, description, interval=20):
    """Report elapsed waiting time, not a fabricated percentage."""
    if callback is None:
        yield
        return
    stop=threading.Event(); started=time.monotonic()
    def report():
        while not stop.wait(interval):
            try: callback(f'{description} · ожидание {timestamp(time.monotonic()-started)}')
            except Exception: return
    thread=threading.Thread(target=report,daemon=True);thread.start()
    try: yield
    finally:
        stop.set();thread.join()

def file_stamp(path):
    st=Path(path).stat(); return {'size':st.st_size,'mtime_ns':st.st_mtime_ns}

def test_transcript_cache(root, video, job, progress, check_local):
    """Separate test transcription from job output, using a full-content fingerprint."""
    progress('Транскрибация','Проверка тестового видео для кэша транскрипции')
    digest=hashlib.sha256()
    last_report=time.monotonic(); read=0; size=Path(video).stat().st_size
    with open(video,'rb') as f:
        while True:
            block=f.read(4*1024*1024)
            if not block: break
            digest.update(block); read+=len(block)
            if time.monotonic()-last_report >= 5:
                progress('Транскрибация',f'Проверка тестового видео для кэша: {min(100,int(read*100/max(size,1)))}%')
                last_report=time.monotonic()
    check_local()
    source_hash=digest.hexdigest()
    target=Path(root)/'state'/'test_transcripts'/source_hash
    # Old builds kept chunks under a unique job folder. Match saved filename,
    # size and mtime before importing; old chunks cannot prove a content hash.
    db_path=Path(root)/'state'/'jobs.sqlite3'
    if db_path.exists():
        store=JobStore(db_path)
        ids=store.local_test_ids(job['local_name'],job['local_size'],job['local_mtime_ns'])
        if ids and not store.bind_legacy_test_cache(job['local_name'],job['local_size'],job['local_mtime_ns'],source_hash):
            ids=[] # Metadata was reused by a different file; never import legacy chunks again.
        count=0
        for vod_id in ids:
            if not re.fullmatch(r'test-[a-f0-9]{32}',str(vod_id)): continue
            legacy=Path(root)/'recordings'/f"Тест {safe_name(Path(job['local_name']).stem)} {vod_id}"/'_work'/'transcripts'
            if not legacy.is_dir() or legacy.is_symlink(): continue
            for directory in legacy.iterdir():
                if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r'[a-f0-9]{64}',directory.name): continue
                for source in directory.iterdir():
                    if source.is_symlink() or not source.is_file() or not re.fullmatch(r'\d{4}(?:\.half-[01])?\.json',source.name): continue
                    dest=target/directory.name/source.name
                    if dest.exists(): continue
                    obj=load_json(source)
                    if not isinstance(obj,dict) or obj.get('key')!=directory.name: continue
                    atomic_json(dest,obj); count+=1
        if count:
            progress('Транскрибация',f'Перенесён кэш из прежних тестов: {count} частей (совпали имя, размер и время файла)')
    return target

def job_identity(job):
    return {k:job.get(k) for k in ('id','vod_id','streamer','local_name','local_size','local_mtime_ns')}

def manual_answer_path(root, job_id):
    if isinstance(job_id,bool) or not isinstance(job_id,int) or job_id <= 0: raise ValueError('Некорректный номер задания')
    return Path(root)/'state'/'manual_reactions'/f'{job_id}.json'

def manual_donation_path(root, job_id):
    if isinstance(job_id,bool) or not isinstance(job_id,int) or job_id <= 0: raise ValueError('Некорректный номер задания')
    return Path(root)/'state'/'manual_donations'/f'{job_id}.json'

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

def validate_donation_cuts(cuts, rows):
    if not isinstance(cuts,list): raise PipelineError('Некорректный список донатов')
    clean=[]
    for c in cuts:
        if not isinstance(c,dict) or isinstance(c.get('reaction'),bool) or not isinstance(c.get('reaction'),int) or not 1<=c['reaction']<=len(rows): raise PipelineError('Некорректный номер реакции доната')
        try:
            if isinstance(c['start'],bool) or isinstance(c['end'],bool): raise ValueError()
            a,b=float(c['start']),float(c['end']);row=rows[c['reaction']-1]
            if not math.isfinite(a) or not math.isfinite(b) or a<row['start'] or b>row['end'] or b<=a or b-a>180 or b-a>(row['end']-row['start'])*.5: raise ValueError()
        except (ValueError,KeyError,TypeError,OverflowError): raise PipelineError('Некорректные границы доната; не выполняю монтаж') from None
        clean.append({'reaction':c['reaction'],'start':a,'end':b})
    clean.sort(key=lambda x:(x['reaction'],x['start']))
    if any(a['reaction']==b['reaction'] and b['start']<a['end'] for a,b in zip(clean,clean[1:])): raise PipelineError('Интервалы донатов пересекаются')
    for i,row in enumerate(rows,1):
        if sum(c['end']-c['start'] for c in clean if c['reaction']==i)>(row['end']-row['start'])*.5:
            raise PipelineError('Суммарно интервалы донатов занимают более половины реакции; проверьте ответ вручную')
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
    if not cli or not Path(cli).is_file(): raise PipelineError('Не найден TwitchDownloaderCLI в desktop/tools/TwitchDownloaderCLI. Запустите INSTALL.bat или проверьте установку.')
    output=Path(output)
    if output.exists(): raise PipelineError('Временный файл уже существует; не перезаписываю')
    command=[str(cli),'videodownload','--id',str(vod_id),'-q',quality,'-t',str(threads),'--collision','Rename','-o',str(output)]
    if begin is not None: command.extend(['-b',timestamp(begin),'-e',timestamp(end)])
    # Download does not implicitly inherit proxies; service-specific proxy support must be explicit.
    env={k:v for k,v in os.environ.items() if not k.lower().endswith('_proxy')}
    env['NO_PROXY']='*'; env['no_proxy']='*'
    result=subprocess.run(command,cwd=str(Path(cli).parent),env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
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
        if any(seg.get('timing_warning')=='end_clipped' for seg in segments):
            progress('Транскрибация',f'Часть {i+1}/{total}: конец сегмента ограничен границей аудио; текст и таймкод у границы требуют проверки')
        result.extend(segments)
    if not result: raise PipelineError('В записи не обнаружена речь')
    return result,duration

def subtract_intervals(keeps, cuts):
    result=[]
    for a,b in keeps:
        cursor=a
        for start,end in cuts:
            if end<=cursor: continue
            if start>=b: break
            if start>cursor: result.append((cursor,min(start,b)))
            cursor=max(cursor,end)
            if cursor>=b: break
        if cursor<b: result.append((cursor,b))
    return result

def render_clip(src, output, row, source_offset, silence_cfg, progress_callback=None, banner_cfg=None):
    ffmpeg,ffprobe=stream_cutter.require_tools()
    args=stream_cutter.parse_args([])
    args.out=str(output)
    # Keep the cutter's generated timeline away from the user's input file.
    args.timeline=str(Path(output).parent/'_work'/(Path(output).stem+'.timeline.json'))
    args.min_cut=float(silence_cfg.get('min_cut',1.5)); args.max_gap=float(silence_cfg.get('max_gap',2.0))
    args.onset_db=float(silence_cfg.get('onset_db',12.0))
    args.blur_regions=[]; args.precise=True; args.fast=False; args.map_timeline=True
    args.scale_height=int(silence_cfg.get('max_height',1080) or 0)
    args.maxrate=silence_cfg.get('maxrate','12M') or None
    stream_cutter.apply_speed(args,silence_cfg.get('speed','turbo'))
    args.vcodec=stream_cutter.pick_encoder(ffmpeg,args.vcodec)
    start=float(row['start'])-source_offset; end=float(row['end'])-source_offset
    if progress_callback: progress_callback('Проверка видео и подготовка нарезки')
    info=stream_cutter.probe(str(src),ffprobe)
    total=float(stream_cutter.duration_of(info))
    if not math.isfinite(total) or total <= 0: raise PipelineError('Некорректная длительность видео')
    if start < 0 or end <= start or total+.3<end: raise PipelineError('Скачанный фрагмент короче найденного интервала')
    end=min(end,total)
    if silence_cfg.get('enabled',True):
        if progress_callback: progress_callback(f'Анализ тишины в выбранном фрагменте {timestamp(start)}–{timestamp(end)}')
        with report_wait(progress_callback,'Анализ тишины ещё выполняется'):
            res=stream_cutter.analyse(str(src),args,ffmpeg,ffprobe,start=start,end=end)
        keeps=full_cycle.trim_keeps(res['keep'],start,end)
    else:
        if progress_callback: progress_callback('Удаление тишины выключено; анализ аудио пропущен')
        res={'src':str(src),'info':info,'streams':stream_cutter.audio_streams(info),'total':total}
        if not res['streams']: raise PipelineError('В видео нет аудиодорожек')
        keeps=[(start,end)]
    donation_cuts=[(c['start']-source_offset,c['end']-source_offset) for c in row.get('donation_cuts',[])]
    if donation_cuts:
        if progress_callback: progress_callback(f'Удаление ответов на донаты: {len(donation_cuts)}')
        keeps=subtract_intervals(keeps,donation_cuts)
    if sum(b-a for a,b in keeps)<.5: raise PipelineError('После удаления тишины и донатов не осталось видео')
    res['keep']=keeps
    detected=None
    if (banner_cfg or {}).get('enabled',False):
        sample_interval=int((banner_cfg or {}).get('interval',5))
        if progress_callback: progress_callback(f'Поиск баннеров по образцам: ключевые кадры, шаг ~{sample_interval} с')
        with report_wait(progress_callback,'Поиск баннеров ещё выполняется'):
            detected=banner_blur.scan(src,[(start,end)],ffmpeg,progress_callback,interval=sample_interval)
        args.timed_banners=detected
        if progress_callback: progress_callback(f'Поиск баннеров завершён: найдено появлений {len(detected)}')
    if progress_callback: progress_callback('Запуск рендера')
    stream_cutter.render_file(res,args,ffmpeg,ffprobe,progress_callback=progress_callback)
    duration=media_duration(output)
    result=RenderedKeeps(keeps,res.get('render_timeline'))
    result.banner_scan=detected
    return result,duration

READY_SILENCE = 'Готовые без тишины'
READY_GENERIC = 'Готовые нарезки'

def job_output_folder(root, job):
    vod_id=str(job['vod_id']); login=str(job['streamer']); local_name=job.get('local_name')
    if local_name:
        if not re.fullmatch(r'test-[a-f0-9]{32}',vod_id): raise PipelineError('Некорректный ID теста')
        if local_name in ('.','..') or '/' in local_name or '\\' in local_name or Path(local_name).name != local_name:
            raise PipelineError('Некорректное имя тестового файла')
        return Path(root)/'recordings'/f'Тест {safe_name(Path(local_name).stem)} {vod_id}'
    if not vod_id.isdecimal(): raise PipelineError('Некорректный ID VOD')
    return Path(root)/'recordings'/f'{safe_name(login)} VOD {vod_id}'

def clip_stem(i,row): return f'{i:02d} {safe_name(row["title"])}'

def locate_clip_file(folder,name):
    found=[p for p in (folder/name,folder/READY_SILENCE/name,folder/READY_GENERIC/name) if p.exists()]
    if len(found)>1: raise PipelineError(f'Найдено несколько файлов {name}; не перезаписываю')
    return found[0] if found else None

def precise_timestamp(seconds):
    ms=round(float(seconds)*1000)
    return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02}.{ms%1000:03}'

def cut_report(folder,job,i,row,mode):
    stem=clip_stem(i,row)
    label='исходного тестового видео' if job.get('local_name') else 'исходной записи Twitch (VOD)'
    lines=[f'Нарезка: {row["title"]}',f'Интервал: {precise_timestamp(row["start"])} — {precise_timestamp(row["end"])}',
           f'Таймкоды указаны от начала {label}.', 'Границы указаны по плану монтажа; фактические границы могут немного отличаться из-за кадров и кодирования.', '']
    if mode is False:
        lines.append('Удаление тишины было выключено; паузы не вырезались.')
    elif mode is None:
        lines.append('Нет достоверных данных об удалении пауз для этой нарезки.')
    else:
        timeline=load_json(folder/'_work'/(stem+'.rendering.timeline.json'))
        cuts=timeline.get('cuts') if isinstance(timeline,dict) else None
        offset=0 if job.get('local_name') else math.floor(float(row['start']))
        checked=[]
        if isinstance(cuts,list):
            try:
                for cut in cuts:
                    a=float(cut['start'])+offset; b=float(cut['end'])+offset
                    if not math.isfinite(a) or not math.isfinite(b) or b<=a or a<float(row['start'])-.01 or b>float(row['end'])+.01:
                        raise ValueError()
                    checked.append((a,b))
                if any(a2 < b1-.01 for (_,b1),(a2,_) in zip(checked,checked[1:])): raise ValueError()
            except (ValueError,TypeError,KeyError,OverflowError): cuts=None
        if not isinstance(cuts,list):
            lines.append('Данные о вырезанных паузах недоступны или повреждены. Нельзя определить, были ли паузы удалены.')
        elif not checked:
            lines.append('Вырезанных пауз не найдено.')
        else:
            for n,(a,b) in enumerate(checked,1):
                lines.append(f'{n:03}. {precise_timestamp(a)} — {precise_timestamp(b)}  ({b-a:.3f} с)')
            lines.extend(['',f'Всего удалено: {len(checked)} пауз, {sum(b-a for a,b in checked):.3f} с.'])
    return '\n'.join(lines)+'\n'

def donation_report(job,row):
    label='исходного тестового видео' if job.get('local_name') else 'исходной записи Twitch (VOD)'
    lines=[f'Нарезка: {row["title"]}',f'Таймкоды от начала {label}.',
           'Интервалы определены ChatGPT по транскрипту и могут содержать ошибки; проверьте склейки.',
           'Интервалы могут частично пересекаться с удалённой тишиной.', '']
    cuts=row['donation_cuts']
    if not cuts: lines.append('Уверенных ответов на донаты не найдено; это не гарантия, что их не было.')
    for i,c in enumerate(cuts,1):
        lines.append(f"{i:03}. {precise_timestamp(c['start'])} — {precise_timestamp(c['end'])}  ({c['end']-c['start']:.3f} с)")
    return '\n'.join(lines)+'\n'

def organize_clips(folder,job,rows,mode):
    folder=Path(folder)
    ready=folder/(READY_SILENCE if mode is True else READY_GENERIC)
    plans=[]
    for i,row in enumerate(rows,1):
        stem=clip_stem(i,row); meta=load_json(folder/(stem+'.pipeline.json'))
        if not isinstance(meta,dict) or meta.get('vod_id')!=str(job['vod_id']) or meta.get('reaction')!=row:
            raise PipelineError(f'Не найдены подтверждённые данные нарезки {i}; файлы не перемещены')
        srcs=[]
        for ext in ('.mp4','.txt'):
            name=stem+ext; src=locate_clip_file(folder,name)
            if src is None or not src.is_file(): raise PipelineError(f'Не найден файл {name}; файлы не перемещены')
            dest=ready/name
            if src != dest and dest.exists(): raise PipelineError(f'Файл {name} уже существует; не перезаписываю')
            srcs.append((src,dest))
        report=ready/(stem+' — удалённая тишина.txt')
        text=cut_report(folder,job,i,row,mode)
        if report.exists() and report.read_text(encoding='utf-8-sig')!=text:
            raise PipelineError(f'Отчёт {report.name} уже существует и отличается; не перезаписываю')
        reports=[(report,text)]
        if 'donation_cuts' in row:
            dreport=ready/(stem+' — удалённые донаты.txt'); dtext=donation_report(job,row)
            if dreport.exists() and dreport.read_text(encoding='utf-8-sig')!=dtext:
                raise PipelineError(f'Отчёт {dreport.name} уже существует и отличается; не перезаписываю')
            reports.append((dreport,dtext))
        scan=meta.get('banner_scan')
        if isinstance(scan,list):
            breport=ready/(stem+' — баннеры.txt')
            blines=[f"Поиск баннеров по сохранённым образцам (папка banners): ключевые кадры, шаг ~{meta.get('banner_scan_interval',2)} с, границы уточнены.",
                    'Время — от начала исходного видео. Находятся только баннеры, для которых добавлен образец; новые, короткие и движущиеся баннеры могут быть пропущены или закрыты не полностью.',
                    'Указанные интервалы могут частично совпадать с удалённой тишиной или донатами.',
                    'Проверьте готовое видео. Если баннер пропущен — добавьте ещё один образец в настройках.', '']
            if not scan:blines.append('Баннеры не обнаружены. Это не гарантия их отсутствия.')
            for n,d in enumerate(scan,1):
                blines.append(f"{n:03}. {precise_timestamp(d['start'])} — {precise_timestamp(d['end'])}; "+(f"образец «{d['name']}»; " if d.get('name') else '')+f"область на сетке 0–1000: {d['box']}")
            btext='\n'.join(blines)+'\n'
            if breport.exists() and breport.read_text(encoding='utf-8-sig')!=btext: raise PipelineError(f'Отчёт {breport.name} уже существует и отличается; не перезаписываю')
            reports.append((breport,btext))
        plans.append((srcs,reports))
    ready.mkdir(parents=True,exist_ok=True)
    for srcs,reports in plans:
        for src,dest in srcs:
            if src != dest:
                if dest.exists(): raise PipelineError(f'Файл {dest.name} уже существует; не перезаписываю')
                os.rename(src,dest)
        for report,text in reports:
          if not report.exists():
            # Write completely before publishing; never overwrite a user's report.
            fd,tmp=tempfile.mkstemp(prefix='.report-',dir=ready)
            try:
                with os.fdopen(fd,'w',encoding='utf-8-sig',newline='\n') as f:
                    f.write(text); f.flush(); os.fsync(f.fileno())
                if os.name=='nt': os.rename(tmp,report)  # Windows refuses an existing destination
                else: os.link(tmp,report)  # atomic, exclusive on POSIX
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
    return ready,[str(pairs[0][1]) for pairs,_ in plans]

def timeline_matches_metadata(folder,stem,meta):
    # Old metadata did not record this switch. Only reuse a timeline whose
    # keep plan agrees with the finished clip, avoiding stale analysis files.
    timeline=load_json(folder/'_work'/(stem+'.rendering.timeline.json'))
    if not isinstance(timeline,dict) or not isinstance(meta,dict): return False
    keeps=timeline.get('keeps'); saved=meta.get('keep')
    if not isinstance(keeps,list) or not isinstance(saved,list) or len(keeps)!=len(saved) or not isinstance(timeline.get('cuts'),list): return False
    try:
        return all(abs(float(k['start'])-float(v[0]))<.01 and abs(float(k['end'])-float(v[1]))<.01 for k,v in zip(keeps,saved))
    except (ValueError,TypeError,KeyError,IndexError,OverflowError): return False

def organize_completed(root,job):
    if job.get('state')!='done': raise PipelineError('Выберите задание со статусом «Готово»')
    folder=job_output_folder(root,job)
    data=load_json(folder/'reactions.json')
    if not isinstance(data,dict) or data.get('vod_id')!=str(job['vod_id']) or not isinstance(data.get('reactions'),list):
        raise PipelineError('Не найдены данные о реакциях этого задания')
    rows=data['reactions']
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('title'),str): raise PipelineError('Некорректные данные о реакциях')
        try:
            a=float(row['start']); b=float(row['end'])
            if not math.isfinite(a) or not math.isfinite(b) or a<0 or b<=a: raise ValueError()
        except (KeyError,TypeError,ValueError,OverflowError): raise PipelineError('Некорректные данные о реакциях') from None
    metas=[load_json(folder/(clip_stem(i,row)+'.pipeline.json')) for i,row in enumerate(rows,1)]
    modes=[m.get('silence_enabled') if isinstance(m,dict) else None for m in metas]
    if modes and all(x is True for x in modes): mode=True
    elif modes and all(x is False for x in modes): mode=False
    elif modes and all(x is None for x in modes) and all(
            timeline_matches_metadata(folder,clip_stem(i,row),meta)
            for i,(row,meta) in enumerate(zip(rows,metas),1)): mode=True
    else: mode=None
    return organize_clips(folder,job,rows,mode)

class Pipeline:
    def __init__(self, root, finder, download=default_download, renderer=render_clip, speech=speech_chunks):
        self.root=Path(root).resolve(); self.finder=finder; self.download=download; self.renderer=renderer; self.speech=speech

    def run(self, job, cfg, progress=lambda stage,detail='':None):
        vod_id=str(job['vod_id']); login=str(job['streamer']); local_name=job.get('local_name')
        folder=job_output_folder(self.root,job)
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
                with report_wait(lambda text:progress('Скачивание',text),'Загрузка VOD через TwitchDownloader ещё выполняется'):
                    self.download(vod_id,'160p30',temp,cli=cli,threads=threads)
                duration=check_duration(temp) # do not promote a partial VOD into the cache
                os.replace(temp,src)
        cache=test_transcript_cache(self.root,src,job,progress,check_local) if local_name else work/'transcripts'
        check_local()
        segs,duration=self.speech(src,cache,cfg.get('speech',{}),progress,duration=duration)
        check_local()
        progress('Поиск реакций','Анализируется полный транскрипт')
        finder_cfg=cfg
        if isinstance(job.get('id'),int) and not isinstance(job['id'],bool) and job['id']>0:
            manual=load_json(manual_answer_path(self.root,job['id']))
            if isinstance(manual,dict) and manual.get('job')==job_identity(job) and isinstance(manual.get('text'),str):
                finder_cfg=dict(cfg,_manual_reactions=manual['text'])
        rows=validate_intervals(self.finder(str(src),segs,duration,login,finder_cfg,progress),duration,float(cfg.get('min_reaction_minutes',10))*60)
        check_local()
        if cfg.get('remove_donations',False) and rows:
            if not hasattr(self.finder,'find_donations'): raise PipelineError('Поиск донатов недоступен у текущего поставщика')
            manual_donation=None
            if isinstance(job.get('id'),int) and not isinstance(job['id'],bool) and job['id']>0:
                saved=load_json(manual_donation_path(self.root,job['id']))
                if isinstance(saved,dict) and saved.get('job')==job_identity(job) and isinstance(saved.get('text'),str): manual_donation=saved['text']
            cuts=validate_donation_cuts(self.finder.find_donations(segs,rows,progress,manual=manual_donation),rows)
            for i,row in enumerate(rows,1):
                row['donation_cuts']=[{'start':c['start'],'end':c['end']} for c in cuts if c['reaction']==i]
            check_local()
        atomic_json(folder/'reactions.json',{'vod_id':vod_id,'reactions':rows})
        outputs=[]
        for i,row in enumerate(rows,1):
            progress('Нарезка',f'{i}/{len(rows)} · {timestamp(row["start"])}–{timestamp(row["end"])}')
            stem=clip_stem(i,row); output=folder/(stem+'.mp4'); meta=folder/(stem+'.pipeline.json'); txt=folder/(stem+'.txt')
            old_output=locate_clip_file(folder,stem+'.mp4'); old_txt=locate_clip_file(folder,stem+'.txt')
            hash_data={'row':row,'quality':quality,'silence':cfg.get('silence',{})}
            if cfg.get('banner_blur',{}).get('enabled',False):hash_data['banner_blur']=cfg['banner_blur']
            config_hash=hashlib.sha256(json.dumps(hash_data,sort_keys=True).encode()).hexdigest()
            previous=load_json(meta)
            if old_output and old_txt and isinstance(previous,dict) and previous.get('key')==config_hash:
                media_duration(old_output); outputs.append(str(old_output)); continue
            if old_output or meta.exists() or old_txt: raise PipelineError('Найдены ранее созданные файлы с другими настройками; не перезаписываю')
            begin,end=full_cycle.download_bounds(row['start'],row['end'])
            raw=work/(f'clip-{i:04}.mp4'); temp=work/(f'clip-{i:04}.download.mp4'); rendered=folder/(stem+'.rendering.mp4')
            if rendered.exists():rendered.unlink()
            if local_name:
                check_local()
                render_source=src; offset=0
            else:
                if temp.exists():temp.unlink()
                if raw.exists():raw.unlink()
                progress('Скачивание',f'Нарезка {i}/{len(rows)}: загрузка фрагмента {timestamp(begin)}–{timestamp(end)} через TwitchDownloader')
                with report_wait(lambda text:progress('Скачивание',f'Нарезка {i}/{len(rows)} · {text}'),'Загрузка фрагмента через TwitchDownloader ещё выполняется'):
                    self.download(vod_id,quality,temp,begin=begin,end=end,cli=cli,threads=threads)
                media_duration(temp); os.replace(temp,raw)
                render_source=raw; offset=begin
            progress('Рендер',f'Нарезка {i}/{len(rows)}')
            if self.renderer is render_clip:
                def render_progress(detail): progress('Рендер',f'Нарезка {i}/{len(rows)} · {detail}')
                keeps,actual=self.renderer(render_source,rendered,row,offset,cfg.get('silence',{}),progress_callback=render_progress,banner_cfg=cfg.get('banner_blur',{}))
            else:
                keeps,actual=self.renderer(render_source,rendered,row,offset,cfg.get('silence',{}))
            check_local()
            progress('Проверка нарезки',f'Нарезка {i}/{len(rows)} · проверка таймкодов')
            mapped,planned=full_cycle.remap_segments(segs,offset,keeps,rendered_timeline=getattr(keeps,'rendered_timeline',None))
            if abs(actual-planned)>.5:
                raise PipelineError(f'Длительность видео {actual:.2f} с не совпала с монтажной картой {planned:.2f} с; таймкоды не сохранены')
            mapped=full_cycle.clip_segments(mapped,actual)
            # A partial render is never presented as completed.
            os.replace(rendered,output)
            warning=('Внимание: в нарезке есть сегмент с исправленной границей времени; текст и время рядом с ней требуют проверки.\n\n' if any(x.get('timing_warning')=='end_clipped' for x in mapped) else '')
            txt.write_text('Транскрипт готовой нарезки; времена от начала видео.\n\n'+warning+transcribe.to_txt(mapped),encoding='utf-8-sig')
            banner_scan=getattr(keeps,'banner_scan',None)
            if banner_scan is not None:
                banner_scan=[dict(d,start=round(d['start']+offset,3),end=round(d['end']+offset,3)) for d in banner_scan]
            atomic_json(meta,{'banner_scan':banner_scan,'banner_scan_interval':cfg.get('banner_blur',{}).get('interval',5),'key':config_hash,'vod_id':vod_id,'reaction':row,'keep':keeps,'render_timeline':getattr(keeps,'rendered_timeline',None),'duration':actual,'transcript':txt.name,'silence_enabled':bool(cfg.get('silence',{}).get('enabled',True))})
            if not local_name and not cfg.get('keep_original',False): raw.unlink(missing_ok=True)
            outputs.append(str(output))
        progress('Организация файлов','Собираются готовые нарезки и таймкоды удалённых пауз')
        ready,outputs=organize_clips(folder,job,rows,bool(cfg.get('silence',{}).get('enabled',True)))
        progress('Готово',f'Найдено реакций: {len(rows)}; сохранено видео: {len(outputs)}')
        return {'folder':str(ready),'outputs':outputs,'reactions':len(rows)}
