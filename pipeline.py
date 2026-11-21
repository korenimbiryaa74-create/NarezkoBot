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
import clip_recovery as checkpoints
import publication_guard
import banner_blur
import manual_blur
import stream_cutter
import transcribe
import whisper_cpp
import groq_speech
import long_streams
import low_impact
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
def report_wait(callback, description, interval=20, detail=None):
    """Report elapsed waiting time, not a fabricated percentage."""
    if callback is None:
        yield
        return
    stop=threading.Event(); started=time.monotonic()
    def report():
        while not stop.wait(interval):
            try:
                extra=''
                if detail is not None:
                    try: extra=detail() or ''
                    except Exception: extra=''
                callback(f'{description} · '+(extra+' · ' if extra else '')+f'ожидание {timestamp(time.monotonic()-started)}')
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

FAST_TEST_QUALITY='160p30'


def job_options_path(root, vod_id):
    vod_id=str(vod_id)
    if not vod_id.isdecimal(): raise ValueError('Некорректный ID записи')
    return Path(root)/'state'/'job_options'/f'{vod_id}.json'


def job_options(root, vod_id):
    """Per-VOD options set by «Запись Twitch по ссылке» (e.g. fast low-quality test)."""
    try: value=load_json(job_options_path(root,vod_id))
    except ValueError: return {}
    return value if isinstance(value,dict) else {}


def is_fast_test(root, job):
    return not job.get('local_name') and bool(job_options(root,job.get('vod_id')).get('fast_test'))


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
        row={'start':a,'end':min(b,duration),'title':str(item.get('title') or 'Момент')[:100]}
        if 'type' in item:
            if item['type'] not in ('reaction','discussion','game'): raise PipelineError('Неизвестный тип момента')
            row['type']=item['type']
        if isinstance(item.get('preview_text'),str): row['preview_text']=item['preview_text'].strip()[:80]
        if isinstance(item.get('source_url_hint'),str): row['source_url_hint']=item['source_url_hint'].strip()[:300]
        if isinstance(item.get('source_search_hint'),str): row['source_search_hint']=item['source_search_hint'].strip()[:120]
        if isinstance(item.get('video_title'),str): row['video_title']=item['video_title'].strip()[:100]
        if item.get('confidence') in ('high','medium','low'): row['confidence']=item['confidence']
        clean.append(row)
    clean.sort(key=lambda x:x['start'])
    if any(b['start']<a['end'] for a,b in zip(clean,clean[1:])):
        raise PipelineError('Модель вернула пересекающиеся интервалы')
    return clean

def validate_donation_cuts(cuts, rows):
    if not isinstance(cuts,list): raise PipelineError('Некорректный список донатов')
    # Донаты — необязательная чистка: сомнительный интервал пропускается (донат останется в видео),
    # задание из-за него не останавливается.
    clean=[]
    for c in cuts:
        if not isinstance(c,dict) or isinstance(c.get('reaction'),bool) or not isinstance(c.get('reaction'),int) or not 1<=c['reaction']<=len(rows): continue
        if rows[c['reaction']-1].get('type','reaction')!='reaction': continue
        try:
            if isinstance(c['start'],bool) or isinstance(c['end'],bool): raise ValueError()
            a,b=float(c['start']),float(c['end']);row=rows[c['reaction']-1]
            if not math.isfinite(a) or not math.isfinite(b): raise ValueError()
            a=max(a,float(row['start']));b=min(b,float(row['end']))
            if b-a<.5 or b-a>180 or b-a>(row['end']-row['start'])*.5: raise ValueError()
        except (ValueError,KeyError,TypeError,OverflowError): continue
        clean.append({'reaction':c['reaction'],'start':a,'end':b})
    clean.sort(key=lambda x:(x['reaction'],x['start']))
    merged=[]
    for c in clean:
        if merged and merged[-1]['reaction']==c['reaction'] and c['start']<merged[-1]['end']: merged[-1]['end']=max(merged[-1]['end'],c['end'])
        else: merged.append(dict(c))
    clean=[]
    for i,row in enumerate(rows,1):
        own=[c for c in merged if c['reaction']==i]
        if sum(c['end']-c['start'] for c in own)>(row['end']-row['start'])*.5: continue
        clean.extend(own)
    return clean

def media_duration(path):
    if not Path(path).is_file() or Path(path).stat().st_size<1000: raise PipelineError('Видеофайл не создан или пуст')
    ffprobe=shutil.which('ffprobe')
    if not ffprobe: raise PipelineError('ffprobe не найден')
    data=stream_cutter.probe(str(path),ffprobe)
    duration=float(stream_cutter.duration_of(data))
    if not math.isfinite(duration) or duration<=0: raise PipelineError('Некорректная длительность видео')
    return duration

