"""YouTube descriptions: the streamer's links (from the Twitch channel) plus the owner's footer.

Links come from the public Twitch GQL (channel social links + URLs in the channel description)
and are cached per streamer in state/twitch_links/<login>.json. With no links only Twitch is listed.
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
import requests
import youtube_titles

GQL='https://gql.twitch.tv/gql'
CLIENT_ID='kimne78kx3ncx6brgo4mv6wki5h1ko'
QUERY=('query($l:String!){user(login:$l){login displayName description channel{socialMedias{name title url}} '
       'panels{__typename ... on DefaultPanel{title description linkURL}}}}')
TTL=7*24*3600
SEPARATOR='-'*72
DEFAULT_FOOTER=SEPARATOR+'\n💬 МОЙ TELEGRAMM - https://t.me/korenmbr\n'+SEPARATOR
LIMIT=5000  # YouTube description limit (characters)

# (domain regex, kind, emoji, label) — order here is the order in the description.
KINDS=(
    (r'(?:t\.me|telegram\.me)','telegram','💬','Telegram'),
    (r'boosty\.to','boosty','💎','Boosty'),
    (r'donationalerts\.com','donationalerts','💎','DonationAlerts'),
    (r'(?:donatty\.com|donatepay\.ru|donatov\.net|donate\.stream|streamelements\.com)','donate','💎','Донат'),
    (r'(?:youtube\.com|youtu\.be)','youtube','▶️','YouTube'),
    (r'tiktok\.com','tiktok','🎵','TikTok'),
    (r'instagram\.com','instagram','📸','Instagram'),
    (r'(?:vk\.com|vk\.ru|vkvideo\.ru)','vk','🔵','VK'),
    (r'(?:discord\.gg|discord\.com)','discord','🎧','Discord'),
    (r'(?:x\.com|twitter\.com)','x','🐦','X'),
)
_ORDER={k[1]:i for i,k in enumerate(KINDS)}
_URL=re.compile(r'(?:https?://)?(?:www\.)?(?:'+'|'.join(k[0] for k in KINDS)+r')/[^\s,;()<>«»"\'\]\[]+',re.I)
_LOGIN=re.compile(r'[a-z0-9_]{1,25}')


def _kind(url):
    host=re.sub(r'^https?://','',url,flags=re.I).split('/',1)[0].lower()
    host=host[4:] if host.startswith('www.') else host
    for pattern,kind,emoji,label in KINDS:
        if re.fullmatch(r'(?:[a-z0-9-]+\.)?'+pattern,host):return kind,emoji,label
    return None


def _clean_url(url):
    url=str(url or '').strip().rstrip('.,!?:;…')
    if not url or '<' in url or '>' in url or ' ' in url:return ''
    if not re.match(r'https?://',url,re.I):url='https://'+url
    return url


def _key(url):
    """Dedupe key: host + account part of the path (boosty.to/x and boosty.to/x/ref are the same)."""
    rest=re.sub(r'^https?://(?:www\.)?','',url.lower()).split('?',1)[0].split('#',1)[0]
    host,_,path=rest.partition('/')
    seg=[p for p in path.split('/') if p]
    keep=2 if seg and seg[0] in ('r','user','c','channel') else 1
    return host+'/'+'/'.join(seg[:keep])


def _junk(url):
    """Telegram bots / referral starts are ads, not the streamer's channel."""
    low=url.lower()
    return ('t.me/' in low or 'telegram.me/' in low) and ('?start' in low or re.search(r'/[a-z0-9_]*bot(?:[/?]|$)',low) is not None)


def parse_links(user):
    """Social links (declared ones first, then known social URLs from the channel description)."""
    found=[];seen=set()
    def add(url,title='',declared=False):
        url=_clean_url(url)
        if not url or _junk(url):return
        k=_key(url)
        if k in seen or 'twitch.tv' in k.split('/',1)[0]:return
        kind=_kind(url)
        if not kind:
            if not declared:return  # only well-known social sites from free text
            kind=('other','🔗',(str(title or '').strip() or k.split('/',1)[0])[:40])
        seen.add(k);found.append({'kind':kind[0],'emoji':kind[1],'label':kind[2],'title':str(title or '').strip()[:60],'url':url})
    channel=(user or {}).get('channel') or {}
    for item in channel.get('socialMedias') or []:
        if isinstance(item,dict):add(item.get('url'),item.get('title') or item.get('name'),True)
    # About panels: only well-known social sites (skips betting ads, merch shops, alerts).
    for panel in (user or {}).get('panels') or []:
        if not isinstance(panel,dict):continue
        if panel.get('linkURL'):add(panel['linkURL'],panel.get('title') or '')
        for m in _URL.finditer(str(panel.get('description') or '')):add(m.group(0))
    for m in _URL.finditer(str((user or {}).get('description') or '')):add(m.group(0))
    found.sort(key=lambda x:_ORDER.get(x['kind'],len(_ORDER)))
    return found


