"""Settings persisted locally; never overwrite a damaged configuration."""
from __future__ import annotations
import copy
import json
from pathlib import Path
from monitor import normalize_login
from pipeline import atomic_json
from proxy_setup import parse_proxy

DEFAULTS={
 'twitch':{'client_id':'','client_secret':''},
 'streamers':[], 'monitor':{'enabled':False,'interval':60},
 'proxy':{'enabled':False,'url':'','twitch_api':False,'chatgpt':True},
 'pipeline':{'quality':'1080p60','download_threads':4,'min_reaction_minutes':10,'bridge_minutes':5,
             'speech':{'model':'large-v3-turbo','language':'ru','device':'auto','compute':'auto','beam':1},
             'silence':{'enabled':True,'min_cut':1.5,'max_gap':2.0,'onset_db':12.0,'max_height':1080,'speed':'turbo'},
             'keep_original':False}}

def merge(base, override):
    result=copy.deepcopy(base)
    for k,v in override.items():
        if isinstance(v,dict) and isinstance(result.get(k),dict): result[k]=merge(result[k],v)
        else: result[k]=v
    return result

def validate(config):
    result=merge(DEFAULTS,config)
    result['streamers']=list(dict.fromkeys(normalize_login(s) for s in result['streamers']))
    m=result['monitor']; m['interval']=int(m['interval'])
    if not 15<=m['interval']<=3600: raise ValueError('Интервал мониторинга: от 15 до 3600 секунд')
    p=result['proxy']
    if p['enabled']: parse_proxy(p['url'])
    q=result['pipeline']; q['download_threads']=int(q['download_threads'])
    if not 1<=q['download_threads']<=32: raise ValueError('Потоки загрузки: от 1 до 32')
    for key in ('min_reaction_minutes','bridge_minutes'):
        q[key]=float(q[key])
        if not 0<=q[key]<=180: raise ValueError('Время реакции: от 0 до 180 минут')
    silence=q['silence']
    for key in ('min_cut','max_gap','onset_db'):
        silence[key]=float(silence[key])
        if not 0<=silence[key]<=120: raise ValueError('Некорректные параметры тишины')
    silence['max_height']=int(silence['max_height'])
    if silence['max_height'] not in (0,480,720,1080,1440,2160): raise ValueError('Некорректное разрешение')
    if silence['speed'] not in ('quality','fast','max','turbo'): raise ValueError('Некорректная скорость рендера')
    return result

class Settings:
    def __init__(self,path): self.path=Path(path)
    def load(self):
        if not self.path.exists(): return copy.deepcopy(DEFAULTS)
        try:
            with open(self.path,encoding='utf-8-sig') as f: data=json.load(f)
            if not isinstance(data,dict): raise ValueError('конфигурация должна быть объектом')
            return validate(data)
        except (OSError,ValueError,TypeError,KeyError) as e:
            raise ValueError('Не удалось прочитать настройки; файл не изменён') from e
    def save(self,config):
        checked=validate(config); atomic_json(self.path,checked); return checked
