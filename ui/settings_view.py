"""Settings: Twitch, proxy, processing, speech, ChatGPT accounts, autostart; and notifications."""
from __future__ import annotations
import copy
import threading
import flet as ft
from settings import import_legacy_groq_key
import windows_autostart
from .common import card,chip,muted,note,page_header,empty,ts,MUTED
from .cleanup_card import CleanupCard


class SettingsView:
    def __init__(self,app):
        self.app=app;self.dirty=False;self.streamers=[];self.modes={}
        T=lambda label,**kw:ft.TextField(label=label,on_change=self._changed,**kw)
        S=lambda label:ft.Switch(label=label,on_change=self._changed)
        self.f={
            'client_id':T('Twitch Client ID',expand=True),'client_secret':T('Twitch Client Secret',password=True,can_reveal_password=True,expand=True),
            'interval':T('Интервал проверки, секунды',width=240,keyboard_type=ft.KeyboardType.NUMBER),
            'proxy_enabled':S('Использовать прокси'),'proxy_url':T('Адрес прокси (http:// или socks5://)',password=True,can_reveal_password=True,expand=True),
            'proxy_twitch':S('Для Twitch API'),'proxy_chatgpt':S('Для ChatGPT'),'proxy_groq':S('Для Groq'),
            'quality':T('Качество Twitch',width=200),'threads':T('Потоки скачивания',width=200),
            'min_reaction':T('Минимальная длина момента, мин',width=260),'bridge':T('Допустимое отвлечение, мин',width=260),
            'keep_original':S('Сохранять исходные фрагменты'),'remove_donations':S('Вырезать ответы на донаты (ChatGPT)'),
            'silence':S('Удалять тишину'),'min_cut':T('Минимальная тишина, сек',width=220),'max_gap':T('Допустимая пауза, сек',width=220),
            'onset':T('Порог над шумом, дБ',width=220),'height':T('Максимальная высота видео',width=220),
            'groq_enabled':S('Использовать Groq; при ошибке — локальный Whisper'),
            'groq_api_key':T('Groq API key',password=True,can_reveal_password=True,expand=True),
        }
        f=self.f
        self.streamer_chips=ft.Column(spacing=6)
        self.new_streamer=ft.TextField(label='Добавить стримера (Twitch-ник)',width=280,on_submit=self._add_streamer)
        self.gpt_list=ft.Column(spacing=8);self.gpt_busy={};self._gpt_key=None
        self.save_bar=ft.Container(ft.Row([ft.Icon(ft.Icons.EDIT_NOTE,color='#f3c95b'),ft.Text('Есть несохранённые изменения',color='#f3c95b',expand=True),
                                           ft.TextButton('Отменить',on_click=lambda e:self.load(force=True)),
                                           ft.FilledButton('Сохранить',icon=ft.Icons.SAVE,on_click=lambda e:app.spawn(self.save_async))],spacing=10),
                                   bgcolor='#3a2f12',border_radius=12,padding=ft.Padding.symmetric(horizontal=14,vertical=8),visible=False)
        self.cleanup=CleanupCard(app)
        self.view=ft.Column([
            page_header('Настройки','Применяются к новым заданиям. Секреты хранятся локально в config.json'),
            self.save_bar,
            card(ft.Row([f['client_id'],f['client_secret']],spacing=12),f['interval'],
                 muted('Стримеры для мониторинга. Что искать (реакции / разговоры / гибрид) — в разделе YouTube, в карточке канала стримера.',size=12),self.streamer_chips,ft.Row([self.new_streamer,ft.IconButton(ft.Icons.ADD,tooltip='Добавить',on_click=self._add_streamer)]),
                 title='Twitch и мониторинг',icon=ft.Icons.LIVE_TV),
            card(f['proxy_enabled'],f['proxy_url'],ft.Row([f['proxy_twitch'],f['proxy_chatgpt'],f['proxy_groq']],spacing=20,wrap=True),
                 muted('Загрузка видео работает напрямую. После смены прокси перезапустите приложение.',size=12),
                 title='Прокси',icon=ft.Icons.VPN_LOCK),
            card(ft.Row([f['quality'],f['threads'],f['min_reaction'],f['bridge']],spacing=12,wrap=True),
                 ft.Row([f['keep_original'],f['remove_donations']],spacing=20,wrap=True),
                 title='Обработка',icon=ft.Icons.MOVIE_FILTER),
            card(f['silence'],ft.Row([f['min_cut'],f['max_gap'],f['onset'],f['height']],spacing=12,wrap=True),
                 title='Удаление тишины',icon=ft.Icons.GRAPHIC_EQ),
            card(f['groq_enabled'],ft.Row([f['groq_api_key'],ft.OutlinedButton('Импорт из старого config.json',icon=ft.Icons.FILE_OPEN,on_click=lambda e:app.spawn(self._import_groq))],spacing=12),
                 note('Аудио отправляется в Groq — возможны лимиты и плата. Без ключа или при ошибке используется локальный Whisper. Ключ хранится в config.json открытым текстом.','warn',ft.Icons.WARNING_AMBER),
                 title='Распознавание речи',icon=ft.Icons.MIC),
            card(self.gpt_list,
                 muted('«Проверить» откроет окно браузера этого аккаунта на chatgpt.com — можно посмотреть, всё ли в порядке. '
                       '«Войти» — для входа или повторного входа. Бот пробует аккаунты по очереди и переходит к следующему при лимите загрузок. '
                       'Если бот сейчас отправляет запрос в ChatGPT, окно откроется после него.',size=12),
                 title='Аккаунты ChatGPT',icon=ft.Icons.SMART_TOY,
                 actions=[ft.FilledButton('Добавить аккаунт',icon=ft.Icons.PERSON_ADD,on_click=lambda e:app.spawn(self._gpt_add))]),
            self.cleanup.view,
            card(ft.Row([ft.OutlinedButton('Включить автозапуск',icon=ft.Icons.POWER_SETTINGS_NEW,on_click=lambda e:app.spawn(self._autostart,True)),
                         ft.TextButton('Отключить',on_click=lambda e:app.spawn(self._autostart,False))],spacing=10),
                 muted('После входа в Windows приложение откроется; мониторинг возобновится, если был включён. ПК не должен уходить в сон.',size=12),
                 title='Автозапуск Windows',icon=ft.Icons.ROCKET_LAUNCH),
        ],spacing=16,scroll=ft.ScrollMode.AUTO,expand=True)
        self.load(force=True)

    def _changed(self,_e=None):
        if not self.dirty:self.dirty=True;self.save_bar.visible=True;self.app.page.update()

    def _render_streamers(self):
        rows=[]
        modes=self.app.config.get('streamer_modes') or {}
        for n in self.streamers:
            m=modes.get(n) or {}
            r=bool(m.get('reaction',True));d=bool(m.get('discussion',True))
            kind='гибрид' if r and d else ('только реакции' if r else 'только разговоры')
            rows.append(ft.Container(ft.Row([ft.Icon(ft.Icons.PERSON,size=18,color=MUTED),ft.Text(n,size=14,weight=ft.FontWeight.W_600,width=160),
                muted(kind,size=12,expand=True),
                ft.IconButton(ft.Icons.CLOSE,icon_size=16,tooltip='Убрать из мониторинга',on_click=lambda e,n=n:self._remove_streamer(n))],
                spacing=12,vertical_alignment=ft.CrossAxisAlignment.CENTER),bgcolor='#1d2026',border_radius=10,padding=ft.Padding.symmetric(horizontal=12,vertical=2)))
        self.streamer_chips.controls=rows or [muted('Список пуст',size=12)]

    def _add_streamer(self,_e=None):
        name=(self.new_streamer.value or '').strip().lower()
        if name and name not in self.streamers:self.streamers.append(name);self._render_streamers();self._changed()
        self.new_streamer.value='';self.app.page.update()

    def _remove_streamer(self,name):
        if name in self.streamers:self.streamers.remove(name);self._render_streamers();self._changed();self.app.page.update()

    def load(self,force=False):
        if self.dirty and not force:return
        c=self.app.config;p=c['pipeline'];s=p['silence'];f=self.f
        values={'client_id':c['twitch']['client_id'],'client_secret':c['twitch']['client_secret'],'interval':c['monitor']['interval'],
                'proxy_enabled':c['proxy']['enabled'],'proxy_url':c['proxy']['url'],'proxy_twitch':c['proxy']['twitch_api'],'proxy_chatgpt':c['proxy']['chatgpt'],'proxy_groq':c['proxy']['groq'],
                'groq_enabled':p['speech']['groq_enabled'],'groq_api_key':p['speech']['groq_api_key'],
                'quality':p['quality'],'threads':p['download_threads'],'min_reaction':p['min_reaction_minutes'],'bridge':p['bridge_minutes'],
                'silence':s['enabled'],'min_cut':s['min_cut'],'max_gap':s['max_gap'],'onset':s['onset_db'],'height':s['max_height'],
                'keep_original':p['keep_original'],'remove_donations':p['remove_donations']}
        for k,v in values.items():
            f[k].value=bool(v) if isinstance(f[k],ft.Switch) else ('' if v is None else str(v))
        self.streamers=list(c['streamers'])
        self._render_streamers()
        self.dirty=False;self.save_bar.visible=False
        try:self.app.page.update()
        except Exception:pass

    def add_streamer(self,name):
        if name not in self.streamers:self.streamers.append(name);self._render_streamers()

    def save(self):
        """Returns error text or None."""
        f=self.f;v=lambda k:f[k].value;t=lambda k:(f[k].value or '').strip()
        try:
            c=copy.deepcopy(self.app.config)
            c['twitch']={'client_id':t('client_id'),'client_secret':t('client_secret')}
            c['streamers']=list(self.streamers)
            # modes are edited in the YouTube screen; only drop entries of removed streamers
            c['streamer_modes']={k:v for k,v in (c.get('streamer_modes') or {}).items() if k in self.streamers}
            c['monitor'].update(interval=t('interval'))
            c['proxy']={'enabled':bool(v('proxy_enabled')),'url':t('proxy_url'),'twitch_api':bool(v('proxy_twitch')),'chatgpt':bool(v('proxy_chatgpt')),'groq':bool(v('proxy_groq'))}
            p=c['pipeline'];p.update(quality=t('quality'),download_threads=t('threads'),min_reaction_minutes=t('min_reaction'),bridge_minutes=t('bridge'),
                                     keep_original=bool(v('keep_original')),remove_donations=bool(v('remove_donations')))
            p['speech'].update(groq_enabled=bool(v('groq_enabled')),groq_api_key=t('groq_api_key'))
            p['silence'].update(enabled=bool(v('silence')),min_cut=t('min_cut'),max_gap=t('max_gap'),onset_db=t('onset'),max_height=t('height'))
            self.app.config=self.app.settings.save(c);self.app.service.settings_changed()
        except Exception as exc:return str(exc) or type(exc).__name__
        self.load(force=True);return None

    async def save_async(self):
        err=self.save()
        if err:await self.app.dialogs.error('Ошибка настроек',err);return False
        self.app.dialogs.toast('Настройки сохранены','ok');self.app.refresh_soon();return True

    async def _import_groq(self):
        files=await self.app.file_picker.pick_files(dialog_title='Выберите старый config.json',allowed_extensions=['json'])
        if not files or not files[0].path:return
        try:
            self.f['groq_api_key'].value=import_legacy_groq_key(files[0].path);self.f['groq_enabled'].value=True;self._changed()
            self.app.dialogs.toast('Ключ загружен в поле. Нажмите «Сохранить».','ok')
        except ValueError as exc:await self.app.dialogs.error('Groq',str(exc))

    # ---------- ChatGPT accounts ----------
    def _gpt_row(self,a):
        name=a['name'];busy=self.gpt_busy.get(name)
        if busy:status=chip('проверяю…' if busy=='check' else 'жду входа в окне браузера…','info',ft.Icons.HOURGLASS_TOP)
        elif a['session'] is True:status=chip('вход в порядке','ok',ft.Icons.CHECK_CIRCLE)
        elif a['session'] is False:status=chip('нужен вход','error',ft.Icons.ERROR_OUTLINE)
        elif a['has_cookies']:status=chip('не проверялся','muted')
        else:status=chip('не настроен — войдите','warn')
        meta=[]
        if a['checked_at']:meta.append('проверен '+ts(a['checked_at']))
        if a['last_ok']:meta.append('последний удачный запрос '+ts(a['last_ok']))
        if a['current']:meta.append('открыт сейчас')
        any_busy=bool(self.gpt_busy)
        return ft.Container(ft.Row([
            ft.Icon(ft.Icons.ACCOUNT_CIRCLE,color=MUTED),
            ft.Column([ft.Text(name,size=14,weight=ft.FontWeight.W_600),muted(' · '.join(meta) or 'нет данных',size=12)],spacing=2,tight=True,expand=True),
            status,
            ft.OutlinedButton('Проверить',icon=ft.Icons.OPEN_IN_BROWSER,disabled=any_busy,on_click=lambda e,n=name:self.app.spawn(self._gpt_run,n,'check')),
            ft.TextButton('Войти',icon=ft.Icons.LOGIN,disabled=any_busy,on_click=lambda e,n=name:self.app.spawn(self._gpt_run,n,'login'))],
            spacing=12,vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor='#1d2026',border_radius=10,padding=ft.Padding.symmetric(horizontal=12,vertical=6))

    def _gpt_refresh(self,force=False):
        try:info=self.app.finder.accounts_info()
        except Exception:info=[]
        key=(tuple(tuple(sorted(a.items())) for a in info),tuple(sorted(self.gpt_busy.items())))
        if key==self._gpt_key and not force:return
        self._gpt_key=key
        self.gpt_list.controls=[self._gpt_row(a) for a in info] or [empty('Аккаунтов нет — нажмите «Добавить аккаунт».')]

    async def _gpt_run(self,name,kind):
        app=self.app
        if self.gpt_busy:app.dialogs.toast('Дождитесь окончания предыдущей проверки.','warn');return
        if not await app.ensure_settings_saved():return
        self.gpt_busy[name]=kind;self._gpt_refresh(True);app.page.update()
        app.dialogs.toast(f'Открываю браузер {name}…' if kind=='check' else f'Откроется окно браузера — войдите в ChatGPT ({name}). На вход 5 минут.')
        def work():
            try:
                ok=app.finder.check(name) if kind=='check' else app.finder.login(account=name)
                app.post('gpt_done',(name,kind,ok,None))
            except Exception as exc:app.post('gpt_done',(name,kind,False,f'{type(exc).__name__}: {exc}'))
        threading.Thread(target=work,daemon=True).start()

    async def _gpt_add(self):
        app=self.app
        if self.gpt_busy:app.dialogs.toast('Дождитесь окончания предыдущей проверки.','warn');return
        if not await app.dialogs.confirm('Новый аккаунт ChatGPT','Создать новый профиль и открыть окно входа? Войдите в нём в другой аккаунт ChatGPT — '
                                         'бот будет использовать его, когда у других аккаунтов кончится лимит.','Создать и войти'):return
        try:name=app.finder.create_account()
        except Exception as exc:await app.dialogs.error('ChatGPT',str(exc));return
        app.service.log(f'ChatGPT: создан профиль {name}')
        await self._gpt_run(name,'login')

    async def on_event(self,kind,payload):
        if kind!='gpt_done':return
        name,what,ok,err=payload;self.gpt_busy.pop(name,None);self._gpt_refresh(True)
        app=self.app
        if err:
            app.service.log(f'ChatGPT: {name} — ошибка браузера ({err[:120]})');await app.dialogs.error('ChatGPT',f'{name}: {err}');return
        if what=='login':
            app.service.log(f'ChatGPT: {name} — вход выполнен' if ok else f'ChatGPT: {name} — время входа истекло')
            app.dialogs.toast(f'{name}: вход выполнен' if ok else f'{name}: вход не выполнен — время истекло','ok' if ok else 'warn')
        else:
            app.service.log(f'ChatGPT: {name} — '+('вход в порядке' if ok else 'не авторизован'))
            if ok:app.dialogs.toast(f'{name}: вход в порядке. Окно браузера оставлено открытым — можно посмотреть.','ok')
            else:await app.dialogs.info('ChatGPT',f'{name}: аккаунт не авторизован (или страница не загрузилась за минуту). Нажмите «Войти» и войдите в окне браузера.')

    async def _autostart(self,enabled):
        app=self.app
        if not await app.dialogs.confirm('Автозапуск',('Включить' if enabled else 'Отключить')+' запуск Нарезкобота после входа в текущую учётную запись Windows?'):return
        try:
            windows_autostart.set_autostart(app.root,enabled)
            await app.dialogs.info('Автозапуск','Включён. После входа в Windows приложение откроется.' if enabled else 'Автозапуск отключён.')
        except Exception as exc:await app.dialogs.error('Автозапуск',str(exc))

    def refresh(self,snap):
        self._gpt_refresh()


class NoticesView:
    def __init__(self,app):
        self.app=app;self._key=None
        self.list=ft.Column(spacing=6)
        self.view=ft.Column([
            page_header('Уведомления','События стримов и обработки',
                        actions=[ft.OutlinedButton('Очистить',icon=ft.Icons.CLEAR_ALL,on_click=lambda e:app.spawn(self._clear))]),
            card(self.list,padding=14),
        ],spacing=16,scroll=ft.ScrollMode.AUTO,expand=True)

    def refresh(self,snap):
        rows=snap['notifications']
        key=tuple((r['timestamp'],r['message']) for r in rows)
        if key==self._key:return
        self._key=key
        self.list.controls=[ft.Row([muted(ts(r['timestamp']),size=12,width=90),ft.Text(r['message'],size=13,expand=True,selectable=True)],spacing=12,
                                   vertical_alignment=ft.CrossAxisAlignment.START) for r in rows] or [empty('Уведомлений нет',ft.Icons.NOTIFICATIONS_NONE)]

    async def _clear(self):
        if not await self.app.dialogs.confirm('Уведомления','Очистить список уведомлений? Очередь, профили и результаты сохранятся.','Очистить'):return
        try:self.app.service.store.clear_notifications();self.app.refresh_soon()
        except Exception as exc:await self.app.dialogs.error('Уведомления','Не удалось очистить: '+type(exc).__name__)
