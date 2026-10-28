"""RTX Studio 工程文件：像达芬奇 / Premiere 一样把整个会话存成 .rtxproj。

工程文件是一份人类可读的 JSON：媒体池、拼接时间线（含对齐与转场）、特效参数、
多机位机位、裁切 / 导出 / 代理 / 分离 / 无损分段各页设置。素材按“引用路径”保存
（不打包容器），重新打开时文件缺失的素材标记为离线并提示。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import tempfile
import uuid
from pathlib import Path

from editor_core import AlignmentResult
from effects_library import NO_OVERLAY_LABEL
from media_pool_core import MediaBin, PoolClip, media_key
from transitions_core import TransitionSpec

PROJECT_FORMAT = "rtx-studio-project"
PROJECT_SCHEMA_VERSION = 2
PROJECT_SUFFIX = ".rtxproj"
RECENT_LIMIT = 10
AUTOSAVE_INTERVAL_MS = 30000

# 每个 UI 分节里需要持久化的 tk 变量属性名；缺失的变量自动跳过，方便旧工程与测试桩。
_VAR_SECTIONS: dict[str, tuple[str, ...]] = {
    "export": (
        "input_var", "output_var", "codec_var", "preset_var", "quality_var",
        "resolution_var", "depth_var", "audio_var", "decode_var", "split_var",
        "parallel_var", "maxrate_var",
    ),
    "editor_single": (
        "editor_video_var", "editor_audio_var", "editor_output_var",
        "editor_trim_in_var", "editor_trim_out_var", "editor_trim_mode_var",
        "editor_trim_bitrate_var", "editor_trim_output_var",
    ),
    "editor_join": (
        "editor_join_output_var", "editor_join_mode_var", "editor_join_video_output_var",
        "editor_join_gap_var", "editor_join_bitrate_var", "editor_join_leading_mode_var",
        "editor_join_trailing_mode_var", "editor_join_range_start_var",
        "editor_join_range_end_var", "editor_join_audio_bitrate_var",
        "editor_join_audio_format_var", "editor_join_audio_sample_rate_var",
        "editor_join_manual_time_var", "editor_join_nudge_var",
        "editor_join_shared_settings_var",
    ),
    "editor_split": ("editor_split_mode_var", "editor_split_dir_var"),
    "editor_proxy": (
        "editor_proxy_dir_var", "editor_proxy_format_var", "editor_proxy_parallel_var",
    ),
    "effects": (
        "editor_effects_enabled_var", "editor_effects_brightness_var",
        "editor_effects_contrast_var", "editor_effects_saturation_var",
        "editor_effects_gamma_var", "editor_effects_rotation_var",
        "editor_effects_hflip_var", "editor_effects_vflip_var",
        "editor_effects_sharpen_var", "editor_effects_blur_var",
        "editor_effects_vignette_var", "editor_effects_fade_in_var",
        "editor_effects_fade_out_var", "editor_effects_overlay_var",
        "editor_effects_overlay_opacity_var", "editor_effects_output_var",
    ),
}

# 无损分段页：state 字段 ↔ tk 变量名（显示层由 vars 驱动，导出读 state）。
_LOSSLESS_FIELDS: tuple[tuple[str, str], ...] = (
    ("output_mode", "mode_var"),
    ("output_dir", "output_dir_var"),
    ("merged_output", "merged_output_var"),
    ("extension", "extension_var"),
    ("name_template", "template_var"),
    ("overwrite", "overwrite_var"),
    ("snap_start", "snap_var"),
    ("auto_chapters", "chapters_var"),
    ("auto_save", "auto_save_var"),
    ("metadata_title", "title_var"),
    ("metadata_language", "language_var"),
    ("metadata_tags", "meta_tags_var"),
    ("rotation", "rotation_var"),
    ("hvc1", "hvc1_var"),
)


class ProjectFileError(Exception):
    """工程文件损坏或来自不兼容版本。"""


def _require_mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ProjectFileError(f"工程文件中的{label}类型损坏。")
    return value


def _require_sequence(value, label: str) -> list:
    if not isinstance(value, list):
        raise ProjectFileError(f"工程文件中的{label}类型损坏。")
    return value


def _validate_items(items: list, label: str, *, require_path: bool = False) -> None:
    for item in items:
        _require_mapping(item, label)
        if require_path and not isinstance(item.get("path"), str):
            raise ProjectFileError(f"工程文件中的{label}路径损坏。")


def validate_project_state(state) -> None:
    """在清空当前会话前深度校验 state 容器类型。

    只校验结构，不检查素材是否仍然在线；任何损坏都应在改动 UI 之前
    以 ``ProjectFileError`` 的形式交给入口显示。
    """
    _require_mapping(state, "状态数据")
    from studio_state import validate_okk, validate_lossless_edits
    try:
        validate_okk(state.get('okk_multicam'))
        validate_lossless_edits(state.get('lossless') or {})
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ProjectFileError(f'工程剪辑数据损坏：{exc}') from exc
    for section in _VAR_SECTIONS:
        if section in state:
            values = _require_mapping(state[section], f"{section} 设置")
            for name, value in values.items():
                if not isinstance(name, str) or not isinstance(value, (str, int, float, bool)):
                    raise ProjectFileError(f"工程文件中的 {section} 设置值损坏。")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ProjectFileError(f"工程文件中的 {section} 设置不是有限数值。")
    if "media_pool" in state:
        pool = _require_mapping(state["media_pool"], "媒体池")
        if "bins" in pool:
            bins = _require_sequence(pool["bins"], "媒体池媒体夹")
            _validate_items(bins, "媒体池媒体夹")
        if "clips" in pool:
            clips = _require_sequence(pool["clips"], "媒体池素材")
            _validate_items(clips, "媒体池素材", require_path=True)
    if "join" in state:
        join = _require_mapping(state["join"], "拼接时间线")
        if "files" in join:
            files = _require_sequence(join["files"], "拼接片段")
            if any(not isinstance(path, str) for path in files):
                raise ProjectFileError("工程文件中的拼接片段路径损坏。")
        for key, label in (("alignments", "对齐结果"), ("transitions", "转场")):
            if key in join:
                mapping = _require_mapping(join[key], label)
                for value in mapping.values():
                    _require_mapping(value, label)
        if "overlay_assets" in join:
            overlays = _require_mapping(join["overlay_assets"], "片段叠加特效")
            if any(not isinstance(path, str) or not isinstance(name, str)
                   for path, name in overlays.items()):
                raise ProjectFileError("工程文件中的片段叠加特效数据损坏。")
        if "audio" in join and not isinstance(join["audio"], str):
            raise ProjectFileError("工程文件中的主音频绑定损坏。")
    if "multicam" in state:
        multicam = state["multicam"]
        if isinstance(multicam, dict):
            multicam = _require_sequence(multicam.get("sources", []), "多机位素材")
        else:
            multicam = _require_sequence(multicam, "多机位素材")
        _validate_items(multicam, "多机位素材", require_path=True)
    if "proxy" in state:
        proxy = _require_mapping(state["proxy"], "代理队列")
        files = _require_sequence(proxy.get("files", []), "代理队列")
        if any(not isinstance(path, str) for path in files):
            raise ProjectFileError("工程文件中的代理队列路径损坏。")
    if "lossless" in state:
        _require_mapping(state["lossless"], "无损分段设置")


def projects_dir(app_dir: str | os.PathLike[str]) -> Path:
    folder = Path(app_dir) / "projects"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def autosave_path(app_dir: str | os.PathLike[str]) -> Path:
    return projects_dir(app_dir) / "autosave.rtxproj"


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def collect_state(app) -> dict:
    """把当前会话状态收集成可 JSON 化的 dict（对缺失控件自动降级）。"""
    try:
        _call(getattr(app, "lossless_workspace", None), "_sync_options_from_vars")
    except Exception:
        pass
    state: dict[str, dict] = {}
    for section, names in _VAR_SECTIONS.items():
        values = {}
        for name in names:
            variable = getattr(app, name, None)
            if variable is None:
                continue
            try:
                values[name] = variable.get()
            except Exception:
                continue
        state[section] = values
    state["media_pool"] = _collect_media_pool(app)
    state["join"] = _collect_join(app)
    state["multicam"] = _collect_multicam(app)
    state["proxy"] = _collect_proxy(app)
    state["lossless"] = _collect_lossless(app)
    from studio_state import collect_okk
    okk = collect_okk(app)
    if okk is not None:
        state['okk_multicam'] = okk
    return state


def _collect_media_pool(app) -> dict:
    pool = getattr(app, "media_pool_workspace", None)
    model = getattr(pool, "model", None)
    if model is None:
        return {"bins": [], "clips": []}
    return {
        "bins": [{"id": item.id, "name": item.name} for item in model.bins.values()],
        "clips": [
            {"path": clip.path, "bin": clip.bin_id or ""}
            for clip in model.clips.values()
        ],
    }


def _collect_join(app) -> dict:
    alignments = {}
    for path, result in (getattr(app, "editor_join_alignments", None) or {}).items():
        alignments[str(path)] = {
            "offset_seconds": float(result.offset_seconds),
            "confidence": float(result.confidence),
            "envelope_rate": int(result.envelope_rate),
            "engine": str(result.engine),
        }
    transitions = {}
    for index, spec in (getattr(app, "editor_timeline_transitions", None) or {}).items():
        transitions[str(int(index))] = {
            "kind": spec.kind,
            "label": spec.label,
            "duration": float(spec.duration),
            "xfade_mode": spec.xfade_mode,
            "mask_file": spec.mask_file,
            "reversed": bool(spec.reversed),
        }
    overlays = getattr(app, "editor_timeline_overlays", None)
    if not isinstance(overlays, dict):
        overlays = {}
    return {
        "files": [str(path) for path in (getattr(app, "editor_join_files", None) or [])],
        "alignments": alignments,
        "transitions": transitions,
        "overlay_assets": {path: name for path, name in overlays.items()
                           if isinstance(path, str) and isinstance(name, str)},
        "audio": str(getattr(app, "editor_join_alignment_audio", "") or ""),
    }


def _collect_multicam(app) -> dict:
    """保存多机位素材、布局、选中机位与播放时间。

    多机位面板隐藏/重挂载时真正的素材列表在 ``multicam_snapshot`` 里，
    因此快照优先于已关闭的视图。
    """
    snapshot = getattr(app, "multicam_snapshot", None)
    view = getattr(app, "multicam_view", None)
    if snapshot:
        sources = list(snapshot.get("sources") or [])
        meta = snapshot
    elif view is not None and not getattr(view, "closed", False):
        sources = list(getattr(view, "sources", None) or [])
        meta = {}
    else:
        sources = []
        meta = {}

    def value(name: str, default, view_name: str | None = None):
        if name in meta:
            return meta.get(name)
        attribute = getattr(view, view_name or name, None) if view is not None else None
        if hasattr(attribute, "get"):
            try:
                return attribute.get()
            except Exception:
                return default
        return attribute if attribute is not None else default

    def source_path(item) -> str:
        if isinstance(item, dict):
            return str(item.get("path") or "")
        return str(getattr(item, "path", "") or "")

    def source_offset(item) -> float:
        if isinstance(item, dict):
            return float(item.get("offset", 0.0) or 0.0)
        return float(getattr(item, "offset", 0.0) or 0.0)

    return {
        "sources": [
            {"path": source_path(item), "offset": source_offset(item)}
            for item in sources
        ],
        "rows": str(value("rows", "1", "rows_var")),
        "columns": str(value("columns", "1", "columns_var")),
        "active_index": int(value("active_index", 0) or 0),
        "current_time": float(value("current_time", 0.0) or 0.0),
    }


def _collect_proxy(app) -> dict:
    return {
        "files": [str(path) for path in (getattr(app, "editor_proxy_files", None) or [])],
    }


def _collect_lossless(app) -> dict:
    workspace = getattr(app, "lossless_workspace", None)
    state = getattr(workspace, "state", None)
    if state is None:
        return {}
    data = {"source": str(getattr(state, "source", "") or "")}
    for field, _var in _LOSSLESS_FIELDS:
        data[field] = getattr(state, field, None)
    if hasattr(state, 'model'):
        from studio_state import collect_lossless_edits
        data.update(collect_lossless_edits(workspace))
    return data


def apply_state(app, data: dict) -> list[str]:
    """把工程数据灌回应用；返回需要向用户提示的警告列表（离线素材等）。"""
    if not isinstance(data, dict):
        raise ProjectFileError("工程文件缺少状态数据。")
    state = data.get("state")
    validate_project_state(state)
    warnings: list[str] = []
    _reset_dynamic_state(app)

    for section, names in _VAR_SECTIONS.items():
        values = state.get(section)
        if not isinstance(values, dict):
            continue
        for name in names:
            if name in values:
                _set_var(app, name, values[name])

    warnings += _apply_media_pool(app, state.get("media_pool") or {})
    warnings += _apply_join(app, state.get("join") or {})
    warnings += _apply_multicam(app, state.get("multicam") or [])
    warnings += _apply_proxy(app, state.get("proxy") or {})
    warnings += _apply_effects(app)
    warnings += _apply_lossless(app, state.get("lossless") or {})
    warnings += _apply_export_input(app, state)
    from studio_state import restore_okk
    warnings += restore_okk(app, state.get('okk_multicam'), state.get('multicam'))
    return warnings


def _reset_dynamic_state(app) -> None:
    # 先让播放器代次失效并停掉旧引擎，避免异步加载结果写入新工程。
    invalidate = getattr(app, "_invalidate_editor_player", None)
    if callable(invalidate):
        try:
            invalidate()
        except Exception:
            pass
    else:
        controller = getattr(app, "mpv", None)
        if controller is not None:
            try:
                controller.stop()
            except Exception:
                pass
        app.mpv = None

    for name in (
        "editor_join_files", "editor_join_alignments", "editor_timeline_transitions",
        "editor_timeline_cut_zones", "editor_timeline_transition_assets",
        "editor_join_media_cache",
    ):
        container = getattr(app, name, None)
        if isinstance(container, (list, dict)):
            container.clear()
    app.editor_join_alignment_audio = ""
    app.editor_timeline_overlays = {}
    app.editor_join_alignment_token = None
    app.editor_join_audio_duration = 0.0
    app.editor_join_timeline_probe_key = None
    app.editor_join_timeline_model = None
    timeline_items = getattr(app, "editor_join_timeline_items", None)
    if isinstance(timeline_items, dict):
        timeline_items.clear()
    app.editor_join_preview_context = None
    app.editor_media = None
    app.editor_duration = 0.0
    app.editor_proxy_files = []
    if hasattr(app, "editor_proxy_progress_state"):
        try:
            app.editor_proxy_progress_state.clear()
        except Exception:
            app.editor_proxy_progress_state = {}
    _call(app, "_refresh_proxy_list")

    pool = getattr(app, "media_pool_workspace", None)
    model = getattr(pool, "model", None)
    if model is not None:
        model.clips.clear()
        model.bins.clear()
        _call(pool, "refresh")
    view = getattr(app, "multicam_view", None)
    if view is not None:
        _call(view, "clear")
    app.multicam_snapshot = None
    app.single_video_path = ""
    app.single_resume_time = 0.0
    app.single_resume_paused = True
    app.editor_current_time = 0.0


def _set_var(app, name: str, value) -> None:
    variable = getattr(app, name, None)
    if variable is None:
        return
    try:
        variable.set(value)
    except Exception:
        try:
            variable.set(str(value))
        except Exception:
            pass


def _call(target, name: str, *args):
    handler = getattr(target, name, None)
    if callable(handler):
        return handler(*args)
    return None


def _apply_media_pool(app, pool_state: dict) -> list[str]:
    pool = getattr(app, "media_pool_workspace", None)
    model = getattr(pool, "model", None)
    if pool is None or model is None:
        return []
    warnings: list[str] = []
    id_map: dict[str, str] = {}
    for item in pool_state.get("bins") or []:
        old_id = str(item.get("id") or "")
        name = str(item.get("name") or "").strip()
        if not name or old_id in model.bins:
            continue
        if any(existing.name == name for existing in model.bins.values()):
            continue
        # 保留工程文件里的原 id，恢复后的工程与保存内容逐字节一致。
        bin_id = old_id or uuid.uuid4().hex
        model.bins[bin_id] = MediaBin(id=bin_id, name=name)
        id_map[old_id] = bin_id

    online: dict[str | None, list[str]] = {}
    offline: list[tuple[str, str | None]] = []
    for item in pool_state.get("clips") or []:
        path = str(item.get("path") or "")
        if not path:
            continue
        bin_id = id_map.get(str(item.get("bin") or "")) or None
        if Path(path).is_file():
            online.setdefault(bin_id, []).append(path)
        else:
            offline.append((path, bin_id))
    for bin_id, paths in online.items():
        _call(pool, "add_paths", paths, bin_id)
    for path, bin_id in offline:
        model.clips[media_key(path)] = PoolClip(path=path, bin_id=bin_id)
        model.clips[media_key(path)].status = "offline"
        model.clips[media_key(path)].error = "工程已恢复，但素材文件不存在或已断开连接"
    _call(pool, "refresh")
    if offline:
        warnings.append(f"媒体池有 {len(offline)} 个素材离线（文件不存在或已移动）。")
    return warnings


def _apply_join(app, join_state: dict) -> list[str]:
    app.editor_join_files.extend(str(path) for path in join_state.get("files") or [])
    app.editor_join_alignment_audio = str(join_state.get("audio") or "")
    for path, item in (join_state.get("alignments") or {}).items():
        try:
            app.editor_join_alignments[str(path)] = AlignmentResult(
                offset_seconds=float(item.get("offset_seconds", 0.0) or 0.0),
                confidence=float(item.get("confidence", 0.0) or 0.0),
                envelope_rate=int(item.get("envelope_rate", 1000) or 1000),
                engine=str(item.get("engine") or ""),
            )
        except (TypeError, ValueError):
            continue
    for key, item in (join_state.get("transitions") or {}).items():
        try:
            app.editor_timeline_transitions[int(key)] = TransitionSpec(
                kind=str(item.get("kind") or "xfade"),
                label=str(item.get("label") or ""),
                duration=float(item.get("duration", 0.8) or 0.8),
                xfade_mode=str(item.get("xfade_mode") or "fade"),
                mask_file=str(item.get("mask_file") or ""),
                reversed=bool(item.get("reversed", False)),
            )
        except (TypeError, ValueError):
            continue
    app.editor_timeline_overlays = dict(join_state.get("overlay_assets", {}))
    assets = getattr(app, "editor_effects_assets", None) or []
    labels = {getattr(asset, "name", None) for asset in assets}
    unavailable = sorted({name for name in app.editor_timeline_overlays.values()
                          if name and name not in labels})
    warnings = [f"片段叠加素材“{name}”不在本机特效库中，已保留名称。"
                for name in unavailable]
    _call(app, "_refresh_join_list")
    missing = [path for path in app.editor_join_files if not Path(path).is_file()]
    if missing:
        warnings_text = f"拼接时间线有 {len(missing)} 个片段离线，导出前需要重新链接。"
        warnings.append(warnings_text)
    return warnings


def _apply_multicam(app, items) -> list[str]:
    view = getattr(app, "multicam_view", None)
    if view is None:
        return []
    if isinstance(items, dict):
        sources_state = items.get("sources") or []
        layout_state = items
    else:
        sources_state = items or []
        layout_state = {}
    if not sources_state:
        return []
    warnings: list[str] = []
    paths = []
    normalized: list[tuple[str, float]] = []
    for item in sources_state:
        path = str(item.get("path") or "")
        if not path:
            continue
        paths.append(path)
        try:
            offset = float(item.get("offset", 0.0) or 0.0)
        except (TypeError, ValueError):
            offset = 0.0
        normalized.append((path, offset))
        if not Path(path).is_file():
            warnings.append(f"多机位素材离线:{Path(path).name}")
    try:
        _call(view, "add_sources", paths)
    except Exception as exc:
        return [f"多机位恢复失败:{exc}"]
    # 按实际加入视图的 source 路径匹配偏移，避免离线/重复素材导致错位。
    by_path = {
        os.path.normcase(str(Path(source.path).resolve())): source
        for source in (getattr(view, "sources", None) or [])
    }
    for path, offset in normalized:
        source = by_path.get(os.path.normcase(str(Path(path).resolve())))
        if source is not None:
            source.offset = offset

    def set_var(name: str, value) -> None:
        variable = getattr(view, name, None)
        if hasattr(variable, "set"):
            try:
                variable.set(str(value))
            except Exception:
                pass

    if layout_state:
        if layout_state.get("rows"):
            set_var("rows_var", layout_state["rows"])
        if layout_state.get("columns"):
            set_var("columns_var", layout_state["columns"])
    if layout_state:
        _call(view, "_rebuild_grid")
        try:
            active_index = int(layout_state.get("active_index", 0) or 0)
        except (TypeError, ValueError):
            active_index = 0
        try:
            current_time = float(layout_state.get("current_time", 0.0) or 0.0)
        except (TypeError, ValueError):
            current_time = 0.0
        if getattr(view, "sources", None):
            active_index = min(max(0, active_index), len(view.sources) - 1)
            select = getattr(view, "select", None)
            if callable(select):
                try:
                    select(active_index)
                except Exception:
                    try:
                        view.active_index = active_index
                    except Exception:
                        pass
            else:
                try:
                    view.active_index = active_index
                except Exception:
                    pass
            if current_time > 0:
                seek = getattr(view, "seek", None)
                if callable(seek):
                    try:
                        seek(current_time)
                    except Exception:
                        try:
                            view.current_time = current_time
                        except Exception:
                            pass
                else:
                    try:
                        view.current_time = current_time
                    except Exception:
                        pass
    return warnings


def _apply_proxy(app, proxy_state: dict) -> list[str]:
    files = [str(path) for path in proxy_state.get("files") or [] if path]
    if not files:
        return []
    queue = getattr(app, "editor_proxy_files", None)
    if isinstance(queue, list):
        queue.extend(path for path in files if path not in queue)
    _call(app, "_refresh_proxy_list")
    return []


def _apply_effects(app) -> list[str]:
    warnings: list[str] = []
    variable = getattr(app, "editor_effects_overlay_var", None)
    assets = getattr(app, "editor_effects_assets", None) or []
    labels = {asset.name for asset in assets}
    if variable is not None:
        label = str(variable.get() or "")
        if label and label != NO_OVERLAY_LABEL:
            name = label.split(" · ", 1)[1] if " · " in label else label
            if name not in labels:
                _set_var(app, "editor_effects_overlay_var", NO_OVERLAY_LABEL)
                warnings.append(f"叠加素材“{name}”不在本机特效库中，已重置为不叠加。")
    settings = _call(app, "_collect_effect_settings")
    if settings is not None:
        try:
            from effects_core import effects_summary

            _set_var(app, "editor_effects_summary_var", effects_summary(settings))
        except Exception:
            pass
    return warnings


def _apply_lossless(app, lossless_state: dict) -> list[str]:
    workspace = getattr(app, "lossless_workspace", None)
    state = getattr(workspace, "state", None)
    if workspace is None or state is None:
        return []
    # Every project switch must invalidate the previous media/model, including
    # an empty source in a nonempty settings dict and an offline target source.
    # Otherwise old cuts can be auto-saved under the new source's LLC path.
    if callable(getattr(workspace, "reset_session", None)):
        _call(workspace, "reset_session")
        state = workspace.state
    else:
        state.source = ""
        state.probe = None
        state.keyframes = None
        _set_var(workspace, "source_var", "尚未打开素材")
    if not lossless_state:
        return []

    warnings: list[str] = []
    source = str(lossless_state.get("source") or "")

    def apply_options() -> None:
        for field, var_name in _LOSSLESS_FIELDS:
            if field not in lossless_state:
                continue
            value = lossless_state[field]
            if field == "extension":
                value = str(value or ".mkv")
            elif field in {"overwrite", "snap_start", "auto_chapters", "auto_save", "hvc1"}:
                value = bool(value)
            elif field == "output_mode":
                value = str(value or "separate")
            elif field == "rotation":
                value = str(value or "不修改")
            else:
                value = str(value or "")
            setattr(state, field, value)
            _set_var(workspace, var_name, value)

    state.source = source
    _set_var(workspace, "source_var", source or "尚未打开素材")
    # Restore edits before starting the probe. A preserved probe merges media
    # metadata while retaining saved deselected tracks and external tracks.
    from studio_state import restore_lossless_edits
    restore_lossless_edits(workspace, lossless_state)
    if source and Path(source).is_file():
        try:
            workspace.open_media(source, preserve='segments' in lossless_state)
        except Exception as exc:
            warnings.append(f"无损分段素材重新读取失败:{exc}")
        # open_media 会重置输出目录等选项，保存值必须在调用之后写回。
        apply_options()
        _set_var(workspace, "source_var", source)
    elif source:
        apply_options()
        warnings.append(f"无损分段素材离线:{Path(source).name}")
    else:
        apply_options()
    return warnings


def _apply_export_input(app, state: dict) -> list[str]:
    export = state.get("export") or {}
    path = str(export.get("input_var") or "")
    if not path:
        return []
    output = str(export.get("output_var") or "")
    if path and Path(path).is_file():
        _call(app, "_set_input_video", path)
        if output:
            _set_var(app, "output_var", output)
        return []
    _set_var(app, "input_var", path)
    _set_var(app, "media_var", "工程已恢复，但输入文件不存在或已移动。")
    return [f"导出输入离线:{Path(path).name}"]


def build_project_payload(app, *, project_path: str = "", app_version: str = "") -> dict:
    return {
        "format": PROJECT_FORMAT,
        "schema": PROJECT_SCHEMA_VERSION,
        "saved_at": _now_text(),
        "app_version": app_version,
        "project_path": project_path,
        "state": collect_state(app),
    }


def state_digest(state: dict) -> str:
    payload = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_project_file(path: str | os.PathLike[str], payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                prefix='.' + target.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_project_file(path: str | os.PathLike[str]) -> dict:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProjectFileError("工程文件不存在。") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectFileError(f"工程文件无法读取:{exc}") from None
    if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
        raise ProjectFileError("这不是有效的 RTX 工程文件。")
    schema = data.get("schema", 0)
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        raise ProjectFileError("工程文件版本信息损坏。") from None
    if schema > PROJECT_SCHEMA_VERSION:
        raise ProjectFileError(
            "该工程由更新版本的软件保存，请先升级软件再打开。"
        )
    if not isinstance(data.get("state"), dict):
        raise ProjectFileError("工程文件缺少状态数据。")
    validate_project_state(data["state"])
    return data


def describe_project(data: dict) -> str:
    """用于标题栏 / 最近菜单 / 恢复提示的一句话摘要。"""
    state = data.get("state") or {}
    pool_clips = len((state.get("media_pool") or {}).get("clips") or [])
    join_files = len((state.get("join") or {}).get("files") or [])
    multicam_state = state.get("multicam") or []
    if isinstance(multicam_state, dict):
        multicams = len(multicam_state.get("sources") or [])
    else:
        multicams = len(multicam_state)
    parts = []
    if pool_clips:
        parts.append(f"媒体池 {pool_clips} 素材")
    if join_files:
        parts.append(f"时间线 {join_files} 段")
    if multicams:
        parts.append(f"多机位 {multicams} 路")
    return " · ".join(parts) if parts else "空工程"


def _recent_file(app_dir: str | os.PathLike[str]) -> Path:
    return projects_dir(app_dir) / "recent.json"


def load_recent_projects(app_dir: str | os.PathLike[str]) -> list[dict]:
    try:
        data = json.loads((Path(app_dir) / "projects" / "recent.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    items = []
    for item in data:
        if isinstance(item, dict) and item.get("path"):
            items.append({
                "path": str(item["path"]),
                "name": str(item.get("name") or Path(str(item["path"])).stem),
                "saved_at": str(item.get("saved_at") or ""),
            })
    return items[:RECENT_LIMIT]


def record_recent_project(app_dir: str | os.PathLike[str], path: str | os.PathLike[str]) -> None:
    target = Path(path)
    # 去掉同路径旧记录后置顶插入。
    items = [
        item for item in load_recent_projects(app_dir)
        if not _same_path(item["path"], target)
    ]
    items.insert(0, {
        "path": str(target),
        "name": target.stem,
        "saved_at": _now_text(),
    })
    try:
        _recent_file(app_dir).write_text(
            json.dumps(items[:RECENT_LIMIT], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _same_path(left: str, right: Path) -> bool:
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def new_bin_id() -> str:
    return uuid.uuid4().hex
