from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from exporter_core import MediaInfo


SUPPORTED_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".mkv", ".mxf", ".avi", ".webm", ".mts", ".m2ts",
})
SUPPORTED_AUDIO_EXTENSIONS = frozenset({
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aif", ".aiff", ".mka",
})
ALL_BIN_ID = "__all__"
UNGROUPED_BIN_ID = "__ungrouped__"


def media_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def is_supported_video(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def is_supported_audio(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS


def _collect_paths(paths: Iterable[str | os.PathLike[str]], accepts) -> list[str]:
    collected: dict[str, str] = {}
    for value in paths:
        path = Path(value).expanduser()
        candidates = (
            (item for item in path.rglob("*") if item.is_file())
            if path.is_dir()
            else (path,)
        )
        for candidate in candidates:
            if not candidate.is_file() or not accepts(candidate):
                continue
            resolved = str(candidate.resolve())
            collected.setdefault(media_key(resolved), resolved)
    return list(collected.values())


def collect_video_paths(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    return _collect_paths(paths, is_supported_video)


def collect_media_paths(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    return _collect_paths(paths, lambda path: is_supported_video(path) or is_supported_audio(path))


@dataclass(slots=True)
class MediaBin:
    id: str
    name: str


@dataclass(slots=True)
class PoolClip:
    path: str
    bin_id: str | None = None
    info: MediaInfo | None = None
    status: str = "pending"
    error: str = ""

    @property
    def key(self) -> str:
        return media_key(self.path)

    @property
    def name(self) -> str:
        return Path(self.path).name


@dataclass
class MediaPoolModel:
    clips: dict[str, PoolClip] = field(default_factory=dict)
    bins: dict[str, MediaBin] = field(default_factory=dict)

    def add_paths(self, paths: Iterable[str | os.PathLike[str]], bin_id: str | None = None) -> list[PoolClip]:
        target = self._validate_bin_id(bin_id)
        added: list[PoolClip] = []
        for path in collect_media_paths(paths):
            key = media_key(path)
            if key in self.clips:
                continue
            clip = PoolClip(path=path, bin_id=target)
            self.clips[key] = clip
            added.append(clip)
        return added

    def create_bin(self, name: str) -> MediaBin:
        clean_name = self._validate_bin_name(name)
        item = MediaBin(id=uuid.uuid4().hex, name=clean_name)
        self.bins[item.id] = item
        return item

    def rename_bin(self, bin_id: str, name: str) -> MediaBin:
        item = self._require_custom_bin(bin_id)
        clean_name = self._validate_bin_name(name, exclude_id=bin_id)
        item.name = clean_name
        return item

    def delete_bin(self, bin_id: str) -> None:
        self._require_custom_bin(bin_id)
        del self.bins[bin_id]
        for clip in self.clips.values():
            if clip.bin_id == bin_id:
                clip.bin_id = None

    def move_clips(self, keys: Iterable[str], bin_id: str | None) -> None:
        target = self._validate_bin_id(bin_id)
        for key in keys:
            if key in self.clips:
                self.clips[key].bin_id = target

    def remove_clips(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.clips.pop(key, None)

    def refresh_file_states(self) -> None:
        for clip in self.clips.values():
            if not Path(clip.path).is_file():
                clip.status = "offline"
                clip.error = "文件不存在或已断开连接"
            elif clip.status == "offline":
                clip.status = "pending"
                clip.error = ""

    def clips_for_bin(self, bin_id: str = ALL_BIN_ID) -> list[PoolClip]:
        if bin_id == ALL_BIN_ID:
            return list(self.clips.values())
        if bin_id == UNGROUPED_BIN_ID:
            return [clip for clip in self.clips.values() if clip.bin_id is None]
        self._require_custom_bin(bin_id)
        return [clip for clip in self.clips.values() if clip.bin_id == bin_id]

    def visible_clips(
        self,
        bin_id: str = ALL_BIN_ID,
        search: str = "",
        sort_key: str = "name",
        reverse: bool = False,
    ) -> list[PoolClip]:
        query = search.strip().casefold()
        clips = [
            clip for clip in self.clips_for_bin(bin_id)
            if not query or query in clip.path.casefold()
        ]
        clips.sort(key=lambda clip: self._sort_value(clip, sort_key), reverse=reverse)
        return clips

    def _sort_value(self, clip: PoolClip, sort_key: str):
        info = clip.info
        values = {
            "name": clip.name.casefold(),
            "duration": float(info.duration) if info else -1.0,
            "resolution": (int(info.width) * int(info.height)) if info else -1,
            "fps": float(info.fps) if info else -1.0,
            "codec": info.video_codec.casefold() if info else "",
            "depth": int(info.bit_depth) if info else -1,
            "audio": info.audio_codec.casefold() if info else "",
            "status": clip.status,
            "folder": str(Path(clip.path).parent).casefold(),
        }
        return values.get(sort_key, values["name"])

    def _validate_bin_id(self, bin_id: str | None) -> str | None:
        if bin_id in {None, UNGROUPED_BIN_ID}:
            return None
        if bin_id == ALL_BIN_ID:
            raise ValueError("不能移入“全部素材”；请选择具体媒体夹。")
        bin_id = str(bin_id)
        self._require_custom_bin(bin_id)
        return bin_id

    def _require_custom_bin(self, bin_id: str) -> MediaBin:
        if bin_id not in self.bins:
            raise ValueError("媒体夹不存在或已删除。")
        return self.bins[bin_id]

    def _validate_bin_name(self, name: str, exclude_id: str | None = None) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("媒体夹名称不能为空。")
        if any(
            item.id != exclude_id and item.name.casefold() == clean_name.casefold()
            for item in self.bins.values()
        ):
            raise ValueError("已有同名媒体夹。")
        return clean_name
