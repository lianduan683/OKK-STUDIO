"""DaVinci 风格媒体池工作台：素材引用、媒体夹管理与批量探针。"""

from __future__ import annotations

import tkinter as tk
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import exporter_core
from media_pool_core import (
    ALL_BIN_ID,
    UNGROUPED_BIN_ID,
    MediaPoolModel,
    is_supported_audio,
)
from ui_components import Card, EmptyState, SectionHeader
from ui_theme import (
    BG,
    INPUT,
    MUTED,
    PANEL,
    PANEL_ALT,
    PURPLE,
    RED,
    TEXT,
    UI_FONT,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # 未安装 tkinterdnd2：媒体池仍可用，只是没有资源管理器拖放
    DND_FILES = "DND_Files"
    TkinterDnD = None


PROBE_EVENT = "media_pool_probe"
PROBE_ERROR_EVENT = "media_pool_probe_error"

MEDIA_FILETYPES = [
    ("视频与音频", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts *.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.wma *.aif *.aiff *.mka"),
    ("音频文件", "*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.wma *.aif *.aiff *.mka"),
    ("视频文件", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts"),
    ("所有文件", "*.*"),
]

COLUMNS = ("name", "duration", "resolution", "fps", "codec", "depth", "audio", "status", "folder")
HEADINGS = {
    "name": "名称", "duration": "时长", "resolution": "分辨率", "fps": "FPS",
    "codec": "编码", "depth": "位深", "audio": "音频", "status": "状态", "folder": "目录",
}
COLUMN_WIDTHS = {
    "name": 240, "duration": 84, "resolution": 96, "fps": 62, "codec": 82,
    "depth": 56, "audio": 84, "status": 74, "folder": 220,
}
STATUS_TEXT = {"pending": "待读取", "probing": "读取中", "ready": "就绪", "offline": "离线", "error": "失败"}
PROBE_WORKERS = 2
FALLBACK_EDITOR_INDEX = 2
DROP_EVENTS = ("<<DropEnter>>", "<<DropLeave>>", "<<Drop>>")
DROP_HIGHLIGHT_WIDTH = 2


def _format_duration(value: float) -> str:
    if value <= 0:
        return "—"
    total = int(round(value))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _format_fps(value: float) -> str:
    return f"{value:.2f}" if value > 0 else "—"


# 进程级共享探针线程池：至多 PROBE_WORKERS 个 daemon 线程，随进程创建后常驻，
# 不做哨兵退出——线程中途退出/新建会在 Tk 环境下触发 Tcl_AsyncDelete 竞态，
# 且 daemon 线程永远不会阻塞解释器退出。
_PROBE_QUEUE: queue.Queue = queue.Queue()
_PROBE_THREADS: list[threading.Thread] = []
_PROBE_THREADS_LOCK = threading.Lock()


def _ensure_probe_workers() -> None:
    with _PROBE_THREADS_LOCK:
        while len(_PROBE_THREADS) < PROBE_WORKERS:
            thread = threading.Thread(
                target=_probe_loop,
                name=f"media_pool_probe_{len(_PROBE_THREADS)}",
                daemon=True,
            )
            thread.start()
            _PROBE_THREADS.append(thread)


def _probe_loop() -> None:
    while True:
        workspace, key, path, token = _PROBE_QUEUE.get()
        if workspace.closed:
            continue  # 工作台已关闭：取消未开始的任务，结果也不会被采纳
        try:
            workspace._probe_worker(key, path, token)
        except Exception:
            continue


class MediaPoolWorkspace(tk.Frame):
    """媒体池 UI；模型只保存引用，移除素材不会触碰原片。"""

    def __init__(self, app, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, bg=BG, **kwargs)
        self.app = app
        self.model = MediaPoolModel()
        self.closed = False
        self.current_bin = ALL_BIN_ID
        self.sort_key = "name"
        self.sort_reverse = False
        self._tokens: dict[str, int] = {}
        self._awaiting_capabilities: set[str] = set()
        self._bin_ids: list[str] = []
        self._search_after: str | None = None
        self._drop_targets: list[tk.Widget] = []
        self._timeline_drag = None
        _ensure_probe_workers()
        self.search_var = tk.StringVar()
        self.count_var = tk.StringVar(value="媒体池为空")
        self.search_var.trace_add("write", self._on_search_changed)
        self._build()
        self._setup_drag_drop()
        self.refresh()

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        SectionHeader(
            header,
            "媒体池",
            "像 DaVinci 媒体池一样管理素材引用；移除只是取消引用，绝不删除原片。",
            accent="MEDIA POOL",
            bg=BG,
        ).pack(side="left")
        actions = tk.Frame(header, bg=BG)
        actions.pack(side="right")
        ttk.Button(actions, text="导入文件", command=self.import_files).pack(side="left", padx=3)
        ttk.Button(actions, text="导入文件夹", command=self.import_folder).pack(side="left", padx=3)
        ttk.Button(actions, text="刷新", command=self.refresh).pack(side="left", padx=3)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=4)
        self._build_bin_panel(body)
        self._build_clip_panel(body)

        bottom = tk.Frame(self, bg=BG)
        bottom.grid(row=2, column=0, sticky="ew", padx=14, pady=(4, 12))
        manage = tk.Frame(bottom, bg=BG)
        manage.pack(side="left")
        self.move_button = ttk.Button(manage, text="移动到…", command=self._show_move_menu)
        self.move_button.pack(side="left", padx=3)
        ttk.Button(manage, text="资源管理器定位", command=self.reveal_selected).pack(side="left", padx=3)
        ttk.Button(manage, text="移除所选", command=self.remove_selected).pack(side="left", padx=3)
        send = tk.Frame(bottom, bg=BG)
        send.pack(side="right")
        ttk.Button(send, text="设为导出源", style="Secondary.TButton",
                   command=self.set_export_source).pack(side="left", padx=3)
        ttk.Button(send, text="设为主音频", command=self.set_master_audio).pack(side="left", padx=3)
        ttk.Button(send, text="加入拼接", command=self.add_to_join).pack(side="left", padx=3)
        ttk.Button(send, text="加入代理", command=self.add_to_proxy).pack(side="left", padx=3)

    def _build_bin_panel(self, parent: ttk.Panedwindow) -> None:
        panel = Card(parent, bg=PANEL, padding=12)
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)
        SectionHeader(panel, "媒体夹", accent="BINS").grid(row=0, column=0, sticky="ew")
        self.bin_list = tk.Listbox(
            panel, bg=INPUT, fg=TEXT, selectbackground=PURPLE, selectforeground="#ffffff",
            relief="flat", bd=0, highlightthickness=0, activestyle="none",
            font=(UI_FONT, 10), exportselection=False,
        )
        self.bin_list.grid(row=1, column=0, sticky="nsew", pady=(8, 6))
        self.bin_list.bind("<<ListboxSelect>>", self._on_bin_selected)
        buttons = tk.Frame(panel, bg=PANEL)
        buttons.grid(row=2, column=0, sticky="ew")
        ttk.Button(buttons, text="新建", command=self.create_bin).pack(side="left", padx=2, pady=2)
        ttk.Button(buttons, text="重命名", command=self.rename_bin).pack(side="left", padx=2, pady=2)
        ttk.Button(buttons, text="删除", command=self.delete_bin).pack(side="left", padx=2, pady=2)
        parent.add(panel, weight=1)

    def _build_clip_panel(self, parent: ttk.Panedwindow) -> None:
        panel = Card(parent, bg=PANEL, padding=12)
        panel.rowconfigure(2, weight=1)
        panel.columnconfigure(0, weight=1)
        top = tk.Frame(panel, bg=PANEL)
        top.grid(row=0, column=0, sticky="ew")
        self.search_entry = tk.Entry(
            top, textvariable=self.search_var, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=(UI_FONT, 10),
        )
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=6)
        tk.Label(top, textvariable=self.count_var, bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 9)).pack(side="right", padx=(10, 0))
        tk.Label(panel, text="双击预览；拖动素材到剪辑时间线；也可直接拖入文件 / 文件夹。",
                 bg=PANEL, fg=MUTED, font=(UI_FONT, 8), anchor="w").grid(row=1, column=0, sticky="ew", pady=(6, 4))

        holder = tk.Frame(panel, bg=PANEL)
        holder.grid(row=2, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            holder, columns=COLUMNS, show="headings", selectmode="extended",
            style="Pool.Treeview",
        )
        for column in COLUMNS:
            tree.heading(column, text=HEADINGS[column], command=lambda col=column: self.toggle_sort(col))
            tree.column(column, width=COLUMN_WIDTHS[column], minwidth=40,
                        stretch=column in {"name", "folder"})
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        tree.bind("<Double-Button-1>", self._on_double_click)
        tree.bind("<ButtonPress-1>", self._timeline_drag_press, add="+")
        tree.bind("<B1-Motion>", self._timeline_drag_motion, add="+")
        tree.bind("<ButtonRelease-1>", self._timeline_drag_release, add="+")
        tree.bind("<Escape>", self._timeline_drag_cancel, add="+")
        tree.bind("<Delete>", lambda _event: self.remove_selected())
        self.clip_tree = tree
        self.empty_state = EmptyState(
            holder, "媒体池为空",
            "点击右上角“导入文件 / 导入文件夹”，或直接拖入文件/文件夹；不会移动或复制原片。",
        )
        self._update_headings()
        parent.add(panel, weight=4)

    # --------------------------------------------------------------- public

    def add_paths(self, paths, bin_id: str | None = None) -> list:
        """添加素材引用（文件或文件夹），并对新素材发起探针。"""
        if self.closed:
            return []
        try:
            added = self.model.add_paths(paths, bin_id)
        except ValueError as exc:
            self._error(exc)
            return []
        for clip in added:
            self._enqueue_probe(clip)
        if added:
            self._log(f"媒体池：新增 {len(added)} 个素材引用。")
        self.refresh()
        return added

    def handle_probe(self, payload: dict) -> None:
        """主线程事件回调：探针成功。"""
        key = payload.get("key")
        token = payload.get("token")
        if self.closed or key is None or self._tokens.get(key) != token:
            return
        clip = self.model.clips.get(key)
        if clip is None:
            return
        clip.info = payload.get("info")
        clip.status = "ready"
        clip.error = ""
        self._awaiting_capabilities.discard(key)
        self._refresh_tree()
        self._update_count()

    def handle_probe_error(self, payload: dict) -> None:
        """主线程事件回调：探针失败。"""
        key = payload.get("key")
        token = payload.get("token")
        if self.closed or key is None or self._tokens.get(key) != token:
            return
        clip = self.model.clips.get(key)
        if clip is None:
            return
        detail = str(payload.get("error") or "读取失败")
        if not self.app.caps.ffprobe_path:
            clip.status = "pending"
            clip.error = "等待 ffprobe"
            self._awaiting_capabilities.add(key)
        else:
            clip.status = "error"
            clip.error = detail
        self._refresh_tree()
        self._update_count()

    def on_capabilities(self) -> None:
        """能力检测完成后调用：重试等待 ffprobe 的素材。"""
        if self.closed:
            return
        if not self.app.caps.ffprobe_path or not self._awaiting_capabilities:
            return
        keys = list(self._awaiting_capabilities)
        self._awaiting_capabilities.clear()
        for key in keys:
            clip = self.model.clips.get(key)
            if clip is not None:
                self._enqueue_probe(clip)
        self._refresh_tree()
        self._update_count()

    def close(self) -> None:
        if self.closed:
            return
        self._timeline_drag_cancel()
        self.closed = True
        self._teardown_drag_drop()
        for key in list(self._tokens):
            self._tokens[key] += 1
        self._awaiting_capabilities.clear()
        # 队列中未开始的任务会被 worker 跳过；正在运行的 ffprobe 不等待，
        # 其迟到结果由 token 与 closed 标记丢弃。daemon 线程不阻塞解释器退出。

    # ------------------------------------------------------------ import UI

    def import_files(self) -> None:
        paths = filedialog.askopenfilenames(title="导入素材文件", filetypes=MEDIA_FILETYPES, parent=self)
        if paths:
            self.add_paths(list(paths), self._import_target_bin())

    def import_folder(self) -> None:
        path = filedialog.askdirectory(title="导入整个文件夹（含子文件夹）", parent=self)
        if path:
            self.add_paths([path], self._import_target_bin())

    def _import_target_bin(self) -> str | None:
        return self.current_bin if self.current_bin not in {ALL_BIN_ID, UNGROUPED_BIN_ID} else None

    # ------------------------------------------------------------ drag & drop

    def _timeline_drag_press(self, event) -> None:
        if self.closed or self.clip_tree.identify_region(event.x, event.y) != "cell":
            self._timeline_drag = None
            return
        item = str(self.clip_tree.identify_row(event.y))
        if item not in self.model.clips:
            self._timeline_drag = None
            return
        selected = set(self._selected_keys())
        # Preserve a multi-selection when the class binding changes selection
        # on the same press. Keep the visible order, including sorted lists.
        keys = [key for key in self.clip_tree.get_children() if key in selected] if item in selected else [item]
        self._timeline_drag = {"x": event.x_root, "y": event.y_root, "keys": keys, "active": False}

    def _timeline_drag_motion(self, event):
        drag = self._timeline_drag
        if not drag or self.closed:
            return None
        if not drag["active"]:
            distance = max(abs(event.x_root - drag["x"]), abs(event.y_root - drag["y"]))
            if distance < 6:
                return None
            drag["active"] = True
            self.clip_tree.configure(cursor="hand2")
        hover = getattr(self.app, "_media_pool_timeline_hover", None)
        if callable(hover):
            hover(event.x_root, event.y_root)
        return "break"

    def _timeline_drag_release(self, event):
        drag = self._timeline_drag
        self._timeline_drag = None
        if not drag or not drag["active"] or self.closed:
            return None
        self.clip_tree.configure(cursor="")
        clear = getattr(self.app, "_media_pool_timeline_clear_hover", None)
        if callable(clear):
            clear()
        drop = getattr(self.app, "_media_pool_timeline_drop", None)
        if callable(drop):
            clips = [self.model.clips[key] for key in drag["keys"] if key in self.model.clips]
            if clips:
                drop(clips, event.x_root, event.y_root)
        return "break"

    def _timeline_drag_cancel(self, _event=None):
        drag = self._timeline_drag
        self._timeline_drag = None
        if drag and drag["active"]:
            self.clip_tree.configure(cursor="")
            clear = getattr(self.app, "_media_pool_timeline_clear_hover", None)
            if callable(clear):
                clear()
            return "break"
        return None

    def _setup_drag_drop(self) -> None:
        """把整个媒体池可见区域注册为拖放目标；tkinterdnd2 缺失时静默降级。"""
        if TkinterDnD is None or not hasattr(self, "drop_target_register"):
            return
        try:
            # 兼容主窗口尚未切换为 TkinterDnD.Tk 的情况：直接往当前解释器加载 tkdnd
            TkinterDnD._require(self.winfo_toplevel())
        except Exception as exc:
            self._log(f"媒体池：tkdnd 拖放库加载失败，已禁用拖放导入（{exc}）。")
            return
        # Treeview 等子控件会挡住落点，必须把整个可见区域逐个注册
        for widget in self._iter_drop_targets():
            self._register_drop_target(widget)

    def _iter_drop_targets(self) -> list[tk.Widget]:
        """所有可接收拖放的区域：工作区 Frame、素材列表、媒体夹、搜索框与空状态。"""
        targets: list[tk.Widget] = [self, self.clip_tree, self.bin_list, self.search_entry]
        stack = [self.empty_state]
        while stack:
            widget = stack.pop()
            targets.append(widget)
            stack.extend(widget.winfo_children())
        return targets

    def _register_drop_target(self, widget: tk.Widget) -> None:
        """为 widget 注册 DND_FILES 目标并绑定三个拖放事件；失败则跳过该控件。"""
        if TkinterDnD is None or not hasattr(widget, "drop_target_register"):
            return
        if widget in self._drop_targets:
            return
        try:
            widget.drop_target_register(DND_FILES)
            for sequence, handler in (
                (DROP_EVENTS[0], self._on_drop_enter),
                (DROP_EVENTS[1], self._on_drop_leave),
                (DROP_EVENTS[2], self._on_drop),
            ):
                widget.dnd_bind(sequence, handler)
        except tk.TclError as exc:
            self._log(f"媒体池：拖放目标注册失败（{exc}）。")
            return
        self._drop_targets.append(widget)

    def _teardown_drag_drop(self) -> None:
        for widget in list(self._drop_targets):
            unregister = getattr(widget, "drop_target_unregister", None)
            if unregister is None:
                continue
            try:
                unregister()
            except tk.TclError:
                pass
            for sequence in DROP_EVENTS:
                try:
                    widget.unbind(sequence)
                except tk.TclError:
                    pass
        self._drop_targets.clear()

    def _on_drop_enter(self, event):
        self._set_drop_hover(True)
        return getattr(event, "action", None)

    def _on_drop_leave(self, _event=None) -> None:
        self._set_drop_hover(False)

    def _on_drop(self, event):
        self._set_drop_hover(False)
        try:
            self._import_dropped_data(getattr(event, "data", ""))
        except Exception as exc:
            # 拖放回调向外抛异常会打断 Tk 事件循环，这里只记录
            try:
                self._log(f"媒体池：拖放导入失败：{exc}")
            except Exception:
                pass
        return getattr(event, "action", None)

    def _import_dropped_data(self, raw) -> None:
        text = str(raw or "").strip()
        if not text or self.closed:
            return
        # splitlist 会拆开 "{带 空格 的路径}" 这类花括号包裹项，兼容多文件/文件夹
        paths = [str(item) for item in self.tk.splitlist(text) if str(item).strip()]
        if not paths:
            return
        self.add_paths(paths, self._import_target_bin())

    def _set_drop_hover(self, active: bool) -> None:
        try:
            if active:
                self.configure(
                    highlightthickness=DROP_HIGHLIGHT_WIDTH,
                    highlightbackground=PURPLE,
                    highlightcolor=PURPLE,
                )
            else:
                self.configure(highlightthickness=0)
        except tk.TclError:
            pass

    # ------------------------------------------------------------ bin actions

    def create_bin(self) -> None:
        name = self._ask_text("新建媒体夹", "媒体夹名称：")
        if name is None:
            return
        try:
            item = self.model.create_bin(name)
        except ValueError as exc:
            self._error(exc)
            return
        self.current_bin = item.id
        self.refresh()
        self._log(f"媒体池：已新建媒体夹“{item.name}”。")

    def rename_bin(self) -> None:
        item = self._selected_bin_item()
        if item is None:
            self._error("请选择一个自定义媒体夹进行重命名。")
            return
        name = self._ask_text("重命名媒体夹", "新的媒体夹名称：", initial=item.name)
        if name is None:
            return
        try:
            self.model.rename_bin(item.id, name)
        except ValueError as exc:
            self._error(exc)
            return
        self.refresh()
        self._log(f"媒体池：媒体夹已重命名为“{name}”。")

    def delete_bin(self) -> None:
        item = self._selected_bin_item()
        if item is None:
            self._error("请选择一个自定义媒体夹进行删除（其中素材会回到“未分类”）。")
            return
        if not messagebox.askyesno(
            "删除媒体夹",
            f"删除媒体夹“{item.name}”？\n其中素材会回到“未分类”，原片不受影响。",
            parent=self,
        ):
            return
        try:
            self.model.delete_bin(item.id)
        except ValueError as exc:
            self._error(exc)
            return
        self.current_bin = ALL_BIN_ID
        self.refresh()
        self._log(f"媒体池：已删除媒体夹“{item.name}”。")

    def _selected_bin_item(self):
        selection = self.bin_list.curselection()
        if not selection:
            return None
        index = selection[0]
        if not 0 <= index < len(self._bin_ids):
            return None
        return self.model.bins.get(self._bin_ids[index])

    def _on_bin_selected(self, _event=None) -> None:
        selection = self.bin_list.curselection()
        if not selection:
            return
        index = selection[0]
        if 0 <= index < len(self._bin_ids):
            self.current_bin = self._bin_ids[index]
            self._refresh_tree()
            self._update_count()

    # ------------------------------------------------------------ clip actions

    def _selected_keys(self) -> list[str]:
        return [str(item) for item in self.clip_tree.selection()]

    def _selected_clips(self) -> list:
        clips = []
        for key in self._selected_keys():
            clip = self.model.clips.get(key)
            if clip is not None:
                clips.append(clip)
        return clips

    @staticmethod
    def _audio_only(clip) -> bool:
        if clip.info is not None:
            return not getattr(clip.info, "has_video", True)
        return is_supported_audio(clip.path)

    def toggle_sort(self, column: str) -> None:
        if self.sort_key == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = column
            self.sort_reverse = False
        self._update_headings()
        self._refresh_tree()

    def _update_headings(self) -> None:
        arrow = " ▼" if self.sort_reverse else " ▲"
        for column in COLUMNS:
            text = HEADINGS[column] + (arrow if column == self.sort_key else "")
            self.clip_tree.heading(column, text=text)

    def _show_move_menu(self) -> None:
        keys = self._selected_keys()
        if not keys:
            self._error("请先选择需要移动的素材。")
            return
        previous = getattr(self, "_move_menu", None)
        if previous is not None:
            previous.destroy()
        menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=TEXT, activebackground=PANEL_ALT,
                       activeforeground=TEXT, bd=0, font=(UI_FONT, 10))
        self._move_menu = menu
        menu.add_command(label=f"移动 {len(keys)} 个素材到：", state="disabled")
        menu.add_separator()
        menu.add_command(label="未分类", command=lambda: self._move_selected(UNGROUPED_BIN_ID))
        for item in self.model.bins.values():
            menu.add_command(label=item.name, command=lambda bin_id=item.id: self._move_selected(bin_id))
        menu.tk_popup(self.move_button.winfo_rootx(),
                      self.move_button.winfo_rooty() + self.move_button.winfo_height())

    def _move_selected(self, bin_id: str) -> None:
        try:
            self.model.move_clips(self._selected_keys(), bin_id)
        except ValueError as exc:
            self._error(exc)
            return
        target = "未分类" if bin_id == UNGROUPED_BIN_ID else self.model.bins[bin_id].name
        self._log(f"媒体池：已移动素材到“{target}”。")
        self.refresh()

    def reveal_selected(self) -> None:
        clips = self._selected_clips()
        if not clips:
            self._error("请先选择素材。")
            return
        path = Path(clips[0].path)
        if not path.is_file():
            self._error("素材文件不存在，可能已被移动或删除。")
            self.refresh()
            return
        self.app._open_folder(str(path))

    def remove_selected(self) -> None:
        keys = self._selected_keys()
        if not keys:
            self._error("请先选择需要移除的素材。")
            return
        if not messagebox.askyesno(
            "移除素材",
            f"从媒体池移除 {len(keys)} 个素材？\n只是取消引用，原片不会被删除或修改。",
            parent=self,
        ):
            return
        for key in keys:
            self._tokens[key] = self._tokens.get(key, 0) + 1
            self._awaiting_capabilities.discard(key)
        self.model.remove_clips(keys)
        self._log(f"媒体池：已移除 {len(keys)} 个素材引用（原片保留）。")
        self.refresh()

    def set_export_source(self) -> None:
        clips = self._selected_clips()
        if not clips:
            self._error("请先选择一个素材。")
            return
        path = clips[0].path
        if not Path(path).is_file():
            self._error("素材文件不存在，无法设为导出源。")
            self.refresh()
            return
        if self._audio_only(clips[0]):
            self._error("音频素材不能设为视频导出源，请使用“设为主音频”。")
            return
        setter = getattr(self.app, "_set_input_video", None)
        if callable(setter):
            setter(path)
        elif hasattr(self.app, "input_var"):
            self.app.input_var.set(path)
        else:
            self._error("导出页尚未就绪，无法设置导出源。")
            return
        self._log(f"媒体池：已设为导出源：{Path(path).name}")

    def set_master_audio(self) -> None:
        clips = self._selected_clips()
        if len(clips) != 1:
            self._error("请选择一个音频素材作为连续主音频。")
            return
        clip = clips[0]
        if not Path(clip.path).is_file():
            self._error("素材文件不存在，无法设为主音频。")
            self.refresh()
            return
        if clip.info is not None and not clip.info.has_audio:
            self._error("所选素材没有音频轨。")
            return
        setter = getattr(self.app, "_set_editor_audio", None)
        if not callable(setter):
            self._error("拼接 / 换音功能尚未就绪。")
            return
        setter(clip.path)
        self._log(f"媒体池：已设为连续主音频：{clip.name}")

    def add_to_join(self) -> None:
        clips = self._selected_clips()
        if not clips:
            self._error("请先选择需要加入拼接的素材。")
            return
        online = [clip for clip in clips if Path(clip.path).is_file()]
        paths = [clip.path for clip in online if not self._audio_only(clip)]
        missing = len(clips) - len(online)
        if not paths:
            self._error("请选择视频素材加入拼接；音频可使用“设为主音频”。" if online
                        else "所选素材文件都不存在。")
            self.refresh()
            return
        joiner = getattr(self.app, "_add_join_paths", None)
        if not callable(joiner):
            self._error("剪辑拼接功能尚未就绪。")
            return
        joiner(paths)
        note = f"（{missing} 个离线素材已跳过）" if missing else ""
        if len(online) > len(paths):
            note += f"（{len(online) - len(paths)} 个音频素材已跳过）"
        self._log(f"媒体池：已加入拼接 {len(paths)} 个素材{note}。")

    def add_to_proxy(self) -> None:
        clips = self._selected_clips()
        if not clips:
            self._error("请先选择需要加入代理的原片。")
            return
        online = [clip for clip in clips if Path(clip.path).is_file()]
        paths = [clip.path for clip in online if not self._audio_only(clip)]
        if not paths:
            self._error("请选择视频素材生成代理。" if online else "所选素材文件都不存在。")
            self.refresh()
            return
        self.app._add_proxy_paths(paths)
        self._log(f"媒体池：已加入代理 {len(paths)} 个原片。")

    def _on_double_click(self, event) -> None:
        item = self.clip_tree.identify_row(event.y)
        if not item:
            return
        clip = self.model.clips.get(str(item))
        if clip is None:
            return
        if not Path(clip.path).is_file():
            self._error("素材文件不存在，无法进入剪辑预览。")
            self.refresh()
            return
        loader = getattr(self.app, "_load_editor_video", None)
        if callable(loader):
            loader(clip.path)
        notebook = getattr(self.app, "main_notebook", None)
        if notebook is not None:
            try:
                notebook.select(getattr(self.app, "editor_tab", FALLBACK_EDITOR_INDEX))
            except tk.TclError:
                try:
                    notebook.select(FALLBACK_EDITOR_INDEX)
                except tk.TclError:
                    pass
        self._log(f"媒体池：已在剪辑工作台打开 {clip.name}")

    # ------------------------------------------------------------ probe plumbing

    def _enqueue_probe(self, clip) -> None:
        if self.closed:
            return
        key = clip.key
        token = self._tokens.get(key, 0) + 1
        self._tokens[key] = token
        clip.status = "probing"
        clip.error = ""
        _PROBE_QUEUE.put((self, key, clip.path, token))

    def _probe_worker(self, key: str, path: str, token: int) -> None:
        try:
            ffprobe = self.app.caps.ffprobe_path
            if not ffprobe:
                raise RuntimeError("未找到 ffprobe.exe，请放入 bin 文件夹。")
            info = exporter_core.probe_media(ffprobe, path, allow_audio_only=True)
            self.app.events.put((PROBE_EVENT, {"key": key, "path": path, "token": token, "info": info}))
        except Exception as exc:
            self.app.events.put((PROBE_ERROR_EVENT, {"key": key, "path": path, "token": token, "error": str(exc)}))

    # ------------------------------------------------------------ refresh

    def _on_search_changed(self, *_args) -> None:
        if self._search_after:
            try:
                self.after_cancel(self._search_after)
            except tk.TclError:
                pass
        self._search_after = self.after(250, self._search_flush)

    def _search_flush(self) -> None:
        self._search_after = None
        self._refresh_tree()
        self._update_count()

    def refresh(self) -> None:
        """刷新媒体夹列表、素材状态与右侧列表。"""
        self.model.refresh_file_states()
        for clip in self.model.clips.values():
            if clip.key in self._awaiting_capabilities:
                continue  # 等 ffprobe 就绪后由 on_capabilities 统一重试
            if clip.status == "pending":
                # 含离线恢复（info 仍是旧数据）与新增素材，都必须重新探针
                self._enqueue_probe(clip)
            elif clip.status == "error":
                # 上次探针失败：直接重试，无需移除后重新导入
                self._enqueue_probe(clip)
        self._refresh_bins()
        self._refresh_tree()
        self._update_count()

    def _refresh_bins(self) -> None:
        ungrouped_count = sum(1 for clip in self.model.clips.values() if clip.bin_id is None)
        entries = [
            (ALL_BIN_ID, f"全部素材 ({len(self.model.clips)})"),
            (UNGROUPED_BIN_ID, f"未分类 ({ungrouped_count})"),
        ]
        entries.extend((item.id, item.name) for item in self.model.bins.values())
        self._bin_ids = [bin_id for bin_id, _text in entries]
        if self.current_bin not in self._bin_ids:
            self.current_bin = ALL_BIN_ID
        listbox = self.bin_list
        listbox.delete(0, "end")
        for _bin_id, text in entries:
            listbox.insert("end", text)
        index = self._bin_ids.index(self.current_bin)
        listbox.selection_clear(0, "end")
        listbox.selection_set(index)
        listbox.see(index)

    def _refresh_tree(self) -> None:
        tree = self.clip_tree
        selected = set(tree.selection())
        tree.delete(*tree.get_children())
        clips = self.model.visible_clips(
            self.current_bin, self.search_var.get(), self.sort_key, self.sort_reverse,
        )
        for clip in clips:
            info = clip.info
            values = (
                clip.name,
                _format_duration(info.duration) if info else "—",
                f"{info.width}×{info.height}" if info and info.width else "—",
                _format_fps(info.fps) if info else "—",
                (info.video_codec.upper() if info.has_video else "音频") if info else "—",
                f"{info.bit_depth}bit" if info and info.has_video else "—",
                "无" if info and not info.has_audio else (info.audio_codec if info and info.has_audio else "—"),
                STATUS_TEXT.get(clip.status, clip.status),
                str(Path(clip.path).parent),
            )
            tree.insert("", "end", iid=clip.key, values=values, tags=(clip.status,))
        tree.tag_configure("ready", foreground=TEXT)
        tree.tag_configure("probing", foreground=MUTED)
        tree.tag_configure("pending", foreground=MUTED)
        tree.tag_configure("offline", foreground=RED)
        tree.tag_configure("error", foreground=RED)
        restore = [item for item in selected if tree.exists(item)]
        if restore:
            tree.selection_set(restore)
        if clips:
            self.empty_state.place_forget()
        else:
            self.empty_state.place(relx=0.5, rely=0.5, anchor="center")

    def _update_count(self) -> None:
        total = len(self.model.clips)
        if not total:
            self.count_var.set("媒体池为空")
            return
        pending = sum(1 for clip in self.model.clips.values() if clip.status in {"pending", "probing"})
        errors = sum(1 for clip in self.model.clips.values() if clip.status in {"error", "offline"})
        summary = f"共 {total} 个素材"
        if pending:
            summary += f" · {pending} 个读取中"
        if errors:
            summary += f" · {errors} 个异常"
        self.count_var.set(summary)

    # ------------------------------------------------------------ helpers

    def _log(self, message: str) -> None:
        logger = getattr(self.app, "_log", None)
        if callable(logger):
            logger(message)

    def _error(self, detail: object) -> None:
        self._log(f"媒体池：{detail}")
        messagebox.showerror("媒体池", str(detail), parent=self)

    def _ask_text(self, title: str, label: str, initial: str = "") -> str | None:
        root = self.winfo_toplevel()
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.configure(bg=PANEL, padx=18, pady=16)
        dialog.transient(root)
        dialog.resizable(False, False)
        result: dict[str, str | None] = {"value": None}
        tk.Label(dialog, text=label, bg=PANEL, fg=TEXT, font=(UI_FONT, 10)).pack(anchor="w")
        variable = tk.StringVar(value=initial)
        entry = tk.Entry(dialog, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                         relief="flat", font=(UI_FONT, 10), width=36)
        entry.pack(fill="x", ipady=6, pady=(6, 12))
        entry.selection_range(0, "end")
        entry.focus_set()

        def confirm(_event=None) -> None:
            result["value"] = variable.get().strip()
            dialog.destroy()

        def cancel(_event=None) -> None:
            dialog.destroy()

        row = tk.Frame(dialog, bg=PANEL)
        row.pack(fill="x")
        ttk.Button(row, text="确定", command=confirm).pack(side="right")
        ttk.Button(row, text="取消", command=cancel).pack(side="right", padx=(0, 8))
        dialog.bind("<Return>", confirm)
        dialog.bind("<Escape>", cancel)
        dialog.grab_set()
        root.wait_window(dialog)
        return result["value"]


__all__ = ["MediaPoolWorkspace"]
