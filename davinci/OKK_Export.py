from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "OKK GROUP · 达芬奇 RTX 导出"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "Timeline")
    return cleaned.strip(" .") or "Timeline"


def _find_mapping_value(mapping: dict, preferred_labels: tuple[str, ...]) -> str | None:
    items = [(str(label), str(value)) for label, value in (mapping or {}).items()]
    for preferred in preferred_labels:
        token = preferred.casefold()
        for label, value in items:
            if token in label.casefold() or token == value.casefold():
                return value
    return None


def _resolve_object():
    existing = globals().get("resolve")
    if existing is not None:
        return existing
    fusion_object = globals().get("fusion")
    if fusion_object is not None and hasattr(fusion_object, "GetResolve"):
        return fusion_object.GetResolve()
    bmd_object = globals().get("bmd")
    if bmd_object is not None and hasattr(bmd_object, "scriptapp"):
        return bmd_object.scriptapp("Resolve")

    modules = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / (
        r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"
    )
    if modules.is_dir():
        sys.path.insert(0, str(modules))
    try:
        import DaVinciResolveScript as dvr_script
    except ImportError as exc:
        raise RuntimeError("无法载入 DaVinci Resolve 脚本接口。请在达芬奇内通过“工作区 → 脚本”运行。") from exc
    result = dvr_script.scriptapp("Resolve")
    if result is None:
        raise RuntimeError("没有连接到 DaVinci Resolve。")
    return result


def _read_app_root() -> Path:
    config = Path(__file__).resolve().with_name("okk_app_path.txt")
    if not config.is_file():
        raise RuntimeError("缺少 okk_app_path.txt，请重新运行“一键安装达芬奇连接器.bat”。")
    root = Path(config.read_text(encoding="utf-8-sig").strip()).expanduser().resolve()
    if not (root / "rtx5090_exporter.py").is_file() and not (root / "RTX_Video_Render_Studio.exe").is_file():
        raise RuntimeError(f"找不到 OKK 软件本体：{root}\n请把软件移动到固定位置后重新安装连接器。")
    return root


def _launcher_command(app_root: Path, arguments: list[str]) -> list[str]:
    executable = app_root / "RTX_Video_Render_Studio.exe"
    if executable.is_file():
        return [str(executable), *arguments]
    script = app_root / "rtx5090_exporter.py"
    py_launcher = shutil.which("py")
    if py_launcher:
        return [py_launcher, "-3", str(script), *arguments]
    python = shutil.which("python")
    if python:
        return [python, str(script), *arguments]
    raise RuntimeError("未找到 Windows Python 3。请安装 Python 3.11 或更新版本，或先生成软件 EXE。")


class ExportCancelled(RuntimeError):
    pass


def _configure_intermediate(project) -> tuple[str, str]:
    formats = project.GetRenderFormats() or {}
    format_code = _find_mapping_value(formats, ("QuickTime", "mov"))
    if not format_code:
        raise RuntimeError("当前达芬奇没有提供 QuickTime/MOV 渲染格式。")
    codecs = project.GetRenderCodecs(format_code) or {}
    codec_code = _find_mapping_value(
        codecs,
        ("DNxHR HQX 12-bit", "DNxHR HQX 10-bit", "DNxHR HQX", "DNxHR HQ", "DNxHR"),
    )
    if not codec_code:
        names = "、".join(str(name) for name in codecs) or "无"
        raise RuntimeError(f"当前达芬奇未找到 DNxHR 编码器。可用编码器：{names}")
    if not project.SetCurrentRenderFormatAndCodec(format_code, codec_code):
        raise RuntimeError("达芬奇拒绝切换到 QuickTime/DNxHR 中间母版格式。")
    return format_code, codec_code


def _start_one_render_job(project, job_id) -> None:
    started = False
    try:
        started = bool(project.StartRendering([job_id], False))
    except TypeError:
        pass
    if not started:
        try:
            started = bool(project.StartRendering(job_id))
        except TypeError:
            started = bool(project.StartRendering())
    if not started:
        raise RuntimeError("达芬奇没有启动渲染任务。请确认当前时间线可正常导出。")


