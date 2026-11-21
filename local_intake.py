"""Read a local input only while Windows grants a handle that excludes writers."""
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

EXTENSIONS={'.mp4','.mkv','.mov','.webm','.m4v','.avi'}


@contextmanager
def ready_reader(path):
    if os.name!='nt':
        with open(path,'rb') as f: yield f
        return
    import ctypes, msvcrt
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    op=kernel.CreateFileW; op.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]; op.restype=w.HANDLE
    h=op(str(path),0x80000000,1,None,3,0x80,None)  # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING
    if h==ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
    try: fd=msvcrt.open_osfhandle(h,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(w.HANDLE(h)); raise
    with os.fdopen(fd,'rb') as f: yield f


def import_file(src,folder):
    src=Path(src); folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    if src.is_symlink() or src.suffix.lower() not in EXTENSIONS: raise ValueError('Неподдерживаемый файл')
    dest=folder/src.name
    if dest.exists(): raise FileExistsError(f'Файл {src.name} уже есть в test_input')
    fd,tmp=tempfile.mkstemp(dir=folder,suffix='.partial')
    try:
        with os.fdopen(fd,'wb') as out, ready_reader(src) as inp:
            before=os.fstat(inp.fileno()); shutil.copyfileobj(inp,out,1024*1024)
            after=os.fstat(inp.fileno()); out.flush(); os.fsync(out.fileno())
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or out.tell()!=before.st_size or not out.tell():
                raise ValueError('Исходник пуст или изменился при копировании')
        if os.name=='nt': os.rename(tmp,dest)  # Windows refuses to replace an existing file
        else: os.link(tmp,dest)
    finally: Path(tmp).unlink(missing_ok=True)
    return dest
