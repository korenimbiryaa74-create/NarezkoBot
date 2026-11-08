"""Windows desktop control panel. All background work is owned by this process."""
from __future__ import annotations
import copy
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from settings import Settings, import_legacy_groq_key
from service import Service
from chatgpt_finder import ChatGPTFinder
import banner_blur
import stream_cutter
import manual_blur
import windows_autostart
import single_instance

ROOT=Path(__file__).resolve().parent

class App:
    def __init__(self,window):
        self.window=window; self.window.title('Нарезкобот — разработка'); self.window.geometry('1100x760'); self.window.minsize(850,600)
        self.settings=Settings(ROOT/'config.json')
        try:self.config=self.settings.load()
        except Exception as exc:
            messagebox.showerror('Настройки',str(exc)); raise
        self.finder=ChatGPTFinder(ROOT); self.service=Service(ROOT,self.settings,self.finder)
        self.service.recover_interrupted()
        self.variables={}; self._last_job=None
        self._build()
        self._load_controls()
        self._import_profiles_once()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        if self.config['monitor']['enabled']:
            self.service.start()
        self.refresh()

    def var(self,key,default='',kind='str'):
        v=tk.BooleanVar(value=default) if kind=='bool' else tk.StringVar(value=str(default))
        self.variables[key]=v; return v
    def _row(self,parent,row,label,key,secret=False,width=36):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky='w',padx=8,pady=5)
        box=ttk.Entry(parent,textvariable=self.var(key),show='•' if secret else '',width=width)
        box.grid(row=row,column=1,sticky='ew',padx=8,pady=5); return box
    def _toggle(self,parent,row,label,key):
        ttk.Checkbutton(parent,text=label,variable=self.var(key,False,'bool')).grid(row=row,column=0,columnspan=2,sticky='w',padx=8,pady=5)
    def _build(self):
        outer=ttk.Frame(self.window,padding=12); outer.pack(fill='both',expand=True)
        header=ttk.Frame(outer); header.pack(fill='x')
        ttk.Label(header,text='Нарезкобот',font=('Segoe UI',18,'bold')).pack(side='left')
        ttk.Label(header,text='Экспериментальная сборка — не для работы без контроля').pack(side='left',padx=16)
        tabs=ttk.Notebook(outer); tabs.pack(fill='both',expand=True,pady=10)
        dashboard=ttk.Frame(tabs,padding=10); settings=ttk.Frame(tabs,padding=10)
        notices=ttk.Frame(tabs,padding=10)
        tabs.add(dashboard,text='Состояние'); tabs.add(notices,text='Уведомления'); tabs.add(settings,text='Настройки')
        noticebar=ttk.Frame(notices);noticebar.pack(fill='x')
        ttk.Label(noticebar,text='События стримов и обработки').pack(side='left')
        ttk.Button(noticebar,text='Очистить уведомления',command=self.clear_notifications).pack(side='right')
        noticeframe=ttk.Frame(notices);noticeframe.pack(fill='both',expand=True,pady=8)
        self.notifications=tk.Text(noticeframe,state='disabled',wrap='word')
        scroll=ttk.Scrollbar(noticeframe,orient='vertical',command=self.notifications.yview)
        self.notifications.configure(yscrollcommand=scroll.set);self.notifications.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        bar=ttk.Frame(dashboard);bar.pack(fill='x')
        ttk.Button(bar,text='Сохранить и запустить',command=self.start).pack(side='left')
        ttk.Button(bar,text='Остановить после текущей операции',command=self.stop).pack(side='left',padx=5)
        ttk.Button(bar,text='Войти в ChatGPT',command=self.login).pack(side='left',padx=5)
        self.gpt_account=tk.StringVar(value='acc_01')
        ttk.Combobox(bar,textvariable=self.gpt_account,values=[f'acc_{n:02d}' for n in range(1,8)],state='readonly',width=7).pack(side='left')
        ttk.Button(bar,text='Открыть папку результатов',command=self.open_outputs).pack(side='left',padx=5)
        testbar=ttk.Frame(dashboard); testbar.pack(fill='x',pady=(6,0))
        ttk.Button(testbar,text='Открыть папку тестовых видео',command=self.open_test_input).pack(side='left')
        ttk.Button(testbar,text='Запустить тест из папки',command=self.start_test).pack(side='left',padx=5)
        ttk.Button(testbar,text='Продолжить очередь',command=self.resume_queue).pack(side='left',padx=5)
        self.status=tk.StringVar(value='Остановлено')
        ttk.Label(dashboard,textvariable=self.status).pack(anchor='w',pady=(9,4))
        ttk.Label(dashboard,text='Стримеры и ожидаемые записи (сохранённый список)').pack(anchor='w')
        streamframe=ttk.Frame(dashboard);streamframe.pack(fill='x',pady=4)
        self.streams=ttk.Treeview(streamframe,columns=('login','id','state','details'),show='headings',height=5)
        for name,width,text in [('login',140,'Стример'),('id',130,'ID эфира'),('state',150,'Состояние'),('details',350,'Ошибка / VOD')]:
            self.streams.heading(name,text=text); self.streams.column(name,width=width,stretch=True)
        streamscroll=ttk.Scrollbar(streamframe,orient='vertical',command=self.streams.yview)
        self.streams.configure(yscrollcommand=streamscroll.set)
        self.streams.pack(side='left',fill='x',expand=True);streamscroll.pack(side='right',fill='y')
        ttk.Label(dashboard,text='Очередь и история обработки').pack(anchor='w',pady=(7,0))
        self.jobs=ttk.Treeview(dashboard,columns=('id','streamer','vod','state','stage','detail'),show='headings',height=9)
        for name,width,text in [('id',50,'№'),('streamer',100,'Стример'),('vod',100,'VOD'),('state',90,'Статус'),('stage',130,'Этап'),('detail',400,'Подробности')]:
            self.jobs.heading(name,text=text); self.jobs.column(name,width=width,stretch=True)
        self.jobs.pack(fill='both',expand=True,pady=4)
        jobbar=ttk.Frame(dashboard); jobbar.pack(fill='x')
        ttk.Button(jobbar,text='Повторить выбранное задание с ошибкой',command=self.retry).pack(side='left')
        ttk.Button(jobbar,text='Скопировать ошибку выбранного задания',command=self.copy_job_detail).pack(side='left',padx=5)
        recoverybar=ttk.Frame(dashboard);recoverybar.pack(fill='x',pady=(4,0))
        ttk.Button(recoverybar,text='Очистить историю',command=self.clear_history).pack(side='left',padx=(0,5))
        ttk.Button(recoverybar,text='Очистить очередь',command=self.clear_queue).pack(side='left',padx=(0,5))
        ttk.Button(recoverybar,text='Вставить ответ ChatGPT',command=self.import_chatgpt_answer).pack(side='left')
        ttk.Button(recoverybar,text='Собрать готовые файлы',command=self.organize_finished).pack(side='left',padx=5)
        eventbar=ttk.Frame(dashboard); eventbar.pack(fill='x',pady=(8,0))
        ttk.Label(eventbar,text='Последние события (выделение приостанавливает обновление текста)').pack(side='left')
        ttk.Button(eventbar,text='Скопировать все события',command=self.copy_events).pack(side='right')
        self.events=tk.Text(dashboard,height=5,state='disabled',wrap='word'); self.events.pack(fill='x')
        self.events.bind('<Control-c>',self.copy_event_selection)
        self.events.bind('<Control-C>',self.copy_event_selection)
        self.events.bind('<Control-a>',self.select_events)
        self.events.bind('<Control-A>',self.select_events)
        self.event_menu=tk.Menu(self.window,tearoff=False)
        self.event_menu.add_command(label='Копировать выделенное',command=self.copy_event_selection)
        self.event_menu.add_command(label='Выделить всё',command=self.select_events)
        self.event_menu.add_command(label='Копировать все события',command=self.copy_events)
        self.events.bind('<Button-3>',self.show_event_menu)
        # Settings are organised into independently scrollable tabs rather than an oversized form.
        sub=ttk.Notebook(settings);sub.pack(fill='both',expand=True)
        common=ttk.Frame(sub,padding=8); processing=ttk.Frame(sub,padding=8); speech=ttk.Frame(sub,padding=8)
        sub.add(common,text='Twitch и прокси');sub.add(processing,text='Обработка');sub.add(speech,text='Распознавание речи')
        for f in (common,processing,speech):f.columnconfigure(1,weight=1)
        self._row(common,0,'Twitch Client ID','client_id')
        self._row(common,1,'Twitch Client Secret','client_secret',secret=True)
        self._row(common,2,'Стримеры через запятую (добавляются из профилей блюра)','streamers')
        self._row(common,3,'Интервал проверки, секунды','interval')
        self._toggle(common,4,'Мониторинг включён','monitor_enabled')
        self._toggle(common,5,'Использовать прокси','proxy_enabled')
        self._row(common,6,'Адрес прокси (http:// или socks5://)','proxy_url')
        self._toggle(common,7,'Прокси для Twitch API','proxy_twitch')
        self._toggle(common,8,'Прокси для ChatGPT','proxy_chatgpt')
        ttk.Label(common,text='Загрузка видео работает напрямую. После смены прокси перезапустите приложение.').grid(row=9,column=0,columnspan=2,sticky='w',padx=8,pady=7)
        startup=ttk.Frame(common);startup.grid(row=10,column=0,columnspan=2,sticky='w',padx=8,pady=5)
        ttk.Button(startup,text='Включить автозапуск Windows',command=lambda:self.set_autostart(True)).pack(side='left')
        ttk.Button(startup,text='Отключить автозапуск',command=lambda:self.set_autostart(False)).pack(side='left',padx=5)
        ttk.Label(common,text='Автозапуск — после входа в Windows. ПК не должен переходить в сон.').grid(row=11,column=0,columnspan=2,sticky='w',padx=8)
        self._row(processing,0,'Качество Twitch','quality')
        self._row(processing,1,'Потоки скачивания','threads')
        self._row(processing,2,'Минимальная длина реакции, мин','min_reaction')
        self._row(processing,3,'Допустимое отвлечение, мин','bridge')
        self._toggle(processing,4,'Удалять тишину','silence')
        self._row(processing,5,'Минимальная тишина, сек','min_cut')
        self._row(processing,6,'Допустимая пауза, сек','max_gap')
        self._row(processing,7,'Порог над шумом, дБ','onset')
        self._row(processing,8,'Максимальная высота видео','height')
        self._toggle(processing,9,'Сохранять исходные фрагменты','keep_original')
        self._toggle(processing,10,'Вырезать ответы на донаты (ChatGPT)','remove_donations')
        self._toggle(processing,11,'Размывать области по профилю стримера','banner_blur')
        self._row(processing,12,'Ник стримера для тестового видео','banner_test_streamer')
        bbar=ttk.Frame(processing);bbar.grid(row=13,column=0,columnspan=2,sticky='w',padx=8,pady=(4,0))
        ttk.Button(bbar,text='Настроить блюр стримера…',command=self.configure_streamer_blur).pack(side='left')
        ttk.Button(bbar,text='Без блюра для стримера…',command=self.streamer_without_blur).pack(side='left',padx=5)
        ttk.Button(bbar,text='Настроить по локальному видео…',command=self.blur_from_local).pack(side='left')
        self.banner_info=tk.StringVar(value='Области действуют на протяжении всей нарезки. Для каждого стримера настройте профиль или укажите «Без блюра».')
        ttk.Label(processing,textvariable=self.banner_info,wraplength=900).grid(row=14,column=0,columnspan=2,sticky='w',padx=8)
        ttk.Label(processing,text='Речь: Groq с резервом Whisper. Поиск реакций: ChatGPT.\nПапка результатов создаётся в проекте автоматически.\nTwitchDownloaderCLI находится автоматически.').grid(row=15,column=0,columnspan=2,sticky='w',padx=8,pady=8)
        self._toggle(speech,0,'Использовать Groq; при ошибке — локальный Whisper','groq_enabled')
        self._row(speech,1,'Groq API key','groq_api_key',secret=True)
        ttk.Button(speech,text='Импортировать ключ из старого config.json',command=self.import_groq_key).grid(row=2,column=0,columnspan=2,sticky='w',padx=8,pady=5)
        self._toggle(speech,3,'Прокси для Groq (дополнительно включите общий прокси)','proxy_groq')
        ttk.Label(speech,text='Аудио отправляется в Groq. Возможны лимиты и плата.\nБез ключа или при ошибке используется локальный Whisper.\nКлюч хранится в config.json открытым текстом; не делитесь этим файлом.').grid(row=4,column=0,columnspan=2,sticky='w',padx=8,pady=8)
        ttk.Button(settings,text='Сохранить настройки',command=self.save).pack(anchor='w',pady=8)
        ttk.Label(settings,text='Настройки применяются к новым заданиям. Секреты хранятся локально в config.json.').pack(anchor='w')
    def _load_controls(self):
        c=self.config; p=c['pipeline']; s=p['silence']; v=self.variables
        values={'client_id':c['twitch']['client_id'],'client_secret':c['twitch']['client_secret'],
          'streamers':', '.join(c['streamers']),'interval':c['monitor']['interval'],'monitor_enabled':c['monitor']['enabled'],
          'proxy_enabled':c['proxy']['enabled'],'proxy_url':c['proxy']['url'],'proxy_twitch':c['proxy']['twitch_api'],'proxy_chatgpt':c['proxy']['chatgpt'],'proxy_groq':c['proxy']['groq'],
          'groq_enabled':p['speech']['groq_enabled'],'groq_api_key':p['speech']['groq_api_key'],
          'quality':p['quality'],'threads':p['download_threads'],'min_reaction':p['min_reaction_minutes'],'bridge':p['bridge_minutes'],
          'silence':s['enabled'],'min_cut':s['min_cut'],'max_gap':s['max_gap'],'onset':s['onset_db'],'height':s['max_height'],'keep_original':p['keep_original'],'remove_donations':p['remove_donations'],'banner_blur':p['banner_blur']['enabled'],'banner_test_streamer':p['banner_blur'].get('test_streamer','')}
        for key,value in values.items(): v[key].set(value)
    def save(self):
        try:
            v=self.variables; c=copy.deepcopy(self.config)
            c['twitch']={'client_id':v['client_id'].get().strip(),'client_secret':v['client_secret'].get().strip()}
            c['streamers']=[x.strip() for x in v['streamers'].get().split(',') if x.strip()]
            c['monitor'].update(enabled=v['monitor_enabled'].get(),interval=v['interval'].get())
            c['proxy']={'enabled':v['proxy_enabled'].get(),'url':v['proxy_url'].get().strip(),'twitch_api':v['proxy_twitch'].get(),'chatgpt':v['proxy_chatgpt'].get(),'groq':v['proxy_groq'].get()}
            p=c['pipeline'];p.update(quality=v['quality'].get().strip(),download_threads=v['threads'].get(),min_reaction_minutes=v['min_reaction'].get(),bridge_minutes=v['bridge'].get(),keep_original=v['keep_original'].get(),remove_donations=v['remove_donations'].get())
            p['banner_blur'].update(enabled=v['banner_blur'].get(),test_streamer=v['banner_test_streamer'].get().strip())
            p['speech'].update(groq_enabled=v['groq_enabled'].get(),groq_api_key=v['groq_api_key'].get().strip())
            p['silence'].update(enabled=v['silence'].get(),min_cut=v['min_cut'].get(),max_gap=v['max_gap'].get(),onset_db=v['onset'].get(),max_height=v['height'].get())
            self.config=self.settings.save(c);self.service.settings_changed();self.status.set('Настройки сохранены')
            return True
        except Exception as exc:
            messagebox.showerror('Ошибка настроек',str(exc));return False
    def import_groq_key(self):
        path=filedialog.askopenfilename(title='Выберите старый config.json',filetypes=[('JSON','*.json'),('Все файлы','*.*')])
        if not path: return
        try:
            self.variables['groq_api_key'].set(import_legacy_groq_key(path))
            self.variables['groq_enabled'].set(True)
            messagebox.showinfo('Groq','Ключ загружен в поле. Нажмите «Сохранить настройки».')
        except ValueError as exc: messagebox.showerror('Groq',str(exc))
    def start(self):
        v=self.variables
        if not v['client_id'].get().strip() or not v['client_secret'].get().strip() or not v['streamers'].get().strip():
            messagebox.showwarning('Нужны данные','Для мониторинга укажите Twitch Client ID, Client Secret и хотя бы одного стримера.');return
        self.variables['monitor_enabled'].set(True)
        if not self.save():return
        if self.service.start(): self.status.set('Мониторинг запущен')
        else: messagebox.showwarning('Остановка', 'Дождитесь завершения текущей операции перед повторным запуском.')
    def stop(self):
        self.variables['monitor_enabled'].set(False)
        if self.save(): self.service.request_stop();self.status.set('Остановка запрошена')
    def login(self):
        if not self.save():return
        account=self.gpt_account.get()
        self.status.set(f'Откройте окно браузера и войдите в ChatGPT ({account})')
        def action():
            try:
                ok=self.finder.login(account=account)
                self.service.log(f'ChatGPT: {account} — вход выполнен' if ok else f'ChatGPT: {account} — время входа истекло')
            except Exception as exc:self.service.log('ChatGPT: не удалось открыть браузер ('+type(exc).__name__+')')
        threading.Thread(target=action,daemon=True).start()
    def retry(self):
        rows=self.jobs.selection()
        if not rows:return
        if not messagebox.askyesno('Повтор','Проверьте, что предыдущий запрос ChatGPT не выполняется. Повторить задание?'):return
        try:self.service.store.retry(int(rows[0]))
        except ValueError as exc:messagebox.showerror('Повтор',str(exc))
    def clear_history(self):
        if not messagebox.askyesno('Очистить историю','Скрыть завершённые задания и задания с ошибкой? Ожидающие и текущие задания, кэш, результаты и мониторинг сохранятся.'):
            return
        try:
            count=self.service.store.clear_history()
            self.service.log(f'История очищена: скрыто заданий {count}')
            self.status.set(f'История очищена: {count}')
        except Exception as exc: messagebox.showerror('История','Не удалось очистить историю: '+type(exc).__name__)

    def clear_queue(self):
        if not messagebox.askyesno('Очистить очередь','Отменить ожидающие задания и задания, ожидающие повтора? Текущее задание продолжится. Мониторинг может добавить новые задания. Кэш и результаты сохранятся.'):
            return
        try:
            count=self.service.store.clear_queue()
            self.service.log(f'Очередь очищена: отменено заданий {count}')
            self.status.set(f'Очередь очищена: {count}')
        except Exception as exc: messagebox.showerror('Очередь','Не удалось очистить очередь: '+type(exc).__name__)

    def organize_finished(self):
        selected=self.jobs.selection()
        if not selected:
            messagebox.showinfo('Готовые файлы','Выберите завершённое задание в истории.');return
        if not messagebox.askyesno('Готовые файлы','Собрать видео и транскрипты выбранного завершённого задания в отдельную папку и создать отчёты о паузах? Повторная обработка не потребуется.'):
            return
        try:
            folder,count=self.service.organize_finished(int(selected[0]))
            messagebox.showinfo('Готовые файлы',f'Собрано видео: {count}\nПапка: {folder}')
        except Exception as exc: messagebox.showerror('Готовые файлы',str(exc))

    def import_chatgpt_answer(self):
        selected=self.jobs.selection()
        if not selected:
            messagebox.showinfo('Ответ ChatGPT','Выберите задание со статусом «Ошибка».');return
        try: text=self.window.clipboard_get()
        except tk.TclError:
            messagebox.showerror('Ответ ChatGPT','Буфер обмена пуст. Скопируйте полный ответ из нужного чата.');return
        if not messagebox.askyesno('Ответ ChatGPT','Это полный ответ о реакциях или донатах именно для выбранного задания? Он будет использован вместо соответствующего нового запроса к ChatGPT.'):
            return
        try:
            count=self.service.save_manual_reactions(int(selected[0]),text)
            messagebox.showinfo('Ответ ChatGPT',f'Ответ сохранён; интервалов: {count}. Нажмите «Повторить выбранное задание с ошибкой», затем «Продолжить очередь».')
        except (ValueError,OSError) as exc: messagebox.showerror('Ответ ChatGPT',str(exc))
    def _clipboard(self,text):
        self.window.clipboard_clear()
        self.window.clipboard_append(text)
        self.status.set('Скопировано. Проверьте текст на секреты перед отправкой.')
    def copy_job_detail(self):
        selected=self.jobs.selection()
        if not selected:
            messagebox.showinfo('Копирование','Выберите задание в очереди.'); return
        job=next((j for j in self.service.store.list_jobs(1000) if str(j['id'])==selected[0]),None)
        if not job:
            messagebox.showwarning('Копирование','Задание не найдено.'); return
        name=('Тест: '+job['local_name']) if job.get('local_name') else job['streamer']
        text=(f"Задание №{job['id']}: {name}\nСостояние: {job['state']}\n"
              f"Этап: {job['stage']}\nПодробности: {job['detail'] or 'нет'}")
        self._clipboard(text)
    def _events_text(self):
        return '\n'.join(__import__('datetime').datetime.fromtimestamp(t).strftime('%H:%M:%S')+'  '+m for t,m in self.service.logs())
    def copy_events(self):
        self._clipboard(self._events_text())
    def copy_event_selection(self,event=None):
        try: text=self.events.get('sel.first','sel.last')
        except tk.TclError: return 'break'
        self._clipboard(text); return 'break'
    def select_events(self,event=None):
        self.events.tag_add('sel','1.0','end-1c')
        self.events.focus_set(); return 'break'
    def show_event_menu(self,event):
        try: self.event_menu.tk_popup(event.x_root,event.y_root)
        finally: self.event_menu.grab_release()

    def open_test_input(self):
        folder=ROOT/'test_input'; folder.mkdir(exist_ok=True)
        if hasattr(os,'startfile'): os.startfile(str(folder))
    def resume_queue(self):
        if not self.save(): return
        if self.service.start(): self.status.set('Очередь запущена')
        else: messagebox.showwarning('Остановка','Дождитесь завершения текущей операции.')
    def start_test(self):
        if not self.save(): return
        try:
            added,active=self.service.queue_test_inputs()
            if not added and not active:
                messagebox.showinfo('Тест','Поместите видео в папку test_input и запустите тест снова.'); return
            if not self.service.start():
                messagebox.showwarning('Остановка','Задание в очереди; дождитесь остановки и нажмите «Продолжить очередь».'); return
            self.status.set(f'Тест: добавлено {added}, уже в очереди {active}')
        except Exception as exc: messagebox.showerror('Тест',str(exc))

    def _blur_login(self):
        from tkinter import simpledialog
        name=simpledialog.askstring('Блюр стримера','Введите никнейм Twitch:',parent=self.window)
        if name is None:return None
        try:return manual_blur.normalize_login(name)
        except ValueError:messagebox.showerror('Блюр','Введите корректный логин Twitch: латинские буквы, цифры или _.');return None
    def _import_profiles_once(self):
        # Migrate existing profiles once; later the user can remove a name from
        # the editable watch list without it silently reappearing on restart.
        if self.config['monitor'].get('profiles_imported'):return
        names=[x.strip() for x in self.variables['streamers'].get().split(',') if x.strip()]
        existing={x.lower() for x in names}
        for name in manual_blur.profile_logins(ROOT):
            if name not in existing:names.append(name);existing.add(name)
        self.variables['streamers'].set(', '.join(names))
        self.config['monitor']['profiles_imported']=True
        if not self.save():self.config['monitor']['profiles_imported']=False

    def _blur_saved(self,name):
        names=[x.strip() for x in self.variables['streamers'].get().split(',') if x.strip()]
        if name not in {x.lower() for x in names}:names.append(name)
        self.variables['streamers'].set(', '.join(names))
        if self.save():
            self.banner_info.set(f'Профиль {name} сохранён и добавлен в список мониторинга. Блюр действует на всей нарезке.')
        else:
            self.banner_info.set('Профиль сохранён. Исправьте настройки и нажмите «Сохранить настройки», чтобы добавить стримера в мониторинг.')
    def streamer_without_blur(self):
        name=self._blur_login()
        if not name:return
        if not messagebox.askyesno('Без блюра',f'Сохранить профиль {name} без блюра? Старые области этого профиля будут заменены.') :return
        try:manual_blur.save_profile(ROOT,name,[]);self._blur_saved(name)
        except Exception as exc:messagebox.showerror('Блюр',str(exc))
    def configure_streamer_blur(self):
        name=self._blur_login()
        if not name or not self.save():return
        proxy=self.config['proxy'];p=self.config['pipeline']
        cli=ROOT/'desktop'/'tools'/'TwitchDownloaderCLI'/'TwitchDownloaderCLI.exe'
        try:ffmpeg,_=stream_cutter.require_tools()
        except Exception as exc:messagebox.showerror('Блюр',str(exc));return
        state={};self.status.set(f'Получаю кадр последней записи {name}…')
        def work():
            try:state['data']=manual_blur.latest_frame(name,proxy['url'] if proxy['enabled'] and proxy['twitch_api'] else '',str(cli),ffmpeg,threads=p['download_threads'])
            except Exception as exc:state['error']=str(exc)
        def poll():
            if thread.is_alive():self.window.after(400,poll);return
            if 'error' in state:
                self.status.set('Не удалось получить кадр');messagebox.showerror('Блюр',state['error']);return
            image,vod,t=state['data'];self.status.set(f'Кадр VOD {vod}: {t} с')
            try:
                from manual_blur_picker import ManualBlurPicker
                ManualBlurPicker(self.window,ROOT,name,image,lambda:self._blur_saved(name))
            except Exception as exc:messagebox.showerror('Блюр',str(exc))
        thread=threading.Thread(target=work,daemon=True);thread.start();self.window.after(400,poll)
    def blur_from_local(self):
        from tkinter import simpledialog
        from manual_blur_picker import ManualBlurPicker
        name=self._blur_login()
        if not name:return
        path=filedialog.askopenfilename(title='Выберите локальное видео',filetypes=[('Видео','*.mp4 *.mkv *.ts *.flv *.mov *.webm *.avi'),('Все файлы','*.*')])
        if not path:return
        seconds=simpledialog.askfloat('Кадр','Время кадра в секундах:',initialvalue=0,minvalue=0,parent=self.window)
        if seconds is None:return
        try:
            ffmpeg,_=stream_cutter.require_tools();image=banner_blur.full_frame_at(path,seconds,ffmpeg)
            ManualBlurPicker(self.window,ROOT,name,image,lambda:self._blur_saved(name))
        except Exception as exc:messagebox.showerror('Блюр',str(exc))
    def set_autostart(self,enabled):
        action='Включить' if enabled else 'Отключить'
        if not messagebox.askyesno('Автозапуск',f'{action} запуск Нарезкобота после входа в текущую учётную запись Windows?'):return
        try:
            windows_autostart.set_autostart(ROOT,enabled)
            messagebox.showinfo('Автозапуск',('Включён. После входа в Windows приложение откроется; мониторинг возобновится, если он был включён.' if enabled else 'Автозапуск отключён.'))
        except Exception as exc:messagebox.showerror('Автозапуск',str(exc))

    def clear_notifications(self):
        if not messagebox.askyesno('Уведомления','Очистить список уведомлений? Очередь, профили и результаты сохранятся.'):return
        try:self.service.store.clear_notifications()
        except Exception as exc:messagebox.showerror('Уведомления','Не удалось очистить уведомления: '+type(exc).__name__)

    def open_outputs(self):
        folder=ROOT/'recordings';folder.mkdir(exist_ok=True)
        if hasattr(os,'startfile'):os.startfile(str(folder))
    def refresh(self):
        try:
            streams=self.service.stream_rows(self.config)
            self.streams.delete(*self.streams.get_children())
            for row in streams:self.streams.insert('', 'end',values=row)
            selected=self.jobs.selection()
            self.jobs.delete(*self.jobs.get_children())
            for j in self.service.store.list_jobs(200):
                labels={'queued':'В очереди','running':'В работе','done':'Готово','failed':'Ошибка','retry':'Повтор'}
                self.jobs.insert('','end',iid=str(j['id']),values=(j['id'],('Тест: '+j['local_name']) if j.get('local_name') else j['streamer'], 'локально' if j.get('local_name') else j['vod_id'],labels.get(j['state'],j['state']),j['stage'],j['detail']))
            for item in selected:
                if self.jobs.exists(item):self.jobs.selection_add(item)
            text='\n'.join(__import__('datetime').datetime.fromtimestamp(t).strftime('%H:%M:%S')+'  '+m for t,m in self.service.logs()[-30:])
            # Redrawing every three seconds used to clear the user's selection.
            # Keep it until the selection is cleared, then catch up on new events.
            if not self.events.tag_ranges('sel') and self.events.get('1.0','end-1c') != text:
                self.events.configure(state='normal'); self.events.delete('1.0','end');self.events.insert('1.0',text);self.events.configure(state='disabled')
            rows=self.service.store.notifications(200)
            notice_text='\n'.join(__import__('datetime').datetime.fromtimestamp(r['timestamp']).strftime('%d.%m %H:%M')+'  '+r['message'] for r in reversed(rows))
            if not self.notifications.tag_ranges('sel') and self.notifications.get('1.0','end-1c') != notice_text:
                self.notifications.configure(state='normal');self.notifications.delete('1.0','end');self.notifications.insert('1.0',notice_text);self.notifications.configure(state='disabled');self.notifications.see('end')
        except Exception as exc:self.status.set('Не удалось обновить состояние: '+type(exc).__name__)
        self.window.after(3000,self.refresh)
    def close(self):
        active=any(j['state']=='running' for j in self.service.store.list_jobs(1000))
        if active and not messagebox.askyesno('Выход','Обработка ещё идёт. Выход прервёт её. Выйти?'):return
        self.service.request_stop()
        if not active:
            threading.Thread(target=self.finder.close,daemon=True).start()
        self.window.destroy()

if __name__=='__main__':
    root=tk.Tk()
    try:
        lock=single_instance.acquire(ROOT)
    except (single_instance.AlreadyRunning,OSError) as exc:
        root.withdraw();messagebox.showwarning('Нарезкобот',str(exc));root.destroy()
    else:
        try:App(root);root.mainloop()
        finally:single_instance.release(lock)
