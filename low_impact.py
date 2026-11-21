"""Адаптивная нагрузка: «играю — бот тихий», «не играю — бот на полной скорости».

Фоновый поток каждые несколько секунд смотрит, запущена ли игра (по умолчанию Dota 2, dota2.exe).

* Игра запущена («тихий» режим): бот и все его дочерние процессы (ffmpeg, TwitchDownloader, Chrome)
  получают приоритет «ниже среднего» и только часть логических ядер (верхние — нижние берёт игра),
  рендер стартует с пресетом game_speed (eco), загрузки — с обычным числом потоков.
* Игры нет («полный» режим): обычный приоритет, все ядра, рендер с пресетом free_speed (turbo),
  загрузки VOD и чата — с увеличенным числом потоков.

Переключение применяется сразу и к уже запущенным процессам бота (приоритет и ядра). Пресет рендера и
число потоков загрузки выбираются в момент старта задачи.

Настройки: config.json → pipeline.low_impact
  mode: auto (по игре) | always (всегда тихо, как раньше) | off (всегда полная скорость)
  games: ["dota2.exe"]        cpu_share: 0.5       priority: below_normal | idle
  game_speed: eco             free_speed: turbo
  free_download_threads: 16   free_chat_threads: 8  game_bandwidth_kib: 0 (лимит КиБ/с на поток в игре, 0 — без лимита)
"""
from __future__ import annotations
import os
import subprocess
import threading
import time

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040
NORMAL_PRIORITY_CLASS = 0x00000020
DEFAULT = {'enabled': True, 'mode': 'auto', 'games': ['dota2.exe'], 'cpu_share': 0.5, 'priority': 'below_normal',
           'game_speed': 'eco', 'free_speed': 'turbo', 'free_download_threads': 16, 'free_chat_threads': 8,
           'game_bandwidth_kib': 0, 'poll_seconds': 5}
SPEEDS = ('eco', 'quality', 'fast', 'max', 'turbo')
GAME_TITLES = {'dota2.exe': 'Dota 2'}

_state = {'applied': None, 'gaming': False, 'game': '', 'thread': None, 'log': None, 'checked': 0.0}
_lock = threading.Lock()


def settings(cfg) -> dict:
    s = dict(DEFAULT)
    try:
        s.update((cfg or {}).get('pipeline', {}).get('low_impact') or {})
    except AttributeError:
        pass
    if not s.get('enabled', True):
        s['mode'] = 'off'
    if s.get('mode') not in ('auto', 'always', 'off'):
        s['mode'] = 'auto'
    s['games'] = [str(g).strip().lower() for g in (s.get('games') or []) if str(g).strip()] or list(DEFAULT['games'])
    return s


def allowed_cores(share: float) -> list[int]:
    """Логические ядра для бота в тихом режиме: верхние ядра, нижние (их первыми берут игры) остаются свободными."""
    total = os.cpu_count() or 4
    share = min(1.0, max(0.1, float(share or 0.5)))
    n = max(2, min(total, int(round(total * share))))
    return list(range(total - n, total))


# ---------- процессы Windows (без psutil) ----------

def _snapshot() -> list[tuple[int, int, str]]:
    """[(pid, ppid, exe в нижнем регистре)] всех процессов."""
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile.lower()))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def running_game(games, procs=None) -> str:
    """Имя exe запущенной игры из списка или ''."""
    names = {p[2] for p in (procs if procs is not None else _snapshot())}
    for g in games:
        if g in names:
            return g
    return ''


def _descendants(procs, root: int) -> list[int]:
    children = {}
    for pid, ppid, _ in procs:
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    out, stack, seen = [], [root], {root}
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen:
                seen.add(c); out.append(c); stack.append(c)
    return out


def _set_process(pid: int | None, prio: int, mask: int | None) -> bool:
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    if pid is None:
        h = ctypes.c_void_p(k32.GetCurrentProcess()); own = False
    else:
        h = k32.OpenProcess(0x0200 | 0x1000, False, int(pid))  # SET_INFORMATION | QUERY_LIMITED_INFORMATION
        if not h:
            return False
        h = ctypes.c_void_p(h); own = True
    try:
        ok = bool(k32.SetPriorityClass(h, prio))
        if mask:
            ok = bool(k32.SetProcessAffinityMask(h, ctypes.c_size_t(mask))) and ok
        return ok
    finally:
        if own:
            k32.CloseHandle(h)


def _system_mask() -> int:
    total = os.cpu_count() or 4
    return (1 << total) - 1


def _profile(s, gaming: bool) -> tuple[int, int]:
    if gaming:
        prio = IDLE_PRIORITY_CLASS if s.get('priority') == 'idle' else BELOW_NORMAL_PRIORITY_CLASS
        mask = 0
        for c in allowed_cores(s.get('cpu_share', 0.5)):
            mask |= 1 << c
        return prio, mask
    return NORMAL_PRIORITY_CLASS, _system_mask()


def _apply_profile(s, gaming: bool, procs=None) -> int:
    """Приоритет и ядра для самого бота и всех его дочерних процессов. Возвращает число процессов."""
    if os.name != 'nt':
        return 0
    prio, mask = _profile(s, gaming)
    n = 1 if _set_process(None, prio, mask) else 0
    procs = procs if procs is not None else _snapshot()
    for pid in _descendants(procs, os.getpid()):
        try:
            n += 1 if _set_process(pid, prio, mask) else 0
        except Exception:
            pass
    return n


# ---------- текущее состояние для остального кода ----------

