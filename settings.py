"""Settings persisted locally; never overwrite a damaged configuration."""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from monitor import normalize_login
from pipeline import atomic_json
from proxy_setup import parse_proxy
from youtube_descriptions import DEFAULT_FOOTER
import long_streams

DEFAULTS={
 'twitch':{'client_id':'','client_secret':''},
 'youtube':{'enabled':False,'auto_approve_reactions':False,'description_footer':DEFAULT_FOOTER},
 'streamers':[], 'streamer_modes':{}, 'monitor':{'enabled':False,'interval':60,'profiles_imported':False},
 'proxy':{'enabled':False,'url':'','twitch_api':False,'chatgpt':True,'groq':False},
 'pipeline':{'quality':'1080p60','download_threads':4,'min_reaction_minutes':10,'bridge_minutes':5,
             'speech':{'model':'large-v3-turbo','language':'ru','device':'auto','compute':'auto','beam':1,'groq_enabled':True,'groq_api_key':''},
             'silence':{'enabled':True,'min_cut':1.5,'max_gap':2.0,'onset_db':12.0,'max_height':1080,'speed':'auto'},
             'low_impact':{'enabled':True,'mode':'auto','games':['dota2.exe'],'cpu_share':0.5,'priority':'below_normal',
                           'game_speed':'eco','free_speed':'turbo','free_download_threads':16,'free_chat_threads':8,'game_bandwidth_kib':0},
             'keep_original':False,'remove_donations':True,'long_stream':dict(long_streams.DEFAULT),'preview':{'enabled':False,'test_streamer':'','templates':{},'prompts':{}},'banner_blur':{'enabled':False,'interval':5,'test_streamer':''}}}

def merge(base, override):
    result=copy.deepcopy(base)
    for k,v in override.items():
        if isinstance(v,dict) and isinstance(result.get(k),dict): result[k]=merge(result[k],v)
        else: result[k]=v
    return result

def _clip_limit(value):
    """Clips per stream for a streamer: 0 = no limit, otherwise 1..50."""
    try: value=int(value or 0)
    except (TypeError,ValueError): value=0
    return max(0,min(50,value))

