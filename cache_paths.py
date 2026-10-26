"""Writable cache locations on fixed non-system drives, selected on demand.

Importing this module does not create, migrate or delete any files. Environment
changes made by initialize_cache_policy affect this process and its children.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

_CACHE_NAME='OKK STUDIO Cache'
_KIND=re.compile(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z')
_RESERVED={'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}
_selected_root=None
_selection_key=None
_lock=threading.RLock()


def _system_drive():
    value=os.environ.get('SystemDrive','C:').strip().rstrip('\\/').upper()
    return value if re.fullmatch(r'[A-Z]:',value) else 'C:'


def _logical_fixed_drives():
    if os.name!='nt':raise ValueError('缓存策略需要 Windows 固定非系统磁盘，不能退回系统临时目录。')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetLogicalDrives.argtypes=[];kernel.GetLogicalDrives.restype=wintypes.DWORD
    kernel.GetDriveTypeW.argtypes=[wintypes.LPCWSTR];kernel.GetDriveTypeW.restype=wintypes.UINT
    mask=kernel.GetLogicalDrives()
    if not mask:raise ValueError('无法枚举本机固定磁盘，未使用系统盘缓存。')
    return [Path(f'{chr(65+index)}:/') for index in range(26)
            if mask&(1<<index) and kernel.GetDriveTypeW(f'{chr(65+index)}:\\')==3]


def _ensure_writable(path):
    probe=path/('.okk-write-probe-'+uuid.uuid4().hex+'.tmp')
    try:
        path.mkdir(parents=True,exist_ok=True)
        with probe.open('xb') as handle:handle.write(b'0')
    finally:
        if probe.exists():probe.unlink()


def _checked_location(value,drives):
    path=Path(value)
    if not path.is_absolute():raise ValueError('缓存根目录必须使用固定非系统盘上的绝对路径。')
    path=path.resolve()
    drive=path.drive.upper()
    if drive in {'C:',_system_drive()}:
        raise ValueError(f'缓存禁止使用系统盘或 C 盘：{path}')
    if drive not in {item.drive.upper() for item in drives}:
        raise ValueError(f'缓存目录必须位于本机固定非系统磁盘：{path}')
    return path


def cache_root() -> Path:
    """Prefer D:, then other fixed drives; never silently fall back to C:."""
    global _selected_root,_selection_key
    override=os.environ.get('OKK_CACHE_DIR','').strip()
    key=(override,_system_drive())
    with _lock:
        if key!=_selection_key:
            _selected_root=None;_selection_key=key
        if _selected_root is not None:
            try:
                resolved=_selected_root.resolve()
                if resolved!=_selected_root or resolved.drive.upper() in {'C:',_system_drive()}:
                    raise ValueError('缓存根目录位置已变化，请重新选择非系统盘缓存目录。')
                _ensure_writable(resolved)
            except (OSError,ValueError) as error:
                raise ValueError(f'已选缓存目录无法写入，未退回系统盘：{_selected_root}；{error}') from error
            return _selected_root
        drives=_logical_fixed_drives()
        if override:
            root=_checked_location(override,drives)
            try:_ensure_writable(root)
            except OSError as error:
                raise ValueError(f'指定缓存目录无法写入，未退回系统盘：{root}；{error}') from error
            _selected_root=root
            return root
        candidates=sorted((drive for drive in drives if drive.drive.upper() not in {'C:',_system_drive()}),
                          key=lambda drive:(drive.drive.upper()!='D:',drive.drive.upper()))
        failures=[]
        for drive in candidates:
            try:
                root=_checked_location(drive/_CACHE_NAME,drives)
                _ensure_writable(root)
            except (OSError,ValueError) as error:
                failures.append(f'{drive}：{error}');continue
            _selected_root=root
            return root
        details='；'.join(failures) or '没有可用的固定非系统磁盘'
        raise ValueError('找不到可写的非系统盘缓存目录，未使用 C 盘或系统临时目录。'+details)


def cache_dir(kind: str) -> Path:
    """Create a safe named cache category, e.g. stabilization/image."""
    if not isinstance(kind,str) or not _KIND.fullmatch(kind) or any(part.upper() in _RESERVED for part in kind.split('/')):
        raise ValueError('缓存分类必须是安全的相对名称，只能包含字母、数字、下划线、连字符和 /。')
    root=cache_root()
    path=root.joinpath(*kind.split('/')).resolve()
    if not path.is_relative_to(root):raise ValueError('缓存分类超出已选缓存根目录。')
    try:_ensure_writable(path)
    except OSError as error:raise ValueError(f'缓存目录无法写入：{path}；{error}') from error
    return path


def initialize_cache_policy() -> Path:
    """Route Python and inherited process temporary files to the chosen drive."""
    root=cache_root()
    temporary=cache_dir('temporary')
    tempfile.tempdir=str(temporary)
    os.environ['TMP']=str(temporary);os.environ['TEMP']=str(temporary)
    return root
