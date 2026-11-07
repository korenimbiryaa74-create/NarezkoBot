"""Restored proxy config adapter; no credentials are stored in this module.
Reads config.json -> proxy. TWLOADER_PROXY_URL overrides its URL and enables it.
TWLOADER_CONFIG_PATH selects a config file (useful for tests).
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def _settings():
    path = Path(os.environ.get('TWLOADER_CONFIG_PATH') or Path(__file__).with_name('config.json'))
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Не удалось прочитать config.json для прокси') from exc
    value = raw.get('proxy', {})
    if isinstance(value, str):
        return {'url': value}
    if not isinstance(value, dict):
        raise ValueError('config.json: proxy должен быть объектом или URL-строкой')
    return value


def parse_proxy(value):
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'socks5', 'socks5h', 'socks4') or not p.hostname or not p.port:
            raise ValueError
        if p.path not in ('', '/') or p.query or p.fragment:
            raise ValueError
        return p.scheme, p.hostname, p.port, unquote(p.username or ''), unquote(p.password or '')
    except (ValueError, TypeError) as exc:
        raise ValueError('Прокси: требуется http:// или socks5://HOST:PORT; логин/пароль URL-кодировать') from exc


def _url(cfg):
    env = os.environ.get('TWLOADER_PROXY_URL', '').strip()
    if env:
        parse_proxy(env)
        return env
    raw = str(cfg.get('url') or cfg.get('server') or '').strip()
    if not raw and cfg.get('host') and cfg.get('port'):
        scheme = str(cfg.get('type') or cfg.get('scheme') or 'socks5').lower()
        host = str(cfg['host'])
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        raw = f'{scheme}://{host}:{int(cfg["port"])}'
    if raw:
        parse_proxy(raw)
        # Optional credentials stored separately, as in Playwright-style configs.
        p = urlsplit(raw)
        user = cfg.get('username', cfg.get('login'))
        password = cfg.get('password')
        if p.username is None and user is not None:
            host = p.hostname
            if ':' in host:
                host = '[' + host + ']'
            raw = f'{p.scheme}://{quote(str(user), safe="")}:{quote(str(password or ""), safe="")}@{host}:{p.port}'
    return raw


def enabled():
    cfg = _settings()
    if not os.environ.get('TWLOADER_PROXY_URL', '').strip() and cfg.get('enabled') is False:
        return False
    url = _url(cfg)
    if cfg.get('enabled') is True and not url:
        raise ValueError('Прокси включён, но proxy.url не заполнен')
    return bool(url)


def proxy_url():
    return _url(_settings()) if enabled() else ''


def requests_proxies():
    url = proxy_url()
    if not url:
        return {}
    # SOCKS5 DNS on the proxy side, not on the local Windows machine.
    if url.startswith('socks5://'):
        url = 'socks5h://' + url[len('socks5://'):]
    return {'http': url, 'https': url}


def _for(name, default):
    if not enabled():
        return False
    cfg = _settings()
    value = cfg.get(name, cfg.get('for_' + name, default))
    if not isinstance(value, bool):
        raise ValueError('proxy.' + name + ' должен быть true/false')
    return value


def for_telegram():
    return _for('telegram', False)


def for_twitch_api():
    return _for('twitch_api', False)


def for_chatgpt():
    return _for('chatgpt', True)


def direct_env():
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith('_proxy')}
    env['NO_PROXY'] = env['no_proxy'] = '*'
    return env


def describe():
    url = proxy_url()
    if not url:
        return 'выключен'
    scheme, host, port, user, _ = parse_proxy(url)
    return f'{scheme}://{host}:{port}' + (' (с авторизацией)' if user else '')
