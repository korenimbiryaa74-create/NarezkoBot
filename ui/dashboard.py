"""Main screen: monitoring status, current work, streamers, job queue and events."""
from __future__ import annotations
import asyncio
import os
import flet as ft
from .common import card,chip,muted,page_header,empty,ts,MUTED

JOB={'queued':('В очереди','warn'),'running':('В работе','info'),'done':('Готово','ok'),'failed':('Ошибка','error'),'retry':('Повтор','warn')}


def _stat(icon,title):
    value=ft.Text('—',size=22,weight=ft.FontWeight.BOLD)
    sub=ft.Text('',size=12,color=MUTED,max_lines=2,overflow=ft.TextOverflow.ELLIPSIS)
    box=ft.Container(ft.Column([ft.Row([ft.Icon(icon,size=18,color=ft.Colors.PRIMARY),muted(title,size=13)],spacing=8),value,sub],spacing=6,tight=True),
                     bgcolor='#17191e',border=ft.Border.all(1,'#262a31'),border_radius=16,padding=16,expand=True)
    return box,value,sub


class Dashboard:
    def __init__(self,app):
        self.app=app;self._jobs_key=None;self._streams_key=None
        self.monitor_chip=ft.Container()
        self.btn_start=ft.FilledButton('Запустить мониторинг',icon=ft.Icons.PLAY_ARROW,on_click=lambda e:app.spawn(app.start_monitoring))
        self.btn_stop=ft.OutlinedButton('Остановить',icon=ft.Icons.STOP,on_click=lambda e:app.spawn(app.stop_monitoring))
        s1,self.now_value,self.now_sub=_stat(ft.Icons.AUTORENEW,'Сейчас')
        s2,self.queue_value,self.queue_sub=_stat(ft.Icons.MOVIE_FILTER,'Нарезки')
        s3,self.yt_value,self.yt_sub=_stat(ft.Icons.SMART_DISPLAY,'YouTube')
        s3.on_click=lambda e:app.navigate('youtube');s3.ink=True;s3.tooltip='Открыть YouTube'
        self.streams=ft.Column(spacing=6)
        self.jobs=ft.Column(spacing=8)
        self.events=ft.Text('',size=12,font_family='Consolas',selectable=True,color='#c9ced8')
        self.view=ft.Column([
            page_header('Нарезкобот','Экспериментальная сборка — не оставляйте без контроля',
                        actions=[self.monitor_chip,self.btn_start,self.btn_stop]),
            ft.Row([s1,s2,s3],spacing=16),
            card(ft.Row([
                ft.FilledTonalButton('Тест из папки',icon=ft.Icons.SCIENCE,on_click=lambda e:app.spawn(self._start_test)),
                ft.OutlinedButton('Импорт видео',icon=ft.Icons.FILE_OPEN,on_click=lambda e:app.spawn(self._import_video)),
                ft.FilledTonalButton('Запись Twitch по ссылке',icon=ft.Icons.LINK,on_click=lambda e:app.spawn(self._add_vod)),
                ft.FilledTonalButton('Продолжить очередь',icon=ft.Icons.PLAY_CIRCLE_OUTLINE,on_click=lambda e:app.spawn(self._resume)),
                ft.OutlinedButton('Папка тестовых видео',icon=ft.Icons.FOLDER_OPEN,on_click=lambda e:app.open_folder('test_input')),
                ft.OutlinedButton('Папка результатов',icon=ft.Icons.VIDEO_FILE,on_click=lambda e:app.open_folder('recordings')),
            ],spacing=10,wrap=True),title='Быстрые действия',icon=ft.Icons.BOLT),
            card(self.streams,title='Стримеры',icon=ft.Icons.LIVE_TV,subtitle='Отслеживаемые каналы и ожидаемые записи'),
            card(self.jobs,title='Очередь и история',icon=ft.Icons.HISTORY,
                 actions=[ft.TextButton('Очистить историю',icon=ft.Icons.CLEANING_SERVICES,on_click=lambda e:app.spawn(self._clear_history)),
                          ft.TextButton('Очистить очередь',icon=ft.Icons.DELETE_SWEEP,on_click=lambda e:app.spawn(self._clear_queue))]),
            card(ft.Container(ft.Column([self.events],scroll=ft.ScrollMode.AUTO),height=220,bgcolor='#101215',border_radius=10,padding=12),
                 title='Последние события',icon=ft.Icons.TERMINAL,
                 actions=[ft.IconButton(ft.Icons.CONTENT_COPY,tooltip='Скопировать все события',on_click=lambda e:app.spawn(self._copy_events))]),
        ],spacing=16,scroll=ft.ScrollMode.AUTO,expand=True)

    def refresh(self,snap):
        cfg=self.app.config;jobs=snap['jobs']
        on=bool(cfg['monitor']['enabled'])
        self.monitor_chip.content=chip('Мониторинг включён' if on else 'Мониторинг выключен','ok' if on else 'muted',
                                       ft.Icons.RADIO_BUTTON_CHECKED if on else ft.Icons.PAUSE_CIRCLE_OUTLINE)
        self.btn_start.visible=not on;self.btn_stop.visible=on
        running=[j for j in jobs if j['state']=='running']
        if running:
            j=running[0];self.now_value.value=self._name(j);self.now_sub.value=(j['stage'] or '')+(' · '+j['detail'] if j['detail'] else '')
        else:
            self.now_value.value='Нет активных задач';self.now_sub.value=f"Проверка каждые {cfg['monitor']['interval']} с · стримеров: {len(cfg['streamers'])}"
        count=lambda s:sum(1 for j in jobs if j['state']==s)
        self.queue_value.value=f"{count('queued')+count('retry')} в очереди"
        self.queue_sub.value=f"готово {count('done')} · ошибок {count('failed')}"
        ups=[u for u in snap['uploads'] if u['state']!='cancelled']
        review=sum(1 for u in ups if u['state']=='needs_review');queued=sum(1 for u in ups if u['state'] in ('queued','uploading'))
        self.yt_value.value=(f'{review} требуют проверки' if review else f'{queued} в очереди')
        self.yt_sub.value=('Публикация включена' if cfg.get('youtube',{}).get('enabled') else 'Публикация выключена')+f" · каналов: {len(snap['profiles'])}"
        self._refresh_streams(snap['streams']);self._refresh_jobs(jobs)
        text='\n'.join(ts(t,'%H:%M:%S')+'  '+m for t,m in snap['logs'][-40:])
        if self.events.value!=text:self.events.value=text or 'Событий пока нет'

    @staticmethod
    def _name(j):
        return ('Тест: '+j['local_name']) if j.get('local_name') else j['streamer']

    def _refresh_streams(self,rows):
        key=tuple(tuple(r) for r in rows)
        if key==self._streams_key:return
        self._streams_key=key;out=[]
        for row in rows:
            login,sid,state,details=(list(row)+['','','',''])[:4]
            actions=[]
            if sid and state in ('В эфире','Эфир пропускается'):
                actions.append(ft.Checkbox(label='Пропустить этот эфир',value=state=='Эфир пропускается',
                    on_change=lambda e,n=login,s=sid:self.app.spawn(self._set_skip,n,s,bool(e.control.value))))
            kind='error' if details and ('ошиб' in str(details).lower() or 'error' in str(details).lower()) else ('info' if 'эфир' in str(state).lower() or 'live' in str(state).lower() else 'muted')
            out.append(ft.Container(ft.Row([ft.Icon(ft.Icons.PERSON,size=18,color=MUTED),ft.Text(str(login),size=14,weight=ft.FontWeight.W_600,width=150),
                                            chip(str(state) or '—',kind),muted(str(sid or ''),size=12),
                                            ft.Text(str(details or ''),size=12,color=MUTED,expand=True,max_lines=2,overflow=ft.TextOverflow.ELLIPSIS,selectable=True),*actions],
                                           spacing=12,vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                    padding=ft.Padding.symmetric(horizontal=12,vertical=8),bgcolor='#1d2026',border_radius=10))
        self.streams.controls=out or [empty('Стримеры не добавлены — укажите их в настройках.')]

    async def _set_skip(self,login,sid,value):
        try:
            changed=await asyncio.to_thread(self.app.service.set_stream_skipped,login,sid,value)
            if not changed:self.app.dialogs.toast('Эфир уже сменился или закончился. Ничего не изменено.','warn')
        finally:
            self._streams_key=None
            self.app.refresh_soon()

    def _refresh_jobs(self,jobs):
        jobs=jobs[:80]
        key=tuple((j['id'],j['state'],j['stage'],j['detail']) for j in jobs)
        if key==self._jobs_key:return
        self._jobs_key=key
        self.jobs.controls=[self._job_row(j) for j in jobs] or [empty('Заданий пока нет')]

    def _job_row(self,j):
        label,kind=JOB.get(j['state'],(j['state'],'muted'))
        app=self.app;acts=[]
        if j['state']=='failed':
            acts+=[ft.IconButton(ft.Icons.REPLAY,tooltip='Повторить задание',on_click=lambda e,j=j:app.spawn(self._retry,j)),
                   ft.IconButton(ft.Icons.CONTENT_PASTE,tooltip='Проверить и исправить ответ ChatGPT',on_click=lambda e,j=j:app.spawn(self._import_answer,j))]
        if j['state']=='done':
            acts.append(ft.IconButton(ft.Icons.INVENTORY_2,tooltip='Собрать готовые файлы',on_click=lambda e,j=j:app.spawn(self._organize,j)))
        if j.get('output_dir'):
            acts.append(ft.IconButton(ft.Icons.FOLDER_OPEN,tooltip='Открыть папку',on_click=lambda e,j=j:app.open_path(j['output_dir'])))
        acts.append(ft.IconButton(ft.Icons.CONTENT_COPY,tooltip='Скопировать подробности',on_click=lambda e,j=j:app.spawn(self._copy_job,j)))
        acts.append(ft.IconButton(ft.Icons.TIMER,tooltip='Время этапов и кэш',on_click=lambda e,j=j:app.spawn(self._timing,j)))
        vod='локально' if j.get('local_name') else str(j['vod_id'])
        return ft.Container(ft.Row([
            muted('№'+str(j['id']),size=12,width=44),
            ft.Column([ft.Text(self._name(j),size=14,weight=ft.FontWeight.W_600,max_lines=1,overflow=ft.TextOverflow.ELLIPSIS),
                       muted(f"VOD {vod} · {j['stage'] or ''}",size=12)],spacing=2,tight=True,width=280),
            chip(label,kind),
            ft.Text(j['detail'] or '',size=12,color=MUTED,expand=True,max_lines=2,overflow=ft.TextOverflow.ELLIPSIS,selectable=True),
            ft.Row(acts,spacing=0,tight=True)],spacing=12,vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=12,vertical=6),bgcolor='#1d2026',border_radius=10)

    # ---------- actions ----------
    async def _start_test(self):
        app=self.app
        if not await app.ensure_settings_saved():return
        try:
            added,active=app.service.queue_test_inputs()
            pending=app.service.pending_input_count
            if not added and not active and not pending:await app.dialogs.info('Тест','Поместите видео в папку test_input и запустите тест снова.');return
            if not app.service.start():await app.dialogs.info('Тест','Задание в очереди; дождитесь остановки и нажмите «Продолжить очередь».');return
            app.dialogs.toast(f'Тест: добавлено {added}, уже в очереди {active}, ещё копируются {pending}','ok');app.refresh_soon()
        except Exception as exc:await app.dialogs.error('Тест',str(exc))

    async def _import_video(self):
        import local_intake
        app=self.app
        files=await app.file_picker.pick_files(dialog_title='Импорт в test_input',allowed_extensions=sorted(x[1:] for x in local_intake.EXTENSIONS))
        if not files or not files[0].path:return
        try: path=await asyncio.to_thread(local_intake.import_file,files[0].path,app.root/'test_input')
        except (OSError,ValueError) as exc: await app.dialogs.error('Импорт видео',str(exc));return
        await app.dialogs.info('Импорт видео',f'{path.name} скопирован. Теперь можно нажать «Тест из папки».')

    async def _timing(self,j):
        from job_timing import summary
        events=await asyncio.to_thread(self.app.service.store.events,j['id'])
        await self.app.dialogs.info(f"Время этапов · №{j['id']}",summary(events))

    async def _add_vod(self):
        """Process any Twitch VOD by link (e.g. to test game clips on a streamer that is not monitored)."""
        app=self.app
        if not await app.ensure_settings_saved():return
        url=ft.TextField(label='Ссылка на запись Twitch (twitch.tv/videos/…)',width=580,autofocus=True)
        cb_r=ft.Checkbox(label='Реакции',value=True);cb_d=ft.Checkbox(label='Разговоры',value=True);cb_g=ft.Checkbox(label='Игры',value=False)
        from .youtube_view import limit_dropdown
        limit=limit_dropdown(keep=True)
        cb_fast=ft.Checkbox(label='Быстрый тест: низкое качество 160p, без повторной загрузки и без автопубликации',value=True)
        body=ft.Column([url,ft.Row([muted('Что искать:',size=12),cb_r,cb_d,cb_g],wrap=True,spacing=12,vertical_alignment=ft.CrossAxisAlignment.CENTER),limit,cb_fast,
                        muted('Отметки сохранятся для этого стримера (как в карточке канала на странице YouTube). «Игры» — целые матчи, '
                              'не больше 4 за стрим, с учётом чата. Если канал YouTube для стримера не привязан, нарезки останутся '
                              'в папке результатов — выложить их можно через «Выложить нарезки…».',size=12)],tight=True,spacing=14)
        if await app.dialogs._show('Обработать запись Twitch',body,[('Отмена',None,False),('Обработать','ok',True)],icon=ft.Icons.LINK,width=660)!='ok':return
        import twitch_vod
        vid=twitch_vod.parse_vod_id(url.value)
        if not vid:await app.dialogs.error('Запись Twitch','Не похоже на ссылку на запись: нужна ссылка вида twitch.tv/videos/1234567890.');return
        r,d,g=bool(cb_r.value),bool(cb_d.value),bool(cb_g.value)
        if not (r or d or g):await app.dialogs.error('Запись Twitch','Отметьте хотя бы один тип моментов.');return
        try:info=await asyncio.to_thread(twitch_vod.vod_info,vid)
        except Exception as exc:await app.dialogs.error('Запись Twitch',f'Twitch не ответил: {exc}');return
        if not info or not info['login']:await app.dialogs.error('Запись Twitch','Запись не найдена: удалена, ещё не доступна или только для подписчиков.');return
        login=info['login']
        import manual_blur
        if app.config['pipeline'].get('banner_blur',{}).get('enabled') and not manual_blur.profile_path(app.root,login).exists():
            # Without a blur profile the job would stop with an error before downloading anything.
            if not await app.dialogs.confirm('Запись Twitch',f'Для {login} блюр баннеров не настроен. Обработать без блюра? '
                                             '(Настроить блюр можно позже на странице «Блюр».)','Без блюра'):return
            try:manual_blur.save_profile(app.root,login,[])
            except Exception as exc:await app.dialogs.error('Запись Twitch',f'Не удалось сохранить профиль блюра: {exc}');return
        values={'reaction':r,'discussion':d,'game':g}
        if limit.value not in (None,'','keep'):values['max_clips']=int(limit.value)
        # Merge so the streamer's other settings (e.g. the clip limit) survive.
        if not app.save_config(lambda c:c.setdefault('streamer_modes',{}).setdefault(login,{}).update(values)):return
        from pipeline import atomic_json,job_options_path
        try:atomic_json(job_options_path(app.root,vid),{'fast_test':bool(cb_fast.value)})
        except (OSError,ValueError) as exc:await app.dialogs.error('Запись Twitch',f'Не удалось сохранить настройки задания: {exc}');return
        try:job,created=app.service.store.add(vid,login,info['title'],duration_seconds=info['duration'] or None)
        except ValueError as exc:await app.dialogs.error('Запись Twitch',str(exc));return
        if not created:
            await app.dialogs.info('Запись Twitch',f"Эта запись уже есть в истории: задание №{job['id']}, состояние «{job['state']}». "
                                   'Повторно её не добавляю (если была ошибка — нажмите «Повторить» в списке заданий).');return
        app.service.log(f"Запись {vid} ({login}, {info['game'] or 'без категории'}) добавлена вручную"+(' — быстрый тест 160p' if cb_fast.value else ''))
        if not app.service.start():
            await app.dialogs.info('Запись Twitch','Задание в очереди; дождитесь завершения текущей операции и нажмите «Продолжить очередь».')
        else:app.dialogs.toast(f"В очереди: {login} · {info['title'][:60]}",'ok')
        app.refresh_soon()

    async def _resume(self):
        app=self.app
        if not await app.ensure_settings_saved():return
        if app.service.start():app.dialogs.toast('Очередь запущена','ok')
        else:await app.dialogs.info('Очередь','Дождитесь завершения текущей операции.')

    async def _retry(self,j):
        app=self.app
        if not await app.dialogs.confirm('Повтор','Проверьте, что предыдущий запрос ChatGPT не выполняется. Повторить задание №'+str(j['id'])+'?','Повторить'):return
        try:app.service.store.retry(int(j['id']));app.refresh_soon()
        except ValueError as exc:await app.dialogs.error('Повтор',str(exc))

    async def _import_answer(self,j):
        app=self.app
        from pipeline import load_json,manual_answer_path,job_identity
        draft=load_json(manual_answer_path(app.root,j['id']).with_suffix('.draft.json')) or {}
        text=draft.get('text','') if draft.get('job')==job_identity(j) else ''
        field=ft.TextField(label='Полный ответ для этого задания',value=text or (await app.clipboard.get()) or '',multiline=True,min_lines=5,max_lines=12)
        body=ft.Column([ft.Text(str(j.get('detail') or ''),selectable=True),field],tight=True,scroll=ft.ScrollMode.AUTO)
        if await app.dialogs._show(f"Ответ ChatGPT · №{j['id']}",body,[('Отмена',None,False),('Проверить и сохранить','ok',True)],width=720)!='ok':return
        text=field.value or ''
        try:
            count=app.service.save_manual_reactions(int(j['id']),text)
            await app.dialogs.info('Ответ ChatGPT',f'Ответ сохранён; интервалов: {count}. Теперь повторите задание и продолжите очередь.')
        except (ValueError,OSError) as exc:await app.dialogs.error('Ответ ChatGPT',str(exc))

    async def _organize(self,j):
        app=self.app
        if not await app.dialogs.confirm('Готовые файлы','Собрать видео и транскрипты задания в отдельную папку и создать отчёты о паузах? Повторная обработка не потребуется.','Собрать'):return
        try:
            folder,count=app.service.organize_finished(int(j['id']))
            await app.dialogs.info('Готовые файлы',f'Собрано видео: {count}\nПапка: {folder}')
        except Exception as exc:await app.dialogs.error('Готовые файлы',str(exc))

    async def _copy_job(self,j):
        text=(f"Задание №{j['id']}: {self._name(j)}\nСостояние: {j['state']}\nЭтап: {j['stage']}\nПодробности: {j['detail'] or 'нет'}")
        await self.app.clipboard.set(text);self.app.dialogs.toast('Скопировано. Проверьте текст на секреты перед отправкой.')

    async def _copy_events(self):
        text='\n'.join(ts(t,'%H:%M:%S')+'  '+m for t,m in self.app.service.logs())
        await self.app.clipboard.set(text);self.app.dialogs.toast('События скопированы')

    async def _clear_history(self):
        app=self.app
        if not await app.dialogs.confirm('Очистить историю','Скрыть завершённые задания и задания с ошибкой? Ожидающие и текущие задания, кэш, результаты и мониторинг сохранятся.','Очистить'):return
        try:
            count=app.service.store.clear_history();app.service.log(f'История очищена: скрыто заданий {count}')
            app.dialogs.toast(f'История очищена: {count}','ok');app.refresh_soon()
        except Exception as exc:await app.dialogs.error('История','Не удалось очистить историю: '+type(exc).__name__)

    async def _clear_queue(self):
        app=self.app
        if not await app.dialogs.confirm('Очистить очередь','Отменить ожидающие задания нарезки и повторы? Текущее задание продолжится. Очередь YouTube не изменится. Кэш и результаты сохранятся.','Очистить'):return
        try:
            count=app.service.store.clear_queue();app.service.log(f'Очередь очищена: отменено заданий {count}')
            app.dialogs.toast(f'Очередь очищена: {count}','ok');app.refresh_soon()
        except Exception as exc:await app.dialogs.error('Очередь','Не удалось очистить очередь: '+type(exc).__name__)
