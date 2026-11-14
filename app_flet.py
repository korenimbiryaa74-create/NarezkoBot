"""Нарезкобот — Windows control panel on Flet. All background work is owned by this process.

Env (for development only):
  NAREZKO_UI_DEMO=1          read-only preview: no recovery, no monitoring start, no instance lock
  NAREZKO_UI_SELFTEST=<file> open every screen, write a report and exit
"""
from __future__ import annotations
import asyncio
import copy
import os
import queue
import sys
import threading
import traceback
from pathlib import Path

ROOT=Path(__file__).resolve().parent
if sys.stderr is None:  # pythonw: keep tracebacks for diagnostics
    (ROOT/'state').mkdir(exist_ok=True)
    sys.stderr=open(ROOT/'state'/'ui.log','a',encoding='utf-8',buffering=1);sys.stdout=sys.stderr

import flet as ft
from settings import Settings
from service import Service
from chatgpt_finder import ChatGPTFinder
import manual_blur
import single_instance
from ui.common import Dialogs,chip
from ui.dashboard import Dashboard
from ui.youtube_view import YouTubeView
from ui.shorts_view import ShortsView
from ui.channels_view import ChannelsView
from shorts_autopilot import ShortsAutopilot
from ui.media_views import PreviewView,BlurView
from ui.settings_view import SettingsView,NoticesView

DEMO=os.environ.get('NAREZKO_UI_DEMO')=='1'
SELFTEST=os.environ.get('NAREZKO_UI_SELFTEST')
NAV=[('home','Главная',ft.Icons.DASHBOARD_OUTLINED,ft.Icons.DASHBOARD),
     ('youtube','YouTube',ft.Icons.SMART_DISPLAY_OUTLINED,ft.Icons.SMART_DISPLAY),
     ('shorts','Shorts',ft.Icons.VIDEO_LIBRARY_OUTLINED,ft.Icons.VIDEO_LIBRARY),
     ('channels','Каналы',ft.Icons.SUBSCRIPTIONS_OUTLINED,ft.Icons.SUBSCRIPTIONS),
     ('preview','Превью',ft.Icons.IMAGE_OUTLINED,ft.Icons.IMAGE),
     ('blur','Блюр',ft.Icons.BLUR_CIRCULAR,ft.Icons.BLUR_ON),
     ('notices','Уведомления',ft.Icons.NOTIFICATIONS_OUTLINED,ft.Icons.NOTIFICATIONS),
     ('settings','Настройки',ft.Icons.SETTINGS_OUTLINED,ft.Icons.SETTINGS)]