def gaming() -> bool:
    """Тихий режим сейчас? (игра запущена или mode=always)."""
    s = _state['applied']
    if not s or s['mode'] == 'off':
        return False
    if s['mode'] == 'always':
        return True
    t = _state['thread']
    if t is None or not t.is_alive():  # watcher не запущен (отдельный запуск модуля) — проверяем сами
        if time.monotonic() - _state['checked'] > 3:
            _state['game'] = running_game(s['games']); _state['gaming'] = bool(_state['game'])
            _state['checked'] = time.monotonic()
    return bool(_state['gaming'])


def mode_text() -> str:
    s = _state['applied']
    if not s or s['mode'] == 'off':
        return 'полная скорость (адаптивный режим выключен)'
    if s['mode'] == 'always':
        return 'тихий режим (всегда)'
    if gaming():
        return f"тихий режим — запущена {GAME_TITLES.get(_state['game'], _state['game'])}"
    return 'полная скорость — игра не запущена'


def cpu_budget(cfg=None) -> int:
    """Сколько логических ядер может занять рендер (для числа потоков ffmpeg)."""
    total = os.cpu_count() or 4
    s = settings(cfg) if cfg is not None else _state['applied']
    if not s or s['mode'] == 'off':
        return total
    on = gaming() if cfg is None else (s['mode'] == 'always' or bool(running_game(s['games'])))
    return len(allowed_cores(s.get('cpu_share', 0.5))) if on else total


def creation_flags() -> int:
    """Флаги для дочерних процессов: без окна; в тихом режиме — с пониженным приоритетом."""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    s = _state['applied']
    if os.name == 'nt' and s and gaming():
        flags |= IDLE_PRIORITY_CLASS if s.get('priority') == 'idle' else BELOW_NORMAL_PRIORITY_CLASS
    return flags


def render_speed(configured: str | None) -> str:
    """Пресет рендера: 'auto' → eco в игре, turbo без игры; явный пресет из настроек — как есть."""
    configured = str(configured or 'auto')
    if configured in SPEEDS:
        return configured
    s = _state['applied'] or settings(None)
    speed = s.get('game_speed') if gaming() else s.get('free_speed')
    return speed if speed in SPEEDS else ('eco' if gaming() else 'turbo')


def download_threads(base: int) -> int:
    """Потоки TwitchDownloader для видео: в игре — из настроек, без игры — больше."""
    base = max(1, int(base or 4))
    s = _state['applied']
    if not s or gaming():
        return base
    return max(base, min(32, int(s.get('free_download_threads', 16) or 16)))


def chat_threads() -> int:
    s = _state['applied']
    if not s or gaming():
        return 4
    return max(4, min(16, int(s.get('free_chat_threads', 8) or 8)))


def download_extra_args() -> list[str]:
    """Лимит скорости на поток TwitchDownloader в игре (чтобы не рос пинг), если задан."""
    s = _state['applied']
    kib = int((s or {}).get('game_bandwidth_kib', 0) or 0)
    return ['--bandwidth', str(kib)] if s and kib > 0 and gaming() else []


# ---------- запуск ----------

def _watch():
    last = None; tick = 0
    while True:
        s = _state['applied']
        if not s or s['mode'] != 'auto':
            return
        try:
            procs = _snapshot()
            game = running_game(s['games'], procs)
            _state['game'] = game; _state['gaming'] = bool(game); _state['checked'] = time.monotonic()
            changed = _state['gaming'] != last
            # при смене режима — сразу; иначе раз в ~30 с, чтобы подхватить процессы, запущенные до смены
            if changed or tick % 6 == 0:
                n = _apply_profile(s, _state['gaming'], procs)
                if changed and _state['log']:
                    if _state['gaming']:
                        cores = len(allowed_cores(s.get('cpu_share', 0.5)))
                        _state['log'](f"Запущена {GAME_TITLES.get(game, game)} — тихий режим: приоритет ниже среднего, "
                                      f"ядер для бота {cores} из {os.cpu_count()}, рендер {s.get('game_speed')} (процессов: {n})")
                    elif last is not None:
                        _state['log'](f"Игра закрыта — полная скорость: все ядра, обычный приоритет, рендер {s.get('free_speed')}, "
                                      f"загрузка до {s.get('free_download_threads')} потоков (процессов: {n})")
                    else:
                        _state['log'](f"Адаптивный режим: игра не запущена — полная скорость (рендер {s.get('free_speed')}, "
                                      f"загрузка до {s.get('free_download_threads')} потоков); при запуске Dota 2 бот станет тихим")
                last = _state['gaming']
        except Exception as exc:  # наблюдатель не должен ронять бота
            if _state['log'] and tick % 120 == 0:
                _state['log'](f'Адаптивный режим: ошибка проверки ({type(exc).__name__})')
        tick += 1
        time.sleep(max(2, float(s.get('poll_seconds', 5) or 5)))


def apply(cfg=None, log=None) -> bool:
    """Включить режим для текущего процесса и его дочерних процессов; в mode=auto — следить за игрой."""
    s = settings(cfg)
    with _lock:
        _state['applied'] = s; _state['log'] = log
        if os.name != 'nt':
            return False
        try:
            if s['mode'] == 'off':
                _apply_profile(s, False)
                if log:
                    log('Адаптивный режим выключен: бот всегда работает на полной скорости')
                return True
            if s['mode'] == 'always':
                _state['gaming'] = True
                _apply_profile(s, True)
                if log:
                    log(f"Тихий режим (всегда): приоритет ниже среднего, ядер для бота {len(allowed_cores(s.get('cpu_share', 0.5)))} из {os.cpu_count()}")
                return True
            t = _state['thread']
            if t is None or not t.is_alive():
                t = threading.Thread(target=_watch, name='low-impact-watch', daemon=True)
                _state['thread'] = t
                t.start()
            return True
        except Exception as exc:  # режим не должен мешать запуску бота
            if log:
                log(f'Адаптивный режим не включён ({type(exc).__name__})')
            return False
