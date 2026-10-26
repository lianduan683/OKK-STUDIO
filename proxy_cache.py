"""Best-effort discovery of generated proxies moved to the cache drive.

An index belongs to one full source path, never a filename or directory guess.
Both files must still match the recorded path, byte size and nanosecond mtime.
Index failures only hide a proxy; they must never fail a completed video job.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from cache_paths import cache_dir


_lock=threading.RLock()
_directory_key=None
_directory=None
_records={}
_MAX_INDEX_BYTES=128*1024


def _normal_path(value):
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _stamp(value):
    path=_normal_path(value)
    file=Path(path)
    info=file.stat()
    if not stat.S_ISREG(info.st_mode):raise ValueError('代理索引只接受存在的普通文件')
    return {'path':path,'size':info.st_size,'mtime_ns':info.st_mtime_ns}


def _index_directory(*,retry=False):
    """Avoid cache_dir's write probes on frame-by-frame discovery calls."""
    global _directory_key,_directory
    key=(os.environ.get('OKK_CACHE_DIR',''),os.environ.get('SystemDrive','C:'))
    if key!=_directory_key or (retry and _directory is None):
        _directory_key=key;_directory=None;_records.clear()
        try:_directory=Path(cache_dir('proxies/index')).resolve()
        except (OSError,ValueError,RuntimeError):pass
    return _directory


def _key(source_path):
    return hashlib.sha256(source_path.encode('utf-8')).hexdigest()


def _metadata(path):
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>_MAX_INDEX_BYTES:raise ValueError('代理索引无效')
    return info.st_size,info.st_mtime_ns


def _valid_stamp(value):
    return (isinstance(value,dict) and set(value)=={'path','size','mtime_ns'}
            and isinstance(value['path'],str) and Path(value['path']).is_absolute()
            and type(value['size']) is int and value['size']>=0
            and type(value['mtime_ns']) is int and value['mtime_ns']>=0)


def find_proxy(source) -> str:
    """Return a bound, still-current proxy, or an empty string on any miss."""
    try:
        source_stamp=_stamp(source)
        with _lock:
            folder=_index_directory()
            if folder is None:return ''
            key=_key(source_stamp['path']);index=folder/(key+'.json')
            try:metadata=_metadata(index)
            except (OSError,ValueError):
                _records.pop(key,None);return ''
            cached=_records.get(key)
            if cached is not None and cached[0]==metadata:record=cached[1]
            else:
                try:
                    record=json.loads(index.read_text(encoding='utf-8'))
                    if (not isinstance(record,dict) or set(record)!={'version','source','proxy'}
                            or type(record['version']) is not int or record['version']!=1
                            or not _valid_stamp(record['source']) or not _valid_stamp(record['proxy'])):
                        record=None
                except (OSError,ValueError,UnicodeError):record=None
                _records[key]=(metadata,record)
            if record is None or record['source']!=source_stamp:return ''
            proxy=record['proxy']
            if proxy['path']==source_stamp['path'] or _stamp(proxy['path'])!=proxy:return ''
            return proxy['path']
    except (OSError,ValueError,TypeError,RuntimeError):return ''


def register_proxy(source,proxy) -> bool:
    """Atomically bind a completed proxy; an unavailable index is non-fatal."""
    temporary=None
    try:
        source_stamp=_stamp(source);proxy_stamp=_stamp(proxy)
        if source_stamp['path']==proxy_stamp['path']:return False
        record={'version':1,'source':source_stamp,'proxy':proxy_stamp}
        with _lock:
            folder=_index_directory(retry=True)
            if folder is None:return False
            # A deleted index directory can be restored without repeating
            # root/category write probes for every registered camera.
            if folder.resolve()!=folder:return False
            folder.mkdir(parents=True,exist_ok=True)
            key=_key(source_stamp['path']);index=folder/(key+'.json')
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix='.'+key+'.',
                    suffix='.tmp',dir=folder,delete=False) as stream:
                temporary=Path(stream.name)
                json.dump(record,stream,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,index);temporary=None
            _records[key]=(_metadata(index),record)
        return True
    except (OSError,ValueError,TypeError,RuntimeError):return False
    finally:
        if temporary is not None:
            try:temporary.unlink(missing_ok=True)
            except OSError:pass
