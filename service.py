"""One monitor loop and one ordered job worker, independent from the GUI."""
from __future__ import annotations
import copy
import threading
import time
from collections import deque
from job_store import JobStore
from monitor import Monitor,TwitchClient
from pipeline import Pipeline, atomic_json, manual_answer_path, manual_donation_path, job_identity, organize_completed
import reactions
from stream_cutter import InvalidRenderTimeline
from bot_debug import redact
from banner_blur import BannerSetupError

class Service:
    def __init__(self,root,settings,finder,api_factory=TwitchClient,pipeline_factory=Pipeline):
        self.root=root; self.settings=settings; self.finder=finder
        self.api_factory=api_factory; self.pipeline_factory=pipeline_factory
        self.store=JobStore(root/'state'/'jobs.sqlite3')
        self.stop_event=threading.Event(); self.wake=threading.Event()
        self._threads=[]; self._logs=deque(maxlen=200); self._lock=threading.Lock()
        self._monitor=None; self._monitor_key=None

    def log(self,text):
        with self._lock: self._logs.append((time.time(),str(text)))
    def logs(self):
        with self._lock: return list(self._logs)
    def recover_interrupted(self):
        if any(t.is_alive() for t in self._threads): return 0
        interrupted=self.store.recover_interrupted(safe_stages=('Запуск','Скачивание','Транскрибация','Нарезка','Рендер','Организация файлов','Готово'))
        if interrupted:self.log(f'Восстановлено прерванных заданий: {interrupted}; проверьте ошибки в очереди.')
        return interrupted
    def start(self):
        if any(t.is_alive() for t in self._threads): return not self.stop_event.is_set()
        self.recover_interrupted()
        self.stop_event.clear()
        self._threads=[threading.Thread(target=self._monitor_loop,name='twitch-monitor',daemon=True),
                       threading.Thread(target=self._worker_loop,name='clip-worker',daemon=True)]
        for t in self._threads:t.start()
        self.log('Служба запущена')
        return True
    def request_stop(self):
        self.stop_event.set(); self.wake.set()
        self.log('Остановка: текущая операция завершится перед выходом')
    def settings_changed(self): self.wake.set()
    def _get_monitor(self,cfg):
        tw=cfg['twitch']; p=cfg['proxy']
        proxy=p['url'] if p['enabled'] and p.get('twitch_api') else ''
        key=(tw['client_id'],tw['client_secret'],proxy)
        if self._monitor is None or self._monitor_key!=key:
            api=self.api_factory(tw['client_id'],tw['client_secret'],proxy)
            self._monitor=Monitor(self.store,api); self._monitor_key=key
        return self._monitor
    def queue_test_inputs(self):
        folder=self.root/'test_input'; folder.mkdir(parents=True,exist_ok=True)
        extensions={'.mp4','.mkv','.mov','.webm','.m4v','.avi'}
        added=0; active=0
        for path in sorted(folder.iterdir(),key=lambda p:p.name.casefold()):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in extensions: continue
            st=path.stat()
            if st.st_size <= 0: continue
            _,created=self.store.add_local(path.name,st.st_size,st.st_mtime_ns)
            if created: added+=1
            else: active+=1
        if added: self.log(f'Тестовых видео добавлено в очередь: {added}')
        return added,active

    def save_manual_reactions(self, job_id, text):
        job=next((j for j in self.store.list_jobs(1000) if j['id']==job_id),None)
        if not job or job['state']!='failed':
            raise ValueError('Выберите задание со статусом «Ошибка». Дождитесь завершения или перезапустите приложение.')
        if not isinstance(text,str) or not text.strip() or len(text)>1_000_000:
            raise ValueError('Буфер пуст или ответ слишком большой')
        # A separately requested donation answer can also be recovered manually.
        donation=reactions.tc.parse_answer(text).get('donation_cuts')
        if donation is not None:
            if not isinstance(donation,list): raise ValueError('Некорректный список donation_cuts')
            intervals=[]
            for c in donation:
                if not isinstance(c,dict) or isinstance(c.get('reaction'),bool) or not isinstance(c.get('reaction'),int) or c['reaction']<1 or c.get('confidence')!='high':
                    raise ValueError('Некорректный интервал donation_cuts')
                a,b=reactions.reaction_time(c.get('start')),reactions.reaction_time(c.get('end'))
                if a is None or b is None or b<=a: raise ValueError('Некорректные таймкоды donation_cuts')
                intervals.append((c['reaction'],a,b))
            intervals.sort()
            if any(x[0]==y[0] and y[1]<x[2] for x,y in zip(intervals,intervals[1:])): raise ValueError('Интервалы donation_cuts пересекаются')
            atomic_json(manual_donation_path(self.root,job_id),{'job':job_identity(job),'text':text})
            self.log(f"VOD {job['vod_id']}: сохранён ответ ChatGPT о донатах для ручного продолжения")
            return len(intervals)
        try:
            parsed=reactions.parse_reaction_answer(text)
            intervals=[]
            for row in parsed['reactions']:
                if not isinstance(row,dict): raise ValueError()
                a,b=reactions.reaction_time(row.get('start')),reactions.reaction_time(row.get('end'))
                if a is None or b is None or b<=a: raise ValueError()
                intervals.append((a,b))
            intervals.sort()
            if any(b[0]<a[1] for a,b in zip(intervals,intervals[1:])): raise ValueError()
        except (ValueError,TypeError,KeyError,reactions.ReactionsError):
            raise ValueError('В буфере нет полного ответа reactions с корректными непересекающимися интервалами') from None
        atomic_json(manual_answer_path(self.root,job_id),{'job':job_identity(job),'text':text})
        self.log(f"VOD {job['vod_id']}: сохранён ответ ChatGPT для ручного продолжения")
        return len(intervals)

    def organize_finished(self,job_id):
        job=next((j for j in self.store.list_jobs(1000) if j['id']==job_id),None)
        if not job: raise ValueError('Задание не найдено в истории')
        ready,outputs=organize_completed(self.root,job)
        self.log(f"VOD {job['vod_id']}: готовые файлы собраны: {ready}")
        return ready,len(outputs)

    def monitor_once(self):
        cfg=self.settings.load()
        if not cfg['monitor']['enabled']: return
        if not cfg['streamers']: return
        monitor=self._get_monitor(cfg)
        for name in cfg['streamers']:
            if self.stop_event.is_set():break
            try: monitor.tick(name)
            except Exception as exc:
                # Never include error message: URLs may carry credentials.
                self.log(f'{name}: ошибка мониторинга ({type(exc).__name__})')
    def _monitor_loop(self):
        while not self.stop_event.is_set():
            try:
                cfg=self.settings.load(); self.monitor_once(); delay=cfg['monitor']['interval']
            except Exception as exc:
                self.log(f'Мониторинг недоступен ({type(exc).__name__})'); delay=60
            self.wake.wait(delay); self.wake.clear()
    def worker_once(self):
        job=self.store.claim()
        if not job:return False
        cfg=None
        try:
            cfg=copy.deepcopy(self.settings.load())
            engine=self.pipeline_factory(self.root,self.finder)
            speech=cfg['pipeline']['speech']
            speech['groq_proxy_url']=cfg['proxy']['url'] if cfg['proxy']['enabled'] and cfg['proxy'].get('groq') else ''
            secrets=(cfg['twitch']['client_secret'],cfg['proxy']['url'],speech.get('groq_api_key',''))
            def clean(text):
                text=redact(str(text))
                for secret in secrets:
                    if secret: text=text.replace(secret,'[скрыто]')
                return text
            def progress(stage,detail=''):
                detail=clean(detail)
                self.store.update_stage(job['id'],stage,detail)
                if stage in ('Скачивание','Транскрибация','Поиск реакций','Нарезка','Рендер','Проверка нарезки','Организация файлов'): self.log(f"VOD {job['vod_id']}: {detail}")
            outcome=engine.run(job,cfg['pipeline'],progress)
            self.store.finish(job['id'],'done',f"Видео: {len(outcome['outputs'])}; {outcome['folder']}")
            self.log(f"VOD {job['vod_id']}: готово")
        except Exception as exc:
            # No replay without reconciliation: a web request may have been delivered.
            detail=redact(f'{type(exc).__name__}: {str(exc)[:2000]}')
            # Defensive redact of configured secrets and proxy credentials.
            for saved in (cfg,):
                try:
                    for secret in (saved['twitch']['client_secret'],saved['proxy']['url'],saved['pipeline']['speech'].get('groq_api_key','')):
                        if secret: detail=detail.replace(secret,'[скрыто]')
                except (TypeError,KeyError): pass
            current=next((x for x in self.store.list_jobs(1000) if x['id']==job['id']),None)
            safe=current and current['stage'] in ('Скачивание','Транскрибация','Нарезка','Рендер','Организация файлов') and not isinstance(exc,(InvalidRenderTimeline,BannerSetupError))
            if safe and job['attempts']<3:
                delay=60*job['attempts']
                self.store.finish(job['id'],'retry',detail,retry_after=delay)
                self.log(f"VOD {job['vod_id']}: повтор через {delay} с — {detail}")
            else:
                self.store.finish(job['id'],'failed',detail)
                self.log(f"VOD {job['vod_id']}: ошибка — {detail}")
        return True
    def _worker_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.worker_once(): continue
            except Exception as exc:self.log(f'Ошибка очереди ({type(exc).__name__})')
            self.stop_event.wait(5)