def _find_intermediate(folder: Path, base_name: str, started_at: float) -> Path:
    candidates = [
        path for path in folder.iterdir()
        if path.is_file() and path.name != ".okk_resolve_temp" and path.stat().st_mtime >= started_at - 2
    ]
    preferred = [path for path in candidates if path.stem.startswith(base_name) and path.suffix.lower() == ".mov"]
    if not preferred:
        preferred = [path for path in candidates if path.stem.startswith(base_name)]
    if not preferred:
        raise RuntimeError("达芬奇显示完成，但没有找到生成的中间母版文件。")
    return max(preferred, key=lambda path: path.stat().st_mtime)


class ExportDialog:
    def __init__(self, root: tk.Tk, resolve_app, project, timeline, app_root: Path) -> None:
        self.root = root
        self.resolve = resolve_app
        self.project = project
        self.timeline = timeline
        self.app_root = app_root
        self.timeline_name = _safe_name(timeline.GetName() or "Timeline")

        default_dir = Path.home() / "Videos" / "OKK Exports"
        self.output_var = tk.StringVar(value=str(default_dir / f"{self.timeline_name}_OKK_HEVC.mov"))
        self.bitrate_var = tk.StringVar(value="80M")
        self.quality_var = tk.StringVar(value="20")
        self.preset_var = tk.StringVar(value="极速 P2")
        self.depth_var = tk.StringVar(value="10-bit")
        self.audio_var = tk.StringVar(value="复制母版音频（PCM）")
        self.delete_intermediate_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="准备导出当前时间线")
        self.busy = False
        self.cancel_requested = False
        self.process: subprocess.Popen | None = None

        root.title(APP_TITLE)
        root.geometry("680x560")
        root.minsize(620, 520)
        root.configure(bg="#100b17")
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        self._build()

    def _build(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#1d1427")
        style.configure("TLabel", background="#1d1427", foreground="#fff7fd", font=("Microsoft YaHei UI", 11))
        style.configure("TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=8)
        style.configure("TCombobox", font=("Microsoft YaHei UI", 11))
        style.configure("Accent.TButton", background="#9b5de5", foreground="white")

        panel = ttk.Frame(self.root, padding=22)
        panel.pack(fill="both", expand=True, padx=18, pady=18)
        ttk.Label(panel, text="OKK GROUP · 达芬奇 RTX 导出", font=("Microsoft YaHei UI", 18, "bold"), foreground="#ff5cad").pack(anchor="w")
        ttk.Label(panel, text=f"当前时间线：{self.timeline_name}", foreground="#cdb8d4").pack(anchor="w", pady=(5, 18))

        grid = ttk.Frame(panel)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self._row(grid, 0, "最终输出", self.output_var, browse=True)
        self._row(grid, 1, "视频目标码率", self.bitrate_var)

        ttk.Label(grid, text="速度 / 画质").grid(row=2, column=0, sticky="w", pady=7)
        ttk.Combobox(grid, textvariable=self.preset_var, state="readonly", values=("最快 P1", "极速 P2", "均衡 P4", "高质量 P6"), width=22).grid(row=2, column=1, sticky="ew", pady=7)
        ttk.Label(grid, text="输出位深").grid(row=3, column=0, sticky="w", pady=7)
        ttk.Combobox(grid, textvariable=self.depth_var, state="readonly", values=("10-bit", "8-bit", "自动"), width=22).grid(row=3, column=1, sticky="ew", pady=7)
        ttk.Label(grid, text="CQ（码率留空时）").grid(row=4, column=0, sticky="w", pady=7)
        ttk.Entry(grid, textvariable=self.quality_var).grid(row=4, column=1, sticky="ew", pady=7)
        ttk.Label(grid, text="最终音频").grid(row=5, column=0, sticky="w", pady=7)
        ttk.Combobox(
            grid, textvariable=self.audio_var, state="readonly",
            values=("复制母版音频（PCM）", "AAC 320 kbps", "AAC 192 kbps", "无音频"),
        ).grid(row=5, column=1, sticky="ew", pady=7)

        ttk.Checkbutton(
            panel,
            text="最终导出成功后删除达芬奇 DNxHR 临时母版",
            variable=self.delete_intermediate_var,
        ).pack(anchor="w", pady=(14, 6))
        ttk.Label(
            panel,
            text="流程：达芬奇渲染 QuickTime/DNxHR HQX → OKK 使用 RTX NVENC 输出 MOV/H.265。\n"
                 "DNxHR 由 CPU 解码，H.265 编码由 NVIDIA NVENC 完成。",
            foreground="#b59fba",
            wraplength=610,
            justify="left",
        ).pack(anchor="w", pady=(0, 14))

        self.progress = ttk.Progressbar(panel, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 8))
        ttk.Label(panel, textvariable=self.status_var, foreground="#75e7b0").pack(anchor="w")
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=(18, 0))
        self.export_button = ttk.Button(buttons, text="开始 OKK 导出", style="Accent.TButton", command=self.start)
        self.export_button.pack(side="left", fill="x", expand=True)
        self.cancel_button = ttk.Button(buttons, text="关闭", command=self.cancel)
        self.cancel_button.pack(side="left", padx=(10, 0))

    def _row(self, parent, row: int, label: str, variable: tk.StringVar, *, browse: bool = False) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=7, padx=(0, 12))
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=7)
        if browse:
            ttk.Button(parent, text="浏览…", command=self.browse_output).grid(row=row, column=2, padx=(8, 0), pady=7)

    def browse_output(self) -> None:
        chosen = filedialog.asksaveasfilename(
            title="选择 OKK 最终输出",
            initialfile=Path(self.output_var.get()).name,
            initialdir=str(Path(self.output_var.get()).parent),
            defaultextension=".mov",
            filetypes=[("QuickTime MOV", "*.mov")],
        )
        if chosen:
            self.output_var.set(str(Path(chosen).with_suffix(".mov")))

    def _update(self, text: str, percent: float | None = None) -> None:
        self.status_var.set(text)
        if percent is not None:
            self.progress["value"] = max(0.0, min(100.0, percent))
        self.root.update_idletasks()
        self.root.update()

    def cancel(self) -> None:
        if not self.busy:
            self.root.destroy()
            return
        if not messagebox.askyesno(APP_TITLE, "当前导出仍在进行，确定取消吗？"):
            return
        self.cancel_requested = True
        self.status_var.set("正在取消当前导出…")
        try:
            if self.project.IsRenderingInProgress():
                self.project.StopRendering()
        except Exception:
            pass
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

    def start(self) -> None:
        output = Path(self.output_var.get().strip()).expanduser()
        if not self.output_var.get().strip():
            messagebox.showerror(APP_TITLE, "请选择最终输出文件。")
            return
        if output.suffix.lower() != ".mov":
            output = output.with_suffix(".mov")
            self.output_var.set(str(output))
        try:
            quality = int(self.quality_var.get())
            if not 0 <= quality <= 51:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_TITLE, "CQ 必须填写 0–51 的整数。")
            return
        if output.exists() and not messagebox.askyesno(APP_TITLE, f"输出文件已经存在，是否覆盖？\n{output}"):
            return

        self.busy = True
        self.cancel_requested = False
        self.export_button.configure(state="disabled")
        self.cancel_button.configure(text="取消任务")
        job_id = None
        status_file: Path | None = None
        try:
            _configure_intermediate(self.project)
            temp_folder = Path(tempfile.gettempdir()) / f"OKK_Resolve_{uuid.uuid4().hex[:12]}"
            temp_folder.mkdir(parents=True, exist_ok=False)
            (temp_folder / ".okk_resolve_temp").write_text("OKK GROUP DaVinci connector", encoding="utf-8")
            render_name = f"{self.timeline_name}_DNxHR_HQX"
            render_settings = {
                "TargetDir": str(temp_folder),
                "CustomName": render_name,
                "SelectAllFrames": True,
                "ExportVideo": True,
                "ExportAudio": True,
            }
            if not self.project.SetRenderSettings(render_settings):
                raise RuntimeError("达芬奇拒绝应用中间母版渲染设置。")
            job_id = self.project.AddRenderJob()
            if not job_id:
                raise RuntimeError("无法把当前时间线加入达芬奇渲染队列。")

            self._update("阶段 1/2：达芬奇正在渲染 DNxHR HQX 中间母版…", 0)
            render_started = time.time()
            _start_one_render_job(self.project, job_id)
            while self.project.IsRenderingInProgress():
                if self.cancel_requested:
                    raise ExportCancelled("导出已取消。")
                status = self.project.GetRenderJobStatus(job_id) or {}
                percent = float(status.get("CompletionPercentage") or 0.0)
                self._update(f"阶段 1/2：达芬奇渲染 {percent:.1f}%", percent * 0.5)
                time.sleep(0.5)
            if self.cancel_requested:
                raise ExportCancelled("导出已取消。")
            status = self.project.GetRenderJobStatus(job_id) or {}
            if str(status.get("JobStatus", "")).lower() not in {"complete", "completed"}:
                raise RuntimeError(f"达芬奇渲染没有完成：{status.get('JobStatus', '未知状态')}")
            intermediate = _find_intermediate(temp_folder, render_name, render_started)

            status_folder = Path(tempfile.gettempdir()) / "OKK_GROUP_Status"
            status_folder.mkdir(parents=True, exist_ok=True)
            status_file = status_folder / f"{uuid.uuid4().hex}.json"
            preset = self.preset_var.get().split()[-1].lower()
            depth = self.depth_var.get().replace("自动", "auto")
            audio_text = self.audio_var.get()
            audio = "none" if audio_text == "无音频" else ("aac" if audio_text.startswith("AAC") else "copy")
            audio_bitrate = "192k" if "192" in audio_text else "320k"
            arguments = [
                "--headless-export",
                "--input", str(intermediate),
                "--output", str(output.resolve()),
                "--codec", "hevc_nvenc",
                "--preset", preset,
                "--quality", str(quality),
                "--bit-depth", depth,
                "--video-bitrate", self.bitrate_var.get().strip(),
                "--audio", audio,
                "--audio-bitrate", audio_bitrate,
                "--decode", "auto",
                "--status-file", str(status_file),
            ]
            if self.delete_intermediate_var.get():
                arguments.append("--delete-input-on-success")

            command = _launcher_command(self.app_root, arguments)
            self._update("阶段 2/2：正在启动 OKK RTX NVENC…", 50)
            process = subprocess.Popen(command, cwd=str(self.app_root), creationflags=CREATE_NO_WINDOW)
            self.process = process
            latest: dict = {}
            while process.poll() is None:
                if self.cancel_requested:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise ExportCancelled("导出已取消。")
                if status_file.is_file():
                    try:
                        latest = json.loads(status_file.read_text(encoding="utf-8"))
                        encode_percent = float(latest.get("percent") or 0.0)
                        self._update(
                            str(latest.get("message") or "OKK NVENC 正在导出…"),
                            50.0 + encode_percent * 0.5,
                        )
                    except (OSError, ValueError, tk.TclError):
                        pass
                time.sleep(0.5)
            if process.returncode:
                if status_file.is_file():
                    latest = json.loads(status_file.read_text(encoding="utf-8"))
                raise RuntimeError(str(latest.get("message") or f"OKK 导出失败，退出码 {process.returncode}"))
            self._update("OKK 导出完成", 100)
            messagebox.showinfo(APP_TITLE, f"导出完成：\n{output}")
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(output.resolve())])
        except ExportCancelled:
            self.status_var.set("导出已取消")
        except Exception as exc:
            self.status_var.set("导出失败")
            messagebox.showerror(APP_TITLE, str(exc))
        finally:
            if job_id and hasattr(self.project, "DeleteRenderJob"):
                try:
                    self.project.DeleteRenderJob(job_id)
                except Exception:
                    pass
            if status_file is not None:
                status_file.unlink(missing_ok=True)
            self.process = None
            self.busy = False
            self.cancel_requested = False
            self.export_button.configure(state="normal")
            self.cancel_button.configure(text="关闭")


def main() -> None:
    try:
        resolve_app = _resolve_object()
        project_manager = resolve_app.GetProjectManager()
        project = project_manager.GetCurrentProject() if project_manager else None
        timeline = project.GetCurrentTimeline() if project else None
        if project is None or timeline is None:
            raise RuntimeError("请先在达芬奇中打开一个项目和时间线。")
        app_root = _read_app_root()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, str(exc))
        root.destroy()
        return

    root = tk.Tk()
    ExportDialog(root, resolve_app, project, timeline, app_root)
    root.mainloop()


if __name__ == "__main__":
    main()
