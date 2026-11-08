"""One monitor loop and one ordered job worker, independent from the GUI."""
from __future__ import annotations
import copy
import threading
import time
from collections import deque
from job_store import JobStore
from monitor import Monitor,TwitchClient
from pipeline import Pipeline
from bot_debug import redact

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
    def start(self):
        if any(t.is_alive() for t in self._threads): return not self.stop_event.is_set()
        interrupted=self.store.recover_interrupted(safe_stages=('Запуск','Скачивание','Транскрибация','Нарезка','Рендер','Готово'))
        if interrupted:self.log(f'Восстановлено прерванных заданий: {interrupted}; проверьте ошибки в очереди.')
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
                if stage=='Транскрибация': self.log(f"VOD {job['vod_id']}: {detail}")
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
            safe=current and current['stage'] in ('Скачивание','Транскрибация','Нарезка','Рендер')
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