class Controller:
    def __init__(self,page):
        self.page=page;self.root=ROOT;self.closing=False;self.errors=[]
        self.events=queue.SimpleQueue();self._refresh=True

    # ---------- startup ----------
    async def start(self):
        page=self.page
        page.title='Нарезкобот';page.theme_mode=ft.ThemeMode.DARK;page.padding=0;page.bgcolor='#0f1114'
        theme=ft.Theme(color_scheme_seed=ft.Colors.DEEP_PURPLE)
        page.theme=theme;page.dark_theme=theme
        page.window.width=1320;page.window.height=880;page.window.min_width=980;page.window.min_height=640
        self.dialogs=Dialogs(page)
        self.file_picker=ft.FilePicker();self.clipboard=ft.Clipboard()
        page.services.extend([self.file_picker,self.clipboard])
        self.settings=Settings(ROOT/'config.json')
        try:self.config=self.settings.load()
        except Exception as exc:
            page.add(ft.Text('Не удалось прочитать настройки: '+str(exc),color='#ff8a8a'));page.update();return
        self.finder=ChatGPTFinder(ROOT);self.service=Service(ROOT,self.settings,self.finder)
        self.shorts=ShortsAutopilot(ROOT,self.service)
        if not DEMO:self.service.recover_interrupted()
        self.views={'home':Dashboard(self),'youtube':YouTubeView(self),'shorts':ShortsView(self),'channels':ChannelsView(self),'preview':PreviewView(self),
                    'blur':BlurView(self),'notices':NoticesView(self),'settings':SettingsView(self)}
        self.rail=ft.NavigationRail(selected_index=0,label_type=ft.NavigationRailLabelType.ALL,min_width=96,bgcolor='#121418',
            group_alignment=-0.9,on_change=self._nav,
            leading=ft.Container(ft.Column([ft.Icon(ft.Icons.CONTENT_CUT,size=28,color=ft.Colors.PRIMARY),
                                            ft.Text('Нарезко-\nбот',size=12,weight=ft.FontWeight.BOLD,text_align=ft.TextAlign.CENTER)],
                                           horizontal_alignment=ft.CrossAxisAlignment.CENTER,spacing=4,tight=True),padding=ft.Padding.only(top=18,bottom=12)),
            trailing=chip('демо','warn') if DEMO else None,
            destinations=[ft.NavigationRailDestination(icon=i,selected_icon=s,label=l) for _,l,i,s in NAV])
        self.body=ft.Container(self.views['home'].view,expand=True,padding=ft.Padding.only(left=28,right=28,top=24,bottom=12))
        page.add(ft.Row([self.rail,ft.VerticalDivider(width=1,color='#22252b'),self.body],expand=True,spacing=0))
        page.window.prevent_close=True;page.window.on_event=self._window_event
        if not DEMO:
            self._import_profiles_once()
            if self.config['monitor']['enabled']:self.service.start()
            self.shorts.start()  # Shorts autopilot runs independently of Twitch monitoring
        page.update()
        page.run_task(self._loop)
        if SELFTEST:page.run_task(self._selftest)

    def _import_profiles_once(self):
        # Migrate blur profiles into the watch list once; later removals stay removed.
        if self.config['monitor'].get('profiles_imported'):return
        def mutate(c):
            existing={x.lower() for x in c['streamers']}
            for name in manual_blur.profile_logins(ROOT):
                if name not in existing:c['streamers'].append(name);existing.add(name)
            c['monitor']['profiles_imported']=True
        self.save_config(mutate)

    # ---------- helpers used by views ----------
    def spawn(self,fn,*args):
        async def runner():
            try:await fn(*args)
            except Exception as exc:
                self._record(exc)
                try:await self.dialogs.error('Ошибка',f'{type(exc).__name__}: {exc}')
                except Exception:pass
        self.page.run_task(runner)

    def post(self,kind,payload=None):
        self.events.put((kind,payload))

    def refresh_soon(self):
        self._refresh=True

    def navigate(self,key):
        keys=[k for k,*_ in NAV]
        self.rail.selected_index=keys.index(key);self.body.content=self.views[key].view;self.refresh_soon();self.page.update()

    def _nav(self,e):
        self.navigate(NAV[self.rail.selected_index or 0][0])

    def open_folder(self,name):
        folder=ROOT/name;folder.mkdir(exist_ok=True);self.open_path(str(folder))

    def open_path(self,path):
        if path and os.path.exists(path) and hasattr(os,'startfile'):os.startfile(path)
        else:self.dialogs.toast('Путь не найден: '+str(path),'warn')

    def save_config(self,mutator):
        try:
            c=copy.deepcopy(self.config);mutator(c)
            self.config=self.settings.save(c);self.service.settings_changed()
        except Exception as exc:
            self.spawn(self.dialogs.error,'Ошибка настроек',str(exc) or type(exc).__name__);return False
        self.views['settings'].load();self.refresh_soon();return True

    def add_streamer(self,name):
        if name.lower() in {x.lower() for x in self.config['streamers']}:return False
        if not self.save_config(lambda c:c['streamers'].append(name)):return False
        self.views['settings'].add_streamer(name);return True

    async def ensure_settings_saved(self):
        sv=self.views['settings']
        if not sv.dirty:return True
        if not await self.dialogs.confirm('Настройки','В настройках есть несохранённые изменения. Сохранить их сейчас?','Сохранить'):return False
        return await sv.save_async()

    async def start_monitoring(self):
        if not await self.ensure_settings_saved():return
        c=self.config
        if not c['twitch']['client_id'].strip() or not c['twitch']['client_secret'].strip() or not c['streamers']:
            await self.dialogs.error('Нужны данные','Для мониторинга укажите Twitch Client ID, Client Secret и хотя бы одного стримера.')
            self.navigate('settings');return
        if not self.save_config(lambda c:c['monitor'].update(enabled=True)):return
        if DEMO:self.dialogs.toast('Демо-режим: сервис не запускается','warn');return
        if self.service.start():self.dialogs.toast('Мониторинг запущен','ok')
        else:await self.dialogs.info('Мониторинг','Дождитесь завершения текущей операции перед повторным запуском.')

    async def stop_monitoring(self):
        if self.save_config(lambda c:c['monitor'].update(enabled=False)):
            if not DEMO:self.service.request_stop()
            self.dialogs.toast('Остановка запрошена — текущая операция завершится')

    # ---------- loop ----------
    def _record(self,exc):
        text=''.join(traceback.format_exception(exc));self.errors.append(text)
        print(text,file=sys.stderr)

    def _snapshot(self):
        s=self.service;q=s.youtube_queue
        return {'streams':s.stream_rows(self.config),'jobs':s.store.list_jobs(200),'uploads':q.list_uploads(),'profiles':q.list_profiles(),
                'logs':s.logs(),'notifications':s.store.notifications(200),
                'yt_waiting':q.waiting_reason(bool(self.config.get('youtube',{}).get('enabled',False))),
                'shorts':self.shorts.snapshot()}

    async def _loop(self):
        loop=asyncio.get_running_loop();last=0.0
        while not self.closing:
            while True:
                try:kind,payload=self.events.get_nowait()
                except queue.Empty:break
                for v in self.views.values():
                    if hasattr(v,'on_event'):self.spawn(v.on_event,kind,payload)
            if self._refresh or loop.time()-last>=3:
                self._refresh=False;last=loop.time()
                try:snap=await asyncio.to_thread(self._snapshot)
                except Exception as exc:self._record(exc);snap=None
                if snap:
                    for v in self.views.values():
                        try:v.refresh(snap)
                        except Exception as exc:self._record(exc)
            try:self.page.update()
            except Exception as exc:self._record(exc)
            await asyncio.sleep(0.5)

    # ---------- closing ----------
    async def _window_event(self,e):
        if getattr(e,'type',None)==ft.WindowEventType.CLOSE:await self.request_close()

    async def request_close(self):
        pv=self.views['preview']
        if pv.running:
            if not await self.dialogs.confirm('Тест превью','Отменить генерацию превью и выйти?','Выйти'):return
            pv._cancel()
            for _ in range(240):
                if not pv.running:break
                await asyncio.sleep(0.5)
        active=any(j['state']=='running' for j in self.service.store.list_jobs(1000)) or \
               any(u['state']=='uploading' and u.get('tracking',1) for u in self.service.youtube_queue.list_uploads()) or \
               self.shorts.current is not None
        if active and not DEMO and not await self.dialogs.confirm('Выход','Обработка ещё идёт. Выход прервёт её. Выйти?','Выйти'):return
        self.closing=True
        if not DEMO:
            self.service.request_stop()
            self.shorts.stop()
            if not active:threading.Thread(target=self.finder.close,daemon=True).start()
        await self.page.window.destroy()

    def _shot(self,key):
        folder=os.environ.get('NAREZKO_UI_SHOTS')
        if not folder:return
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(os.path.join(folder,key+'.png'))
        except Exception as exc:self._record(exc)

    async def _selftest(self):
        report=[]
        await asyncio.sleep(5)
        for key,*_ in NAV:
            try:self.navigate(key);await asyncio.sleep(1.5);report.append('OK view '+key);self._shot(key)
            except Exception as exc:self._record(exc);report.append('FAIL view '+key)
        try:
            self.navigate('youtube');self.views['youtube'].open_connect();await asyncio.sleep(1.5);report.append('OK connect wizard')
            self.views['youtube']._hide_connect();self.navigate('home');await asyncio.sleep(1)
        except Exception as exc:self._record(exc);report.append('FAIL connect wizard')
        report.append('errors: '+str(len(self.errors)))
        Path(SELFTEST).write_text('\n'.join(report+self.errors),encoding='utf-8')
        self.closing=True;await self.page.window.destroy()


async def _main(page):
    await Controller(page).start()


def main():
    lock=None
    if not DEMO:
        try:lock=single_instance.acquire(ROOT)
        except (single_instance.AlreadyRunning,OSError) as exc:
            message=str(exc)
            async def warn(page):
                page.title='Нарезкобот';page.window.width=520;page.window.height=200;page.theme_mode=ft.ThemeMode.DARK
                page.add(ft.Text('Нарезкобот уже запущен',size=18,weight=ft.FontWeight.BOLD),ft.Text(message))
            ft.run(warn);return
    try:ft.run(_main)
    finally:
        if lock is not None:single_instance.release(lock)


if __name__=='__main__':
    main()
