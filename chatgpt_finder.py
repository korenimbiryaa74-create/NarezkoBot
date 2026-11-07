"""Reaction finder using an existing ChatGPT browser profile, without Telegram.

No account rotation or automatic retry on quota, safety, or uncertain delivery.
The user's login is performed explicitly in a normal browser window.
"""
from __future__ import annotations
import hashlib
import os
import threading
import time
from pathlib import Path
import full_cycle
import reactions

class ChatGPTFinder:
    def __init__(self, root, web_factory=None, timeout=1500):
        self.root=Path(root).resolve(); self.timeout=int(timeout)
        self._lock=threading.RLock(); self._web=None; self._factory=web_factory

    def _browser(self):
        if self._web is None:
            if self._factory is None:
                from chatgpt_web import ChatGPTWeb
                factory=ChatGPTWeb
            else: factory=self._factory
            account=self.root/'gpt_accounts'/'acc_01'
            self._web=factory(cookies_path=str(account/'cookies.json'),out_dir=str(self.root/'gpt_uploads'),
                headless=False,port=9300,profile=str(account/'profile'),timeout=self.timeout,verbose=False,offscreen=False)
        return self._web

    def login(self, timeout=300):
        """Caller must run in a background thread; user logs in using the window."""
        with self._lock:
            web=self._browser(); web.offscreen=False; web.start()
            if web.tab is None:
                web.tab=web._open_tab('https://chatgpt.com/auth/login')
                web.tab.call('Network.enable')
            until=time.monotonic()+timeout
            while time.monotonic()<until:
                if web._has_session():
                    web.dump_cookies(); return True
                time.sleep(3)
            return False

    def _ask_document(self,instructions,document,filename,timeout,log=None,need='reactions'):
        with self._lock:
            prompt=instructions+'\n\nПроанализируй приложенный файл с транскриптом.'
            key=full_cycle.response_key(prompt,document,filename)
            cache=self.root/'gpt_uploads'/'response_cache'/f'{key}.json'
            predicate=lambda text:reactions.answer_has(text,need)
            cached=full_cycle.cached_response(str(cache),key,predicate)
            if cached:
                if log: log('ChatGPT: использован проверенный кэш ответа')
                return cached
            folder=self.root/'gpt_uploads'/hashlib.sha256(document.encode('utf8')).hexdigest()[:16]
            folder.mkdir(parents=True,exist_ok=True)
            # A fixed name prevents untrusted title/path characters entering paths.
            path=folder/'transcript.txt'
            path.write_text(document,encoding='utf8')
            web=self._browser(); web.timeout=int(timeout); web.offscreen=False
            if log: log('ChatGPT: отправка транскрипта и ожидание ответа')
            # Do not automatically repeat: delivery may be uncertain.
            text=(web.ask(prompt,[str(path)],until=predicate).get('text') or '').strip()
            if not predicate(text): raise RuntimeError('ChatGPT вернул неполный ответ; задача остановлена')
            cache.parent.mkdir(parents=True,exist_ok=True)
            full_cycle.atomic_json(str(cache),{'key':key,'text':text})
            return text

    def __call__(self, video, segs, duration, streamer, cfg, progress):
        settings={'reactions':{'mode':'whole','min_minutes':float(cfg.get('min_reaction_minutes',10)),
                   'bridge_minutes':float(cfg.get('bridge_minutes',5)), 'chatgpt_timeout':self.timeout}}
        result=reactions.find_reactions_whole(video,segs,duration,settings,
            extra={'chatgpt_doc':self._ask_document},who=streamer,
            log=lambda message:progress('Поиск реакций',message))
        return result['reactions']

    def close(self):
        with self._lock:
            if self._web:
                self._web.close(); self._web=None
