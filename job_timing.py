"""Calculate foreground stage time from existing events, without polling the DB from the UI."""
import time


def summary(events,now=None):
    now=time.time() if now is None else now
    spent={}; cached=set(); stage=None; prev=None; attempts=0
    for e in [*events,{'stage':None,'timestamp':now}]:
        t=float(e['timestamp']); detail=str(e.get('detail','')).lower(); name=e['stage']
        if stage and prev is not None: spent[stage]=spent.get(stage,0)+max(0,t-prev)
        prev=t
        if name=='Запуск': attempts+=1
        if name=='Рендер':
            if 'склейка частей' in detail: stage='Склейка'
            elif 'проверка длительности частей' in detail or 'проверено частей:' in detail: stage='Проверка нарезки'
            else: stage='Рендер'
        elif name=='Поиск реакций': stage='Поиск / GPT'
        elif name in ('Скачивание','Транскрибация','Нарезка','Проверка нарезки','Организация файлов','Превью','Публикация YouTube'): stage=name
        else: stage=None
        if stage and ('кэш' in detail or 'без повторного запроса' in detail or 'повторного запроса не будет' in detail): cached.add(stage)
    if not spent: return 'Для этого задания пока нет интервалов времени.'
    def fmt(sec):
        n=round(sec); return f'{n//3600:02}:{n//60%60:02}:{n%60:02}'
    rows=[f'{key}: {fmt(value)}'+(' · использован кэш/готовый результат' if key in cached else '') for key,value in spent.items()]
    return f'Попыток: {attempts}. Время по событиям переднего плана; фоновая загрузка отдельно не суммируется.\n\n'+'\n'.join(rows)
