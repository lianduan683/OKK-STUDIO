"""Atomically deliver a completed cache file, including across disk volumes."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import time

from exporter_core import ExportCancelled


COPY_BLOCK_SIZE = 4 * 1024 * 1024


def publish_completed(source, destination, *, cancel_event=None, progress=None) -> str:
    """Publish a completed export without exposing a partial destination.

    Same-volume publication is one atomic replacement. Across volumes, copy
    only the completed file into a unique ``.pending`` sibling, then replace
    the destination. ``progress(copied_bytes, total_bytes)`` runs before that
    final commit and may cancel or fail without replacing the previous file.
    The caller owns cleanup of the completed source cache after a cross-volume
    copy. This helper must only receive generated output, never original media.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError('完成的导出缓存与目标文件不能是同一路径。')
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError('完成的导出文件不存在或为空。')

    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled('已取消写入成片，保留原有输出文件。')

    cancelled()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
        return str(destination)
    except OSError as error:
        if error.errno != errno.EXDEV and getattr(error, 'winerror', None) != 17:
            raise

    pending = None
    try:
        cancelled()
        total = source.stat().st_size
        descriptor, name = tempfile.mkstemp(prefix='.okk-delivery-', suffix='.pending', dir=destination.parent)
        pending = Path(name)
        with os.fdopen(descriptor, 'wb') as target, source.open('rb') as completed:
            copied = 0
            report_at = 0.
            if progress: progress(copied, total)
            while True:
                cancelled()
                block = completed.read(COPY_BLOCK_SIZE)
                if not block: break
                target.write(block)
                copied += len(block)
                now = time.monotonic()
                if progress and (copied == len(block) or copied == total or now >= report_at):
                    progress(copied, total)
                    report_at = now + .1
            cancelled()
            if copied != total:
                raise OSError('完成的导出缓存在交付期间发生变化。')
            target.flush()
            os.fsync(target.fileno())
        cancelled()
        os.replace(pending, destination)
        pending = None
        return str(destination)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
