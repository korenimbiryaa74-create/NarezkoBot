"""Streamer assets for reaction previews: transparent PNG cutouts and a style example.

thumbnail_assets/<login>/NN.png   cutouts (transparent background, trimmed)
thumbnail_styles/<login>.jpg      one example thumbnail of the streamer's style
"""
from __future__ import annotations
import re
from pathlib import Path
from PIL import Image,ImageOps

_LOGIN=re.compile(r'^[a-z0-9_]{1,25}$')
MAX_PIXELS=40_000_000


class AssetError(ValueError):
    pass


def normalize_login(value):
    value=str(value or '').strip().lower().lstrip('@')
    if not _LOGIN.fullmatch(value):raise AssetError('Введите Twitch-ник: латинские буквы, цифры или _ (до 25 символов).')
    return value


def assets_dir(root,login):
    return Path(root)/'thumbnail_assets'/normalize_login(login)


def style_path(root,login):
    folder=Path(root)/'thumbnail_styles';login=normalize_login(login)
    for ext in ('.jpg','.jpeg','.png'):
        p=folder/(login+ext)
        if p.is_file() and not p.is_symlink():return p
    return None


def bind(root,login):
    folder=assets_dir(root,login);folder.mkdir(parents=True,exist_ok=True);return folder


def streamers(root):
    base=Path(root)/'thumbnail_assets';out=[]
    if not base.is_dir():return out
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and _LOGIN.fullmatch(p.name)):
        files=sorted(p for p in folder.glob('*.png') if p.is_file() and not p.is_symlink())
        out.append({'login':folder.name,'cutouts':files,'style':style_path(root,folder.name),'folder':folder})
    return out


def _next_name(folder):
    nums=[int(m.group(1)) for p in folder.glob('*.png') for m in [re.fullmatch(r'(\d+)',p.stem)] if m]
    return f'{(max(nums)+1) if nums else 1:02d}.png'


def add_cutout(root,login,path):
    """Validate a transparent PNG, trim empty margins and store it. Returns the new path."""
    folder=bind(root,login)
    try:
        with Image.open(path) as src:
            if src.width*src.height>MAX_PIXELS:raise AssetError('Картинка слишком большая')
            has_alpha='A' in src.getbands() or (src.mode=='P' and 'transparency' in src.info)
            im=ImageOps.exif_transpose(src).convert('RGBA')
    except AssetError:raise
    except Exception as exc:raise AssetError(f'Не удалось открыть {Path(path).name}: {exc}') from None
    a=im.getchannel('A')
    if not has_alpha or a.getextrema()==(255,255):
        raise AssetError(f'{Path(path).name}: нет прозрачного фона. Нужен PNG без фона (вырез стримера).')
    box=a.point(lambda v:255 if v>20 else 0).getbbox()
    if not box:raise AssetError(f'{Path(path).name}: картинка полностью прозрачная')
    im=im.crop(box)
    if im.height<200:raise AssetError(f'{Path(path).name}: слишком маленький вырез ({im.width}×{im.height}); нужно хотя бы 200 px по высоте')
    dest=folder/_next_name(folder);im.save(dest,'PNG',optimize=True)
    return dest


def remove_cutout(root,login,path):
    folder=assets_dir(root,login).resolve();p=Path(path).resolve()
    if p.parent!=folder or p.suffix.lower()!='.png':raise AssetError('Файл не из папки стримера')
    p.unlink()


def set_style(root,login,path):
    login=normalize_login(login);folder=Path(root)/'thumbnail_styles';folder.mkdir(parents=True,exist_ok=True)
    try:
        with Image.open(path) as src:
            if src.width*src.height>MAX_PIXELS:raise AssetError('Картинка слишком большая')
            im=ImageOps.exif_transpose(src).convert('RGB')
    except AssetError:raise
    except Exception as exc:raise AssetError(f'Не удалось открыть картинку: {exc}') from None
    for ext in ('.jpg','.jpeg','.png'):
        old=folder/(login+ext)
        if old.is_file():old.unlink()
    dest=folder/(login+'.jpg');im.save(dest,'JPEG',quality=92);return dest


def thumbnail_bytes(path,height=110):
    """Small PNG for the UI (keeps transparency)."""
    from io import BytesIO
    with Image.open(path) as src:
        im=src.convert('RGBA');im.thumbnail((height*2,height))
        buf=BytesIO();im.save(buf,'PNG');return buf.getvalue()
