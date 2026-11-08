"""Per-streamer fixed blur rectangles and a local frame from latest Twitch VOD."""
from __future__ import annotations
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from monitor import normalize_login
from banner_blur import BannerSetupError, full_frame_at


def profile_path(root, login):
    return Path(root) / 'state' / 'blur_profiles' / (normalize_login(login) + '.json')


def profile_logins(root):
    """List validly named saved profiles without altering their contents."""
    directory=Path(root)/'state'/'blur_profiles'
    if not directory.exists(): return []
    names=set()
    for path in directory.glob('*.json'):
        if not path.is_file() or path.is_symlink(): continue
        try: names.add(normalize_login(path.stem))
        except ValueError: continue
    return sorted(names)


def validate_profile(value):
    if not isinstance(value, dict) or value.get('version') != 1 or value.get('mode') not in ('regions','none'):
        raise BannerSetupError('Повреждён профиль блюра; настройте области заново')
    boxes=value.get('boxes')
    if not isinstance(boxes,list) or len(boxes)>16 or (value['mode']=='none' and boxes) or (value['mode']=='regions' and not boxes):
        raise BannerSetupError('Некорректный список областей блюра')
    for box in boxes:
        if (not isinstance(box,list) or len(box)!=4 or any(type(x) is not int or not 0<=x<=1000 for x in box)
            or box[2]<=box[0] or box[3]<=box[1] or box[2]-box[0]<4 or box[3]-box[1]<4):
            raise BannerSetupError('Некорректные координаты области блюра')
    return {'version':1,'mode':value['mode'],'boxes':[list(b) for b in boxes]}


def display_box(x0,y0,x1,y1,width,height):
    if width<=0 or height<=0:raise ValueError('Некорректный размер кадра')
    x0=max(0,min(width,x0));x1=max(0,min(width,x1))
    y0=max(0,min(height,y0));y1=max(0,min(height,y1))
    x0,x1=sorted((x0,x1));y0,y1=sorted((y0,y1))
    return [math.floor(x0*1000/width),math.floor(y0*1000/height),
            math.ceil(x1*1000/width),math.ceil(y1*1000/height)]


def load_profile(root,login):
    path=profile_path(root,login)
    if not path.exists():
        raise BannerSetupError(f'Для {normalize_login(login)} не настроен блюр. Откройте «Настроить блюр стримера» или выберите «Без блюра».')
    try:
        with path.open(encoding='utf-8-sig') as f: value=json.load(f)
        return validate_profile(value)
    except (OSError,ValueError,TypeError):
        raise BannerSetupError('Не удалось прочитать профиль блюра; исходный файл не изменён') from None


def save_profile(root,login,boxes):
    boxes=[list(b) for b in boxes]
    value=validate_profile({'version':1,'mode':'regions' if boxes else 'none','boxes':boxes})
    path=profile_path(root,login);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.blur-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return value


# Twitch's public web client and persisted public archive query. This is not a
# personal client ID or secret. Twitch may change its undocumented web API.
_WEB_CLIENT_ID = 'ue6666qo983tsx6so1t0vnawi233wa'
_ARCHIVES_QUERY = '67004f7881e65c297936f32c75246470629557a393788fb5a69d6d9a25a8fd5f'


def latest_public_vod(login, proxy=''):
    """Get the newest public archived broadcast; no user credentials required."""
    import requests
    login = normalize_login(login)
    query = [{'operationName':'FilterableVideoTower_Videos',
              'variables':{'channelOwnerLogin':login, 'broadcastType':'ARCHIVE',
                           'videoSort':'TIME', 'limit':10},
              'extensions':{'persistedQuery':{'version':1,'sha256Hash':_ARCHIVES_QUERY}}}]
    session = requests.Session()
    session.trust_env = False
    session.proxies = {'http':proxy,'https':proxy} if proxy else {}
    try:
        response = session.post('https://gql.twitch.tv/gql', json=query,
                                headers={'Client-ID':_WEB_CLIENT_ID}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload,list) or not payload or not isinstance(payload[0],dict) or payload[0].get('errors'):
            raise BannerSetupError('Twitch не вернул список записей. Попробуйте позже или настройте блюр по локальному видео.')
        data = payload[0].get('data')
        if not isinstance(data,dict) or 'user' not in data:
            raise BannerSetupError('Не удалось прочитать ответ Twitch. Попробуйте позже или выберите локальное видео.')
        user = data['user']
        if user is None:
            raise BannerSetupError(f'Стример {login} не найден на Twitch')
        try: edges = user['videos']['edges']
        except (KeyError, TypeError):
            raise BannerSetupError('Не удалось прочитать список записей Twitch. Попробуйте позже или выберите локальное видео.') from None
        if not isinstance(edges,list):
            raise BannerSetupError('Не удалось прочитать список записей Twitch. Попробуйте позже или выберите локальное видео.')
        if not edges:
            raise BannerSetupError(f'У {login} нет доступной сохранённой записи Twitch (VOD). Можно настроить блюр по локальному видео.')
        # The query is sorted by time; do not silently use another stream if the
        # newest one's metadata is malformed.
        try:
            node=edges[0]['node']; vod=str(node['id']); duration=float(node['lengthSeconds'])
            if not vod.isascii() or not vod.isdecimal() or not math.isfinite(duration) or duration<=0:
                raise ValueError()
        except (KeyError,TypeError,ValueError,OverflowError):
            raise BannerSetupError('Twitch вернул некорректные данные последней записи. Попробуйте позже или выберите локальное видео.') from None
        return vod, duration
    except BannerSetupError:
        raise
    except (requests.RequestException,ValueError,TypeError):
        raise BannerSetupError('Не удалось получить список записей Twitch. Проверьте подключение и прокси или выберите локальное видео.') from None
    finally:
        session.close()


def latest_frame(login,proxy,cli,ffmpeg,quality='160p30',threads=4):
    """Fetch a short excerpt of the latest public VOD without user API keys."""
    login=normalize_login(login)
    vod,duration=latest_public_vod(login,proxy)
    if not Path(cli).is_file():raise BannerSetupError('Не найден TwitchDownloaderCLI. Запустите INSTALL.bat')
    start=max(0,int(duration/2)-2);end=min(int(duration),start+8)
    if end<=start:raise BannerSetupError('Запись Twitch слишком короткая для получения кадра')
    def stamp(t):return f'{t//3600:02}:{t%3600//60:02}:{t%60:02}'
    with tempfile.TemporaryDirectory(prefix='narezko-frame-') as d:
        output=Path(d)/'sample.mp4'
        command=[str(cli),'videodownload','--id',vod,'-q',str(quality),'-t',str(int(threads)),
                 '--collision','Rename','-o',str(output),'-b',stamp(start),'-e',stamp(end)]
        env={k:v for k,v in os.environ.items() if not k.lower().endswith('_proxy')};env['NO_PROXY']='*';env['no_proxy']='*'
        try:r=subprocess.run(command,cwd=str(Path(cli).parent),env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except (OSError,subprocess.SubprocessError):raise BannerSetupError('Не удалось скачать короткий фрагмент VOD для кадра') from None
        if r.returncode or not output.is_file():raise BannerSetupError('Не удалось скачать короткий фрагмент VOD. Проверьте доступность записи и загрузчик')
        image=full_frame_at(output,0,ffmpeg)
        return image,vod,start