def validate(config):
    result=merge(DEFAULTS,config)
    if not isinstance(result.get('youtube'),dict) or not isinstance(result['youtube'].get('enabled'),bool): raise ValueError('Некорректная настройка YouTube')
    result['youtube']['auto_approve_reactions']=bool(result['youtube'].get('auto_approve_reactions',False))
    footer=result['youtube'].get('description_footer')
    if not isinstance(footer,str): footer=DEFAULT_FOOTER
    footer=footer.replace('\r\n','\n').replace('<','').replace('>','')
    if len(footer)>3000: raise ValueError('Подпись описания YouTube: не длиннее 3000 символов')
    result['youtube']['description_footer']=footer
    # Английские (США) каналы: своя подпись и язык по логину стримера.
    fen=result['youtube'].get('description_footer_en')
    if not isinstance(fen,str): fen='💬 My Telegram - https://t.me/korenmbr'
    result['youtube']['description_footer_en']=fen.replace('\r\n','\n').replace('<','').replace('>','')[:3000]
    langs=result.get('languages') if isinstance(result.get('languages'),dict) else {}
    result['languages']={normalize_login(k):('en' if str(v).lower()=='en' else 'ru') for k,v in langs.items() if str(k).strip()}
    result['streamers']=list(dict.fromkeys(normalize_login(s) for s in result['streamers']))
    modes=result.get('streamer_modes') if isinstance(result.get('streamer_modes'),dict) else {}
    result['streamer_modes']={normalize_login(k):{'reaction':bool((v or {}).get('reaction',True)),'discussion':bool((v or {}).get('discussion',True)),
                                                  'game':bool((v or {}).get('game',False)),
                                                  'max_clips':_clip_limit((v or {}).get('max_clips',0))}
                              for k,v in modes.items() if isinstance(v,dict) and str(k).strip()}
    m=result['monitor']; m['interval']=int(m['interval'])
    if not 15<=m['interval']<=3600: raise ValueError('Интервал мониторинга: от 15 до 3600 секунд')
    p=result['proxy']
    if p['enabled']: parse_proxy(p['url'])
    q=result['pipeline']; q['download_threads']=int(q['download_threads'])
    if not 1<=q['download_threads']<=32: raise ValueError('Потоки загрузки: от 1 до 32')
    for key in ('min_reaction_minutes','bridge_minutes'):
        q[key]=float(q[key])
        if not 0<=q[key]<=180: raise ValueError('Время реакции: от 0 до 180 минут')
    if not isinstance(q['remove_donations'],bool): raise ValueError('Некорректная настройка удаления донатов')
    q['long_stream']=long_streams.normalize(q.get('long_stream'))
    preview=q.get('preview')
    if not isinstance(preview,dict) or not isinstance(preview.get('enabled'),bool): raise ValueError('Некорректная настройка превью')
    test_login=preview.get('test_streamer','')
    if not isinstance(test_login,str): raise ValueError('Некорректный ник для теста превью')
    preview['test_streamer']=normalize_login(test_login) if test_login.strip() else ''
    templates=preview.get('templates') if isinstance(preview.get('templates'),dict) else {}
    preview['templates']={normalize_login(k):('manual' if v=='manual' else 'auto') for k,v in templates.items() if str(k).strip()}
    # Per-streamer opening of the preview request (everything before «<надпись>» and «контекст: …»).
    prompts=preview.get('prompts') if isinstance(preview.get('prompts'),dict) else {}
    preview['prompts']={normalize_login(k):str(v).replace('\r\n','\n').strip()[:2000] for k,v in prompts.items()
                        if str(k).strip() and isinstance(v,str) and v.strip()}
    # Per-streamer YouTube channels to take preview samples from (links, @handles or UC ids).
    chans=preview.get('channels') if isinstance(preview.get('channels'),dict) else {}
    preview['channels']={normalize_login(k):[str(x).strip() for x in (v if isinstance(v,list) else re.split(r'[\s,;]+',str(v or ''))) if str(x).strip()][:20]
                         for k,v in chans.items() if str(k).strip() and v}
    preview['channels']={k:v for k,v in preview['channels'].items() if v}
    if not isinstance(q['banner_blur'],dict) or not isinstance(q['banner_blur'].get('enabled'),bool): raise ValueError('Некорректная настройка блюра баннеров')
    q['banner_blur']['interval']=int(q['banner_blur']['interval'])
    if not 1<=q['banner_blur']['interval']<=60: raise ValueError('Интервал поиска баннеров: от 1 до 60 секунд')
    test_login=q['banner_blur'].get('test_streamer','')
    if not isinstance(test_login,str): raise ValueError('Некорректный ник для теста блюра')
    q['banner_blur']['test_streamer']=normalize_login(test_login) if test_login.strip() else ''
    silence=q['silence']
    for key in ('min_cut','max_gap','onset_db'):
        silence[key]=float(silence[key])
        if not 0<=silence[key]<=120: raise ValueError('Некорректные параметры тишины')
    silence['max_height']=int(silence['max_height'])
    if silence['max_height'] not in (0,480,720,1080,1440,2160): raise ValueError('Некорректное разрешение')
    if silence['speed'] not in ('auto','eco','quality','fast','max','turbo'): raise ValueError('Некорректная скорость рендера')
    li=q['low_impact']; li['enabled']=bool(li.get('enabled',True))
    li['cpu_share']=float(li.get('cpu_share',0.5))
    if not 0.1<=li['cpu_share']<=1: raise ValueError('Доля ядер для бота: от 0.1 до 1')
    if li.get('priority') not in ('below_normal','idle'): li['priority']='below_normal'
    if li.get('mode') not in ('auto','always','off'): li['mode']='auto'
    games=li.get('games') if isinstance(li.get('games'),list) else re.split(r'[\s,;]+',str(li.get('games') or ''))
    li['games']=[str(g).strip().lower() for g in games if str(g).strip()][:20] or ['dota2.exe']
    for key,default in (('game_speed','eco'),('free_speed','turbo')):
        if li.get(key) not in ('eco','quality','fast','max','turbo'): li[key]=default
    for key,default,lo,hi in (('free_download_threads',16,1,32),('free_chat_threads',8,1,16),('game_bandwidth_kib',0,0,1000000)):
        try: li[key]=max(lo,min(hi,int(li.get(key,default))))
        except (TypeError,ValueError): li[key]=default
    return result

def import_legacy_groq_key(path):
    """Read only the Groq key from a user-selected local legacy configuration."""
    try:
        with open(path,encoding='utf-8-sig') as f: data=json.load(f)
        key=data.get('timecodes',{}).get('groq_api_key','')
        if not isinstance(key,str) or not key.strip(): raise ValueError()
        return key.strip()
    except (OSError,ValueError,TypeError,AttributeError):
        raise ValueError('Не удалось прочитать ключ timecodes.groq_api_key из выбранного JSON') from None

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