def _cache_path(root,login):
    return Path(root)/'state'/'twitch_links'/(login+'.json')


def fetch_links(root,login,*,ttl=TTL,refresh=False,timeout=20):
    """{'login','display','links':[...]} for a Twitch login; stale cache on network errors."""
    login=str(login or '').strip().lower()
    if not _LOGIN.fullmatch(login):return {'login':'','display':'','links':[]}
    path=_cache_path(root,login);cached=None
    try:cached=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    if cached and not refresh and time.time()-float(cached.get('fetched_at',0))<ttl:return cached
    try:
        s=requests.Session();s.trust_env=False
        r=s.post(GQL,json={'query':QUERY,'variables':{'l':login}},headers={'Client-ID':CLIENT_ID},timeout=timeout)
        r.raise_for_status()
        user=((r.json() or {}).get('data') or {}).get('user')
    except (requests.RequestException,ValueError):
        return cached or {'login':login,'display':login,'links':[]}
    if not user:
        return cached or {'login':login,'display':login,'links':[]}
    data={'login':login,'display':str(user.get('displayName') or login),'links':parse_links(user),'fetched_at':time.time()}
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=1),encoding='utf-8');tmp.replace(path)
    except OSError:pass
    return data


def genitive(name):
    """Very small Russian genitive for streamer nicknames: Никс→Никса, Каша→Каши, Лена→Лены."""
    name=str(name or '').strip()
    if not name or not re.search(r'[А-Яа-яЁё]$',name):return name
    low=name[-1].lower()
    if low=='а':return name[:-1]+('и' if name[-2:-1].lower() in 'гкхжчшщ' and len(name)>1 else 'ы')
    if low=='я':return name[:-1]+'и'
    if low=='й':return name[:-1]+'я'
    if low in 'бвгджзклмнпрстфхцчшщ':return name+'а'
    return name


def login_for(job=None,profile=None,cfg=None):
    """Twitch login whose links go into the description."""
    profile=str(profile or '').strip().lower()
    if profile and profile!='__test__' and _LOGIN.fullmatch(profile):return profile
    job=job or {}
    if job.get('local_name'):
        m=re.match(r'^\s*\[\d{1,4}[-.]\d{1,2}[-.]\d{2,4}\]\s*([A-Za-z0-9_]{1,25})\s+-\s+',str(job['local_name']))
        if m:return m.group(1).lower()
    else:
        s=str(job.get('streamer') or '').strip().lower()
        if _LOGIN.fullmatch(s) and s!='test':return s
    t=str((((cfg or {}).get('pipeline') or {}).get('preview') or {}).get('test_streamer') or '').strip().lower()
    return t if _LOGIN.fullmatch(t) else ''


def build(root,login,footer=None):
    """Full description text. Never raises: on any problem returns at least the footer."""
    footer=DEFAULT_FOOTER if footer is None else str(footer)
    parts=[]
    try:
        login=str(login or '').strip().lower()
        if _LOGIN.fullmatch(login):
            info=fetch_links(root,login)
            import lang
            if lang.is_en():
                name=info.get('display') or login
                lines=['🌐 '+name+"'s links",'🎮 Twitch — https://www.twitch.tv/'+login]
            else:
                name=youtube_titles.ALIASES.get(login) or info.get('display') or login
                lines=['🌐 Ссылки на '+genitive(name),'🎮 Twitch — https://www.twitch.tv/'+login]
            links=info.get('links') or []
            counts={}
            for x in links:counts[x['kind']]=counts.get(x['kind'],0)+1
            for x in links:
                label=x['label']
                if counts[x['kind']]>1 and x.get('title') and x['title'].lower()!=label.lower():label=f"{label} ({x['title']})"
                lines.append(f"{x['emoji']} {label} — {x['url']}")
            parts.append('\n'.join(lines))
    except Exception:
        parts=[]
    if footer.strip():parts.append(footer.strip('\n'))
    text='\n'.join(parts).replace('<','').replace('>','')
    return text[:LIMIT]
