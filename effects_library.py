from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

LIBRARY_DIRNAME = Path("assets") / "effects_library" / "overlays"
TRANSITIONS_DIRNAME = Path("assets") / "effects_library" / "transitions"
MANIFEST_NAME = "library.json"
NO_OVERLAY_LABEL = "无（不叠加）"


@dataclass(frozen=True, slots=True)
class OverlayAsset:
    """One converted overlay asset shipped with the effects library."""

    id: str
    name: str
    kind: str          # "alpha" | "screen"
    category: str
    file: str          # file name inside the library directory
    width: int
    height: int
    fps: float
    frames: int
    duration: float
    source: str


def default_app_dir() -> Path:
    """Directory carrying the side-by-side ``assets`` tree.

    A frozen (PyInstaller) build unpacks ``__file__`` into a temporary
    extraction directory, while the effects library ships next to the
    executable to keep the one-file archive small.  An explicit ``app_dir``
    argument always wins over this default.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def library_dir(app_dir: str | os.PathLike[str] | None = None) -> Path:
    """Directory holding library.json and the converted assets."""
    root = Path(app_dir) if app_dir else default_app_dir()
    return root / LIBRARY_DIRNAME


def _disambiguate_names(assets: list[OverlayAsset]) -> None:
    seen: dict[str, int] = {}
    for asset in assets:
        seen[asset.name] = seen.get(asset.name, 0) + 1
    counts: dict[str, int] = {}
    for asset in assets:
        if seen[asset.name] > 1:
            counts[asset.name] = counts.get(asset.name, 0) + 1
            object.__setattr__(
                asset, "name", f"{asset.name}（{asset.id.split('_')[0]}）",
            )


def load_overlay_assets(app_dir: str | os.PathLike[str] | None = None) -> list[OverlayAsset]:
    """Read the manifest; entries whose file is missing are skipped."""
    manifest = library_dir(app_dir) / MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = payload.get("overlays") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    assets: list[OverlayAsset] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            asset = OverlayAsset(
                id=str(entry["id"]),
                name=str(entry["name"]),
                kind=str(entry["kind"]),
                category=str(entry.get("category") or "其他"),
                file=str(entry["file"]),
                width=int(entry.get("width") or 0),
                height=int(entry.get("height") or 0),
                fps=float(entry.get("fps") or 25.0),
                frames=int(entry.get("frames") or 0),
                duration=float(entry.get("duration") or 0.0),
                source=str(entry.get("source") or ""),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if asset.kind not in ("alpha", "screen"):
            continue
        if not (library_dir(app_dir) / asset.file).is_file():
            continue
        assets.append(asset)
    assets.sort(key=lambda item: (item.category, item.id))
    _disambiguate_names(assets)
    return assets


def asset_path(asset: OverlayAsset, app_dir: str | os.PathLike[str] | None = None) -> str:
    """Absolute path of one asset file, guaranteed to stay in the library."""
    base = library_dir(app_dir).resolve()
    path = (base / asset.file).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"特效库文件越界：{asset.file}")
    return str(path)


def asset_labels(assets: list[OverlayAsset]) -> list[str]:
    """User-facing combo values: 无 + one label per asset, grouped by category."""
    labels = [NO_OVERLAY_LABEL]
    current = None
    for asset in assets:
        prefix = "" if asset.category == current else f"{asset.category} · "
        current = asset.category
        labels.append(prefix + asset.name)
    return labels


def find_asset_by_name(assets: list[OverlayAsset], name: str) -> OverlayAsset | None:
    for asset in assets:
        if asset.name == name:
            return asset
    return None


@dataclass(frozen=True, slots=True)
class TransitionMask:
    """One grayscale wipe mask shipped with the transition library."""

    id: str
    name: str
    category: str
    file: str


def transitions_dir(app_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(app_dir) if app_dir else default_app_dir()
    return root / TRANSITIONS_DIRNAME


def load_transition_masks(app_dir: str | os.PathLike[str] | None = None) -> list[TransitionMask]:
    """Read the transition-mask manifest; missing files are skipped."""
    manifest = transitions_dir(app_dir) / MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = payload.get("transitions") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    masks: list[TransitionMask] = []
    seen_names: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            mask = TransitionMask(
                id=str(entry["id"]),
                name=str(entry["name"]),
                category=str(entry.get("category") or "其他"),
                file=str(entry["file"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not (transitions_dir(app_dir) / mask.file).is_file():
            continue
        seen_names[mask.name] = seen_names.get(mask.name, 0) + 1
        masks.append(mask)
    counts: dict[str, int] = {}
    for mask in masks:
        if seen_names[mask.name] > 1:
            counts[mask.name] = counts.get(mask.name, 0) + 1
            object.__setattr__(mask, "name", f"{mask.name}（{mask.id.split('_')[0]}）")
    masks.sort(key=lambda item: (item.category, item.id))
    return masks


def transition_mask_path(mask: TransitionMask, app_dir: str | os.PathLike[str] | None = None) -> str:
    """Absolute mask path, guaranteed to stay inside the transitions directory."""
    base = transitions_dir(app_dir).resolve()
    path = (base / mask.file).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"转场遮罩文件越界：{mask.file}")
    return str(path)
