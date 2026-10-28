"""Conservative filename matching and cancellable validation of existing proxies."""
from __future__ import annotations

import os
from pathlib import Path
import re

from exporter_core import ExportCancelled
from proxy_media import validate_proxy_media
from quick_proxy import _probe_media_cancel

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.mxf', '.avi', '.mts', '.m2ts', '.webm'}
_SUFFIX = re.compile(r'(?:[ _.-](?:proxy|proxies|代理|1080p|720p|1080|720))$', re.I)


def identity(path):
    return os.path.normcase(os.path.abspath(path))


def proxy_stems(path):
    stem = Path(path).stem.casefold()
    names = {stem}
    while True:
        shorter = _SUFFIX.sub('', stem)
        if shorter == stem:
            return names
        names.add(shorter)
        stem = shorter


def match_proxy_folder(sources, folder, cancel_event=None):
    """Match unique source stems only; ambiguity is never resolved by guessing."""
    unique = {identity(path): str(path) for path in sources}
    by_stem = {}
    for path in unique.values():
        by_stem.setdefault(Path(path).stem.casefold(), []).append(path)
    candidates = {path: set() for path in unique.values()}
    scanned = 0
    for directory, subdirs, names in os.walk(folder, followlinks=False):
        subdirs[:] = [name for name in subdirs if not Path(directory, name).is_symlink()]
        for name in names:
            if cancel_event is not None and cancel_event.is_set():
                raise ExportCancelled('已取消链接代理。')
            scanned += 1
            if scanned > 50000:
                raise ValueError('目录包含超过 50000 个文件，请选择更具体的代理目录。')
            path = Path(directory, name)
            if path.suffix.lower() not in VIDEO_EXTENSIONS or identity(path) in unique:
                continue
            for stem in proxy_stems(path):
                for source in by_stem.get(stem, ()):
                    candidates[source].add(str(path))
    matched, issues = [], []
    for source, choices in candidates.items():
        if len(by_stem[Path(source).stem.casefold()]) > 1:
            issues.append((source, '多个原片同名，请在素材列表逐个选择并链接代理。'))
        elif len(choices) == 1:
            matched.append((source, next(iter(choices))))
        else:
            issues.append((source, '发现多个候选代理，请手动选择。' if choices else '未找到同名代理。'))
    # A name such as A_proxy can itself be an original. Never bind one candidate
    # to two different original files even if both suffix interpretations match.
    counts = {}
    for choices in candidates.values():
        for proxy in choices:
            counts[identity(proxy)] = counts.get(identity(proxy), 0) + 1
    safe = []
    for source, proxy in matched:
        if counts[identity(proxy)] == 1:
            safe.append((source, proxy))
        else:
            issues.append((source, '同一代理匹配了多个原片，请手动选择。'))
    return safe, issues


def validate_existing_proxy(ffprobe, source, proxy, cancel_event=None):
    """Check against the full original, independent of trims on the timeline."""
    probe = lambda binary, path: _probe_media_cancel(binary, path, cancel_event=cancel_event)
    original = probe(ffprobe, source)
    return validate_proxy_media(ffprobe, source, proxy, original.duration,
                                fps=original.fps, probe=probe)