# TwitchDownloaderCLI sometimes stops receiving parts from a Twitch CDN server without exiting:
# the process then hangs forever with no new bytes. We watch its parts folder and restart it.
DOWNLOAD_STALL_SECONDS=8*60          # no new parts for this long while downloading -> restart
DOWNLOAD_CONCAT_STALL_SECONDS=40*60  # ffmpeg concat of a very long VOD may legitimately be quiet
DOWNLOAD_ATTEMPTS=3
_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)

def _kill_tree(pid):
    try: subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=_NO_WINDOW,timeout=30)
    except Exception: pass

def kill_stale_downloaders(output):
    """Stop a TwitchDownloaderCLI left over from a previous bot run that still holds this output file."""
    if os.name!='nt': return 0
    target=str(Path(output)).lower(); killed=0
    script=("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-CimInstance Win32_Process -Filter \"Name='TwitchDownloaderCLI.exe'\" | % { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out=subprocess.run(['powershell','-NoProfile','-Command',script],capture_output=True,text=True,encoding='utf8',errors='replace',creationflags=_NO_WINDOW,timeout=60).stdout
    except Exception: return 0
    for line in (out or '').splitlines():
        pid,_,cmd=line.partition('\t')
        if pid.strip().isdigit() and target in cmd.lower() and int(pid)!=os.getpid():
            _kill_tree(int(pid)); killed+=1
    if killed: time.sleep(2)
    return killed

def remove_temp(path):
    """Delete our own temp file; if an orphaned downloader still holds it, stop that downloader first."""
    path=Path(path)
    if not path.exists(): return
    try: path.unlink()
    except PermissionError:
        kill_stale_downloaders(path); path.unlink()

def _parts_state(folder):
    total=count=0; concat=False
    for root,_dirs,files in os.walk(folder):
        for name in files:
            if name=='concat.txt': concat=True
            try: total+=os.path.getsize(os.path.join(root,name)); count+=1
            except OSError: pass
    return total,count,concat

class _DownloadResult:
    def __init__(self, returncode, stderr, stalled):
        self.returncode=returncode; self.stderr=stderr; self.stalled=stalled

# Live progress of running TwitchDownloader videodownloads, by output path: shown in the UI instead of a bare timer.
DOWNLOAD_STATUS={}
_STAGES={'downloading':'скачано','verifying':'проверка частей','combining':'склейка','finalizing':'завершение'}

def _download_status(log, total, count, concat, threads):
    pct=''; stage=''
    try:
        with open(log,'rb') as f:
            f.seek(0,2); size=f.tell(); f.seek(max(0,size-4096))
            tail=f.read().decode('utf8','replace')
        lines=[l for l in re.split(r'[\r\n]+',tail) if '[STATUS]' in l]
        if lines:
            m=re.search(r'\[STATUS\]\s*-\s*([A-Za-z]+)[^\d]*(\d+)%',lines[-1])
            if m: stage=_STAGES.get(m.group(1).lower(),m.group(1).lower()); pct=m.group(2)+'%'
    except OSError: pass
    if concat and not stage: stage='склейка'
    gb=total/1024**3
    parts=[(stage+' '+pct).strip() if (stage or pct) else 'скачано', (f'{gb:.1f} ГБ' if gb>=1 else f'{total/1024**2:.0f} МБ') if count else '', f'частей {count}' if count else '', f'{threads} потоков']
    return ' · '.join(p for p in parts if p)

def _run_download(command, cli, run_env, output, stall=None, concat_stall=None, attempts=None, poll=15):
    stall=stall or DOWNLOAD_STALL_SECONDS; concat_stall=concat_stall or DOWNLOAD_CONCAT_STALL_SECONDS
    attempts=attempts or DOWNLOAD_ATTEMPTS
    parts=output.with_name(output.name+'.parts'); log=output.with_name(output.name+'.log')
    result=_DownloadResult(-1,'',False)
    try: threads=command[command.index('-t')+1]
    except (ValueError,IndexError): threads='?'
    for _attempt in range(attempts):
        shutil.rmtree(parts,ignore_errors=True); remove_temp(output); parts.mkdir(parents=True,exist_ok=True)
        stalled=False
        with open(log,'w',encoding='utf8',errors='replace') as err:
            # stdout goes to the same log: its [STATUS] lines give the real progress percentage.
            proc=subprocess.Popen(command+['--temp-path',str(parts)],cwd=str(Path(cli).parent),env=run_env,stdout=err,stderr=err,creationflags=low_impact.creation_flags())
            try:
                changed=time.monotonic(); last=None; last_parts=None; next_scan=changed
                total=count=0; concat=False
                while proc.poll() is None:
                    try: proc.wait(timeout=poll); break
                    except subprocess.TimeoutExpired: pass
                    now=time.monotonic()
                    status=_download_status(log,0,0,False,threads)
                    try: out_size=os.path.getsize(output)
                    except OSError: out_size=0
                    state=(status,out_size)
                    if state!=last: last=state; changed=now
                    if now>=next_scan and now-changed>=min(60,stall/2):
                        total,count,concat=_parts_state(parts); next_scan=now+min(60,stall/2)
                        if (total,count)!=last_parts: last_parts=(total,count); changed=now
                    DOWNLOAD_STATUS[str(output)]=status
                    combining=concat or 'склейка' in status or 'завершение' in status
                    if now-changed>(concat_stall if combining else stall):
                        stalled=True; _kill_tree(proc.pid)
                        try: proc.wait(timeout=60)
                        except Exception: pass
                        break
            except BaseException:
                _kill_tree(proc.pid); raise
        try:
            with log.open('rb') as fh:
                fh.seek(max(0,fh.seek(0,2)-65536)); stderr=fh.read().decode('utf8','replace')
        except OSError: stderr=''
        DOWNLOAD_STATUS.pop(str(output),None)
        shutil.rmtree(parts,ignore_errors=True)
        try: log.unlink()
        except OSError: pass
        result=_DownloadResult(-1 if stalled else proc.returncode,stderr,stalled)
        if not stalled: return result
        remove_temp(output)
    return result

def default_download(vod_id, quality, output, begin=None, end=None, cli=None, threads=4):
    if not cli or not Path(cli).is_file(): raise PipelineError('Не найден TwitchDownloaderCLI в desktop/tools/TwitchDownloaderCLI. Запустите INSTALL.bat или проверьте установку.')
    output=Path(output)
    if output.exists(): raise PipelineError('Временный файл уже существует; не перезаписываю')
    # Adaptive: more threads when no game is running, the configured number while gaming (low_impact).
    threads=low_impact.download_threads(threads)
    command=[str(cli),'videodownload','--id',str(vod_id),'-q',quality,'-t',str(threads),'--collision','Rename','-o',str(output)]
    command.extend(low_impact.download_extra_args())
    if begin is not None: command.extend(['-b',timestamp(begin),'-e',timestamp(end)])
    # Download does not implicitly inherit proxies; service-specific proxy support must be explicit.
    env={k:v for k,v in os.environ.items() if not k.lower().endswith('_proxy')}
    env['NO_PROXY']='*'; env['no_proxy']='*'
    def run(run_env):
        return _run_download(command,cli,run_env,output)
    result=run(env)
    if result.returncode or not output.is_file():
        # Some Twitch VOD servers (a CloudFront distribution per recording) drop connections from the
        # local network while others work; the same download then succeeds through the configured proxy.
        proxy=''
        try:
            import proxy_setup
            proxy=proxy_setup.proxy_url()
        except Exception:
            proxy=''
        if proxy:
            remove_temp(output)  # our own partial temp file (checked absent above)
            import proxy_bridge
            bridge=proxy_bridge.ProxyBridge(proxy)
            local=bridge.start()
            try:
                penv={k:v for k,v in env.items() if k.lower()!='no_proxy'}
                penv.update({'HTTP_PROXY':local,'HTTPS_PROXY':local,'ALL_PROXY':local,'NO_PROXY':'127.0.0.1,localhost'})
                result=run(penv)
            finally:
                bridge.close()
    if result.returncode or not output.is_file():
        reason=[l.strip() for l in (result.stderr or '').splitlines() if 'Exception' in l and ':' in l][:1]
        if getattr(result,'stalled',False) and not reason: reason=['загрузка зависала без новых данных '+str(DOWNLOAD_STALL_SECONDS//60)+' мин, перезапуски не помогли']
        raise PipelineError('TwitchDownloader не смог скачать VOD (код '+str(result.returncode)+')'+(' — '+reason[0][:160] if reason else '')+('; через прокси тоже' if 'proxy' in locals() and proxy else ''))

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

_HWACCEL_CACHE: dict = {}
_HWACCEL_LOCK = threading.Lock()
_HWACCEL_RETRY_CPU = 600.0  # «не получилось» помним 10 мин: видеокарта могла быть занята другим рендером

def source_hwaccel(ffmpeg, src, vcodec, want='auto'):
    """Аппаратный декод для рендера нарезок (перенесено из v2, R2): cuda/d3d11va/dxva2 или None (CPU).

    Проверяется один раз на источник (путь + размер + время изменения + кодировщик): все нарезки одного VOD
    берут готовый ответ. Кадры идут через ОЗУ, поэтому блюр и уменьшение кадра работают как раньше;
    если кусок с аппаратным декодом упадёт, stream_cutter.run_jobs переделает его на CPU.
    CPU ffmpeg при рендере падает примерно на треть, параллельные задания тормозят меньше.
    want='none' выключает (pipeline.silence.hwaccel='none' в config.json)."""
    want = str(want or 'none').lower()
    if want == 'none':
        return None
    try:
        st = os.stat(src)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(str(src))), st.st_size, st.st_mtime_ns, str(vcodec), want, str(ffmpeg))
    with _HWACCEL_LOCK:
        hit = _HWACCEL_CACHE.get(key)
        if hit is not None and (hit[0] is not None or time.monotonic() - hit[1] < _HWACCEL_RETRY_CPU):
            return hit[0]
    try:
        hw = stream_cutter.detect_hwaccel(ffmpeg, str(src), want, str(vcodec))
    except (OSError, subprocess.SubprocessError, ValueError):
        hw = None  # нет ffmpeg/драйвера — рендер на CPU, как раньше
    with _HWACCEL_LOCK:
        _HWACCEL_CACHE[key] = (hw, time.monotonic())
    return hw

def render_clip(src, output, row, source_offset, silence_cfg, progress_callback=None, banner_cfg=None):
    ffmpeg,ffprobe=stream_cutter.require_tools()
    args=stream_cutter.parse_args([])
    args.out=str(output)
    args.parts_cache=str(Path(output).parent/'_work'/'render-parts')
    # Keep the cutter's generated timeline away from the user's input file.
    args.timeline=str(Path(output).parent/'_work'/(Path(output).stem+'.timeline.json'))
    args.min_cut=float(silence_cfg.get('min_cut',1.5)); args.max_gap=float(silence_cfg.get('max_gap',2.0))
    if row.get('type')=='game':
        # Gameplay: the streamer is often silent while playing; cut only long dead pauses.
        long_pause=float(silence_cfg.get('game_min_cut',20.0))
        args.min_cut=max(args.min_cut,long_pause); args.max_gap=max(args.max_gap,long_pause)
    args.onset_db=float(silence_cfg.get('onset_db',12.0))
    args.blur_regions=[]; args.precise=True; args.fast=False; args.map_timeline=True
    args.scale_height=int(silence_cfg.get('max_height',1080) or 0)
    args.maxrate=silence_cfg.get('maxrate','12M') or None
    # 'auto': eco while a game (Dota 2) is running, turbo otherwise (low_impact); an explicit preset is kept.
    speed=stream_cutter.apply_speed(args,low_impact.render_speed(silence_cfg.get('speed','auto')))
    if progress_callback: progress_callback(f'Рендер: пресет {speed} · {low_impact.mode_text()}')
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
    donation_cuts=[(c['start']-source_offset,c['end']-source_offset) for c in row.get('donation_cuts',[])] if row.get('type','reaction')=='reaction' else []
    if donation_cuts:
        if progress_callback: progress_callback(f'Удаление ответов на донаты: {len(donation_cuts)}')
        keeps=subtract_intervals(keeps,donation_cuts)
    if sum(b-a for a,b in keeps)<.5: raise PipelineError('После удаления тишины и донатов не осталось видео')
    res['keep']=keeps
    detected=None
    if (banner_cfg or {}).get('enabled',False):
        profile=manual_blur.validate_profile((banner_cfg or {}).get('profile'))
        detected=[{'start':start,'end':end,'box':box} for box in profile['boxes']]
        args.timed_banners=detected
        if progress_callback:progress_callback(f"Блюр по профилю {banner_cfg.get('login','')}: областей {len(detected)}; действует на всём видео")
    args.hwaccel_flag=source_hwaccel(ffmpeg,src,args.vcodec,silence_cfg.get('hwaccel','auto'))
    if progress_callback: progress_callback(f"Декод видео: {args.hwaccel_flag+' (видеокарта)' if args.hwaccel_flag else 'CPU'}")
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
        # A warning is kept alongside the clip even when YouTube is disabled.
        transcript=srcs[1][0].read_text(encoding='utf-8-sig')
        _, warning=publication_guard.assess(row,transcript,stem)
        if warning:
            preport=ready/(stem+' — проверка публикации.txt')
            ptext=warning+'\nНазвания передач могут не попасть в транскрипт. Проверка человеком обязательна.\n'
            if preport.exists() and preport.read_text(encoding='utf-8-sig')!=ptext:
                raise PipelineError(f'Отчёт {preport.name} уже существует и отличается; не перезаписываю')
            reports.append((preport,ptext))
        if 'donation_cuts' in row:
            dreport=ready/(stem+' — удалённые донаты.txt'); dtext=donation_report(job,row)
            if dreport.exists() and dreport.read_text(encoding='utf-8-sig')!=dtext:
                raise PipelineError(f'Отчёт {dreport.name} уже существует и отличается; не перезаписываю')
            reports.append((dreport,dtext))
        scan=meta.get('banner_scan')
        if isinstance(scan,list):
            breport=ready/(stem+' — баннеры.txt')
            if meta.get('banner_mode')=='manual':
                blines=[f"Ручной профиль блюра: {meta.get('banner_login','')}",
                        'Области размываются на протяжении всей нарезки, даже когда баннера нет.',
                        'Времена от начала исходного видео; координаты на сетке 0–1000.',
                        'Проверьте итог: положение баннера может измениться.', '']
                if not scan:blines.append('Для стримера выбрано «Без блюра».')
            else:
                blines=[f"Поиск баннеров по сохранённым образцам (папка banners): ключевые кадры, шаг ~{meta.get('banner_scan_interval',2)} с, границы уточнены.",
                        'Время — от начала исходного видео. Новые, короткие и движущиеся баннеры могут быть пропущены или закрыты не полностью.',
                        'Проверьте готовое видео.', '']
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
        banner_cfg=dict(cfg.get('banner_blur',{}))
        if banner_cfg.get('enabled',False):
            blur_login=banner_cfg.get('test_streamer','') if local_name else login
            if not blur_login:
                raise banner_blur.BannerSetupError('Для тестового видео укажите ник стримера в настройках блюра')
            banner_cfg['login']=manual_blur.normalize_login(blur_login)
            if manual_blur.profile_path(self.root,blur_login).exists():
                banner_cfg['profile']=manual_blur.load_profile(self.root,blur_login)
            else:
                # Новый стример без профиля: не роняем всю запись, режем без блюра и сообщаем.
                banner_cfg['enabled']=False
                progress('Подготовка',f"Блюр для {banner_cfg['login']} не настроен — нарезки без блюра (настройте в разделе «Блюр»)")
        folder=job_output_folder(self.root,job)
        folder.mkdir(parents=True,exist_ok=True)
        work=folder/'_work'; work.mkdir(exist_ok=True)
        cli=cfg.get('cli_path') or str(self.root/'desktop'/'tools'/'TwitchDownloaderCLI'/'TwitchDownloaderCLI.exe')
        threads=int(cfg.get('download_threads',4)); quality=cfg.get('quality','1080p60')
        # Fast test: clips are cut straight from the 160p file already downloaded for transcription.
        fast_test=is_fast_test(self.root,job)
        if fast_test: quality=FAST_TEST_QUALITY
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
        head_limit=None  # «обычный» режим длинных стримов: только первые N часов
        if local_name:
            progress('Скачивание','Проверка локального тестового файла: '+local_name)
            check_local()
            src=self.root/'test_input'/local_name
            duration=media_duration(src)
        else:
            src=work/'full-160p.mp4'
            progress('Скачивание','Загружается запись для транскрибации')
            # Chat is needed later for game clips: download it now, in parallel with the video and
            # transcription, instead of after them. The finder then takes the ready chat.json.
            chat_mode=(cfg.get('_streamer_modes') or {}).get(str(login).lower()) or {}
            if chat_mode.get('game',False):
                try:
                    import twitch_vod
                    # Background chat updates must never overwrite the foreground GPT/recovery stage.
                    twitch_vod.prefetch_chat(vod_id,work,cli,log=lambda m:None)
                except Exception as exc:
                    progress('Скачивание',f'Фоновая загрузка чата не запущена ({type(exc).__name__}) — чат скачается позже')
            expected=job.get('duration_seconds')
            head_limit=long_streams.normal_limit(cfg,expected)
            if head_limit:
                progress('Скачивание',f'Стрим {timestamp(float(expected))} длиннее {head_limit/3600:g} ч — режим «обычный»: '
                                      f'скачиваю и обрабатываю только первые {head_limit/3600:g} ч')
                if not src.exists():src=work/f'first-{int(head_limit)}s-160p.mp4'
                expected=head_limit
            def check_duration(path):
                actual=media_duration(path)
                if expected and actual < float(expected)-max(5.,float(expected)*.005):
                    raise IncompleteVod('Запись короче заявленной Twitch; не обрабатываю неполный VOD')
                return actual
            if src.exists():
                # Preserve old downloads for inspection but do not reuse a known short file.
                try:
                    duration=check_duration(src)
                    progress('Скачивание','Исходная запись для транскрибации из кэша')
                except IncompleteVod:
                    import uuid
                    os.replace(src,work/('full-160p.incomplete-'+uuid.uuid4().hex+'.mp4'))
                    raise
            else:
                temp=work/'full-160p.downloading.mp4'
                remove_temp(temp) # only our own explicitly named temporary file (stops an orphaned downloader)
                with report_wait(lambda text:progress('Скачивание',text),'Загрузка VOD через TwitchDownloader',detail=lambda:DOWNLOAD_STATUS.get(str(temp),'')):
                    if head_limit and src.name.startswith('first-'):
                        self.download(vod_id,'160p30',temp,begin=0,end=head_limit,cli=cli,threads=threads)
                    else:
                        self.download(vod_id,'160p30',temp,cli=cli,threads=threads)
                duration=check_duration(temp) # do not promote a partial VOD into the cache
                os.replace(temp,src)
        head=long_streams.normal_limit(cfg,duration)
        if head and duration>head:
            if not head_limit:
                progress('Транскрибация',f'Запись {timestamp(duration)} длиннее {head/3600:g} ч — режим «обычный»: обрабатываю только первые {head/3600:g} ч')
            duration=head
        cache=test_transcript_cache(self.root,src,job,progress,check_local) if local_name else work/'transcripts'
        check_local()
        segs,duration=self.speech(src,cache,cfg.get('speech',{}),progress,duration=duration)
        check_local()
        progress('Поиск реакций','Поиск реакций и интересных моментов по полному транскрипту')
        finder_cfg=cfg
        if isinstance(job.get('id'),int) and not isinstance(job['id'],bool) and job['id']>0:
            manual=load_json(manual_answer_path(self.root,job['id']))
            if isinstance(manual,dict) and manual.get('job')==job_identity(job) and isinstance(manual.get('text'),str):
                finder_cfg=dict(cfg,_manual_reactions=manual['text'])
        import youtube_titles
        known_name=youtube_titles.streamer_name(job)
        finder_login=(known_name or cfg.get('preview',{}).get('test_streamer','') if local_name else login)
        # Game clips need the VOD's chat and chapters; the finder downloads them only when «Игры» is on.
        if not local_name: finder_cfg=dict(finder_cfg,_vod_id=vod_id,_work=str(work),_cli=cli)
        rows=validate_intervals(self.finder(str(src),segs,duration,finder_login,finder_cfg,progress),duration,float(cfg.get('min_reaction_minutes',10))*60)
        display_name=known_name or (youtube_titles.ALIASES.get(str(finder_login).lower(),str(finder_login)) if finder_login else '')
        # Reaction titles come from the same answer: «<СТРИМЕР> СМОТРИТ: <ролик>».
        for row in rows:
            if row.get('type')=='reaction' and not row.get('video_title'):row['video_title']=youtube_titles.title_from_moment(row,display_name)
        for row in rows:
            row['video_title']=youtube_titles.title_from_moment(row,display_name)
        check_local()
        if cfg.get('remove_donations',False) and any(r.get('type','reaction')=='reaction' for r in rows):
            if not hasattr(self.finder,'find_donations'): raise PipelineError('Поиск донатов недоступен у текущего поставщика')
            manual_donation=None
            if isinstance(job.get('id'),int) and not isinstance(job['id'],bool) and job['id']>0:
                saved=load_json(manual_donation_path(self.root,job['id']))
                if isinstance(saved,dict) and saved.get('job')==job_identity(job) and isinstance(saved.get('text'),str): manual_donation=saved['text']
            cuts=validate_donation_cuts(self.finder.find_donations(segs,rows,progress,manual=manual_donation),rows)
            for i,row in enumerate(rows,1):
                if row.get('type','reaction')=='reaction':
                    row['donation_cuts']=[{'start':c['start'],'end':c['end']} for c in cuts if c['reaction']==i]
            check_local()
        atomic_json(folder/'reactions.json',{'vod_id':vod_id,'reactions':rows})
        outputs=[]
        # Пока рендерится нарезка, следующая уже скачивается в фоне: интернет и рендер друг другу не мешают.
        # Одновременно идёт только одна загрузка; при её ошибке нарезка просто скачивается заново обычным путём.
        prefetch={}
        def get_fragment(j,b,e):
            raw=work/f'clip-{j:04}.mp4'; temp=work/f'clip-{j:04}.download.mp4'
            ident=[vod_id,quality,b,e]; minimum=rows[j-1]['end']-b
            try: hit=checkpoints.cached_fragment(raw,ident,minimum,media_duration)
            except stream_cutter.CutterError: hit=False
            if hit: return True
            remove_temp(temp)
            self.download(vod_id,quality,temp,begin=b,end=e,cli=cli,threads=threads)
            actual=media_duration(temp)
            if actual+.3<minimum: raise IncompleteVod('Скачанный фрагмент короче найденного интервала')
            os.replace(temp,raw)
            checkpoints.save(raw.with_suffix('.source.json'),{'identity':ident,'stamp':checkpoints.stamp(raw),'duration':actual})
            return False
        def start_prefetch(j):
            if local_name or fast_test or j>len(rows) or j in prefetch:return
            nrow=rows[j-1];nstem=clip_stem(j,nrow)
            if locate_clip_file(folder,nstem+'.mp4') or locate_clip_file(folder,nstem+'.txt') or (folder/(nstem+'.pipeline.json')).exists():return
            nb,ne=full_cycle.download_bounds(nrow['start'],nrow['end'])
            state={'done':threading.Event(),'error':None}
            def run():
                try:
                    state['cached']=get_fragment(j,nb,ne)
                except Exception as exc:
                    state['error']=exc
                finally:
                    state['done'].set()
            threading.Thread(target=run,name=f'prefetch-clip-{j}',daemon=True).start()
            prefetch[j]=state
        try:
            for i,row in enumerate(rows,1):
                progress('Нарезка',f"{i}/{len(rows)} · {({'reaction':'Реакция','discussion':'Обсуждение/момент','game':'Игра'}.get(row.get('type'),'Момент'))} · {timestamp(row['start'])}–{timestamp(row['end'])}")
                stem=clip_stem(i,row); output=folder/(stem+'.mp4'); meta=folder/(stem+'.pipeline.json'); txt=folder/(stem+'.txt')
                old_output=locate_clip_file(folder,stem+'.mp4'); old_txt=locate_clip_file(folder,stem+'.txt')
                hash_data={'row':row,'quality':quality,'silence':cfg.get('silence',{})}
                if banner_cfg.get('enabled',False):hash_data['banner_blur']=banner_cfg
                config_hash=hashlib.sha256(json.dumps(hash_data,sort_keys=True).encode()).hexdigest()
                journal=work/(stem+'.commit.json'); rendered=folder/(stem+'.rendering.mp4')
                if journal.exists():
                    checkpoints.complete_bundle(journal,config_hash,rendered,output,txt,meta,media_duration)
                    progress('Проверка нарезки',f'Нарезка {i}: метаданные восстановлены без рендера')
                    old_output,old_txt=output,txt
                previous=load_json(meta)
                if old_output and old_txt and isinstance(previous,dict) and previous.get('key')==config_hash:
                    progress('Нарезка',f'Нарезка {i}: готовое видео из кэша')
                    media_duration(old_output); outputs.append(str(old_output)); continue
                if old_output or meta.exists() or old_txt: raise PipelineError('Найдены ранее созданные файлы с другими настройками; не перезаписываю')
                begin,end=full_cycle.download_bounds(row['start'],row['end'])
                raw=work/(f'clip-{i:04}.mp4'); temp=work/(f'clip-{i:04}.download.mp4'); rendered=folder/(stem+'.rendering.mp4')
                if rendered.exists():rendered.unlink()
                if local_name:
                    check_local()
                    render_source=src; offset=0
                elif fast_test:
                    progress('Скачивание',f'Нарезка {i}/{len(rows)}: быстрый тест — режу из записи 160p без повторной загрузки')
                    render_source=src; offset=0
                else:
                    pre=prefetch.pop(i,None)
                    if pre is not None:
                        if not pre['done'].is_set():
                            progress('Скачивание',f'Нарезка {i}/{len(rows)}: фрагмент {timestamp(begin)}–{timestamp(end)} уже качается в фоне — дожидаюсь')
                            with report_wait(lambda text:progress('Скачивание',f'Нарезка {i}/{len(rows)} · {text}'),'Фоновая загрузка фрагмента ещё выполняется'):
                                pre['done'].wait()
                        if pre['error'] is not None or not raw.exists():
                            progress('Скачивание',f"Нарезка {i}/{len(rows)}: фоновая загрузка не удалась ({type(pre['error']).__name__ if pre['error'] else 'нет файла'}) — качаю заново")
                            pre=None
                        else:
                            msg='фрагмент из кэша' if pre.get('cached') else 'фрагмент уже скачан в фоне'
                            progress('Скачивание',f'Нарезка {i}/{len(rows)}: {msg}')
                    if pre is None:
                        progress('Скачивание',f'Нарезка {i}/{len(rows)}: загрузка фрагмента {timestamp(begin)}–{timestamp(end)} через TwitchDownloader')
                        with report_wait(lambda text:progress('Скачивание',f'Нарезка {i}/{len(rows)} · {text}'),'Загрузка фрагмента через TwitchDownloader',detail=lambda:DOWNLOAD_STATUS.get(str(temp),'')):
                            cached=get_fragment(i,begin,end)
                        if cached: progress('Скачивание',f'Нарезка {i}: использован фрагмент из кэша')
                    render_source=raw; offset=begin
                start_prefetch(i+1)  # следующая нарезка качается, пока рендерится эта
                progress('Рендер',f'Нарезка {i}/{len(rows)}')
                if self.renderer is render_clip:
                    def render_progress(detail): progress('Рендер',f'Нарезка {i}/{len(rows)} · {detail}')
                    keeps,actual=self.renderer(render_source,rendered,row,offset,cfg.get('silence',{}),progress_callback=render_progress,banner_cfg=banner_cfg)
                else:
                    keeps,actual=self.renderer(render_source,rendered,row,offset,cfg.get('silence',{}))
                check_local()
                progress('Проверка нарезки',f'Нарезка {i}/{len(rows)} · проверка таймкодов')
                mapped,planned=full_cycle.remap_segments(segs,offset,keeps,rendered_timeline=getattr(keeps,'rendered_timeline',None))
                if abs(actual-planned)>.5:
                    raise PipelineError(f'Длительность видео {actual:.2f} с не совпала с монтажной картой {planned:.2f} с; таймкоды не сохранены')
                mapped=full_cycle.clip_segments(mapped,actual)
                # A partial render is never presented as completed.
                warning=('Внимание: в нарезке есть сегмент с исправленной границей времени; текст и время рядом с ней требуют проверки.\n\n' if any(x.get('timing_warning')=='end_clipped' for x in mapped) else '')
                category={'reaction':'Реакция на видео','discussion':'Обсуждение / другой интересный момент','game':'Игра целиком'}.get(row.get('type'))
                heading=('Тип момента: '+category+'\n' if category else '')+'Транскрипт готовой нарезки; времена от начала видео.\n\n'
                transcript=heading+warning+transcribe.to_txt(mapped)
                banner_scan=getattr(keeps,'banner_scan',None)
                if banner_scan is not None:
                    banner_scan=[dict(d,start=round(d['start']+offset,3),end=round(d['end']+offset,3)) for d in banner_scan]
                metadata={'banner_scan':banner_scan,'banner_mode':('manual' if banner_scan is not None else None),'banner_login':banner_cfg.get('login',''),'banner_scan_interval':cfg.get('banner_blur',{}).get('interval',5),'key':config_hash,'vod_id':vod_id,'reaction':row,'keep':keeps,'render_timeline':getattr(keeps,'rendered_timeline',None),'duration':actual,'transcript':txt.name,'silence_enabled':bool(cfg.get('silence',{}).get('enabled',True))}
                checkpoints.save(journal,{'key':config_hash,'output':output.name,'stamp':checkpoints.stamp(rendered),'meta':metadata,'text':transcript})
                checkpoints.complete_bundle(journal,config_hash,rendered,output,txt,meta)
                if not local_name and not cfg.get('keep_original',False):
                    raw.unlink(missing_ok=True); raw.with_suffix('.source.json').unlink(missing_ok=True)
                outputs.append(str(output))
        finally:
            # Ошибка задания: дожидаемся фоновой загрузки, чтобы её файлы не мешали повторной попытке.
            for state in prefetch.values():state['done'].wait()
        progress('Организация файлов','Собираются готовые нарезки и таймкоды удалённых пауз')
        ready,outputs=organize_clips(folder,job,rows,bool(cfg.get('silence',{}).get('enabled',True)))
        # Previews: ChatGPT remakes a template thumbnail with this clip's caption and context.
        # Failures never stop the job; previews can be regenerated later from the «Превью» screen.
        preview_cfg=cfg.get('preview',{})
        if preview_cfg.get('enabled',False):
            import gpt_previews
            preview_login=(preview_cfg.get('test_streamer','') if local_name else login) or finder_login
            for i,(row,video) in enumerate(zip(rows,outputs),1):
                target=Path(video).with_name(Path(video).stem+' — ИИ-превью.jpg')
                if target.exists():
                    progress('Превью',f'Нарезка {i}: превью уже есть; не перезаписываю');continue
                progress('Превью',f'Нарезка {i}/{len(rows)}: генерирую превью в ChatGPT')
                try:
                    info=gpt_previews.generate(self.root,self.finder,preview_login,row,target,preview_cfg,log=lambda m,i=i:progress('Превью',f'Нарезка {i}: {m}'))
                    progress('Превью',f"Нарезка {i}: превью сохранено (шаблон {info['template']})")
                except Exception as exc:
                    progress('Превью',f'Нарезка {i}: превью не готово ({type(exc).__name__}): {str(exc)[:250]}')
        progress('Готово',f'Найдено моментов: {len(rows)}; сохранено видео: {len(outputs)}')
        return {'folder':str(ready),'outputs':outputs,'reactions':len(rows),'moments':rows}
