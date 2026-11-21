"""Local checkpoints. No database, browser or network dependencies."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def stamp(path):
    st=Path(path).stat()
    return [st.st_size,st.st_mtime_ns]


def read(path):
    try: return json.loads(Path(path).read_text(encoding='utf8'))
    except (ValueError,OSError): return None


def atomic_bytes(path, data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.clip-')
    try:
        with os.fdopen(fd,'wb') as fh:
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp,path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def save(path,obj):
    atomic_bytes(path,json.dumps(obj,ensure_ascii=False,allow_nan=False).encode('utf8'))


def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,allow_nan=False).encode()).hexdigest()


def cached_fragment(path, identity, required, probe):
    m=read(path.with_suffix('.source.json'))
    try:
        if not isinstance(m,dict) or m.get('identity')!=identity or stamp(path)!=m.get('stamp'): return False
        actual=probe(path)
        return abs(actual-m['duration'])<=0.3 and actual+0.3>=required
    except (OSError,ValueError,TypeError,KeyError,RuntimeError): return False


def complete_bundle(journal,key,rendered,output,txt,meta,probe=None):
    """Replay only our own prepared commit. Never adopt or replace unknown user files."""
    m=read(journal)
    if not isinstance(m,dict) or m.get('key')!=key or m.get('output')!=output.name:
        raise RuntimeError('Журнал завершения не соответствует нарезке; файлы оставлены без изменений')
    video=output if output.exists() else rendered
    if not video.is_file() or stamp(video)!=m['stamp']:
        raise RuntimeError('Видео изменилось после проверки; восстановление остановлено')
    if probe and abs(probe(video)-m['meta']['duration'])>.5:
        raise RuntimeError('Длительность видео изменилась; восстановление остановлено')
    data=m['text'].encode('utf-8-sig')
    if txt.exists() and txt.read_bytes()!=data or meta.exists() and read(meta)!=m['meta']:
        raise RuntimeError('Найдены чужие или изменённые метаданные; не перезаписываю')
    if video==rendered: os.replace(rendered,output)
    if not txt.exists(): atomic_bytes(txt,data)
    if not meta.exists(): save(meta,m['meta'])
    journal.unlink()
