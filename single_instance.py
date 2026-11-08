"""Prevent a second Windows GUI instance from recovering an active queue."""
import hashlib
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


def acquire(root):
    if os.name != 'nt':return None
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    name=os.path.normcase(str(Path(root).resolve())).encode('utf8')
    handle=kernel.CreateMutexW(None,False,'Local\\NarezkoBot-'+hashlib.sha256(name).hexdigest())
    if not handle:raise OSError('Не удалось проверить, не запущено ли приложение')
    if ctypes.get_last_error()==183:
        kernel.CloseHandle(handle)
        raise AlreadyRunning('Нарезкобот уже запущен. Используйте открытое окно.')
    return (kernel,handle)


def release(lock):
    if lock:lock[0].CloseHandle(lock[1])
