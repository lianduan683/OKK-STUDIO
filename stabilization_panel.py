"""Selected-cut Gyroflow configuration without blocking Tk's event loop.

The workspace owns probing, workers, project changes, undo and cancellation.
This dialog only edits validated settings and passes them to that workspace.
No data is described as recognised until a real backend probe reports it.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import math
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from editor_core import format_time
from gyroflow_core import StabilizationSettings, default_settings, validate_settings
from program_edits import program_spans


BG, PANEL, INPUT = '#202124', '#292b2e', '#18191b'
INK, MUTED, ACCENT = '#dedfe3', '#969ba3', '#58a6d6'
ZOOM_NAMES = {'动态缩放': 'dynamic', '固定缩放': 'static', '不缩放': 'none'}
FIELD_LABELS = {
    'smoothness': '平滑程度', 'fov': '视野 FOV', 'zoom_speed': '缩放响应（秒）',
    'horizon_lock': '地平线锁定（%）', 'horizon_roll': '地平线倾斜（°）',
    'rolling_shutter_ms': '滚动快门读出（毫秒）', 'sync_search_size': '同步搜索范围（秒）',
    'sync_offset': '初始同步偏移（秒）',
}


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


class StabilizationDialog(tk.Toplevel):
    """A modal-input, asynchronous-operation dialog for one selected cut."""

    def __init__(self, workspace, *, mode=None):
        super().__init__(workspace.winfo_toplevel())
        self.ws = workspace
        self._closed = False
        self._loading = False
        self._probe_pending = False
        self._probe_token = None
        self._manual_widgets = []
        self.ui_scale = max(1.0, float(getattr(workspace, 'ui_scale', 1.0)))
        self.configure(bg=BG)
        self.withdraw()
        self.title('Gyroflow 防抖 · 所选片段')
        self.transient(workspace.winfo_toplevel())
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.selected_range = tuple(workspace.selected_program_range or ())
        self.mode_var = tk.StringVar(self, value='auto')
        self.zoom_var = tk.StringVar(self, value='动态缩放')
        self.auto_sync_var = tk.BooleanVar(self, value=True)
        self.variables = {name: tk.StringVar(self) for name in FIELD_LABELS}
        self.paths = {name: tk.StringVar(self) for name in (
            'project_path', 'gyro_path', 'lens_path', 'gyroflow_path')}
        self.target_var = tk.StringVar(self)
        self.source_var = tk.StringVar(self)
        self.gyro_status_var = tk.StringVar(self, value='陀螺仪数据：待 Gyroflow 检测')
        self.lens_status_var = tk.StringVar(self, value='镜头配置：待 Gyroflow 检测')
        self.status_var = tk.StringVar(self, value='自动模式将在开始时由 Gyroflow 检查数据和镜头；缺失时会报告原因。')
        self._target_error = ''
        self._build()
        settings = self._existing_settings()
        if mode in ('auto', 'manual'):
            settings = replace(settings, mode=mode)
        self._load(settings)
        self._describe_target()
        self.bind('<Escape>', lambda event: self.close())
        self.update_idletasks()
        root = workspace.winfo_toplevel()
        width = min(self.px(800), max(self.px(550), self.winfo_screenwidth()-self.px(70)))
        height = min(self.px(740), max(self.px(400), self.winfo_screenheight()-self.px(100)))
        x = max(0, root.winfo_rootx() + (root.winfo_width()-width)//2)
        y = max(0, root.winfo_rooty() + (root.winfo_height()-height)//2)
        self.geometry(f'{width}x{height}+{x}+{y}')
        self.minsize(min(width, self.px(570)), min(height, self.px(410)))
        self.deiconify()
        self.grab_set()
        self.focus_set()

    def px(self, value):
        return max(1, round(value*self.ui_scale))

    def _label(self, parent, text=None, *, variable=None, color=MUTED, bold=False):
        return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget('bg'), fg=color,
                        justify='left', anchor='w', font=('Microsoft YaHei UI', 9, 'bold' if bold else 'normal'))

    def _build(self):
        header = tk.Frame(self, bg=PANEL, padx=self.px(13), pady=self.px(9))
        header.pack(fill='x')
        self._label(header, variable=self.target_var, color=INK, bold=True).pack(fill='x')
        source = self._label(header, variable=self.source_var)
        source.pack(fill='x', pady=(self.px(3), 0))
        header.bind('<Configure>', lambda event: source.configure(wraplength=max(self.px(100), event.width-self.px(28))))
        footer = tk.Frame(self, bg=PANEL, padx=self.px(10), pady=self.px(7))
        footer.pack(side='bottom', fill='x')
        status = self._label(footer, variable=self.status_var, color=ACCENT)
        status.pack(fill='x', pady=(0, self.px(6)))
        footer.bind('<Configure>', lambda event: status.configure(wraplength=max(self.px(100), event.width-self.px(24))))
        actions = tk.Frame(footer, bg=PANEL)
        actions.pack(fill='x')
        self.close_button = ttk.Button(actions, text='关闭', command=self.close, style='Compact.TButton')
        self.close_button.pack(side='right', padx=self.px(3))
        self.start_button = ttk.Button(actions, text='开始防抖', command=self.start, style='Compact.TButton')
        self.start_button.pack(side='right', padx=self.px(3))
        self.remove_button = ttk.Button(actions, text='移除所选片段防抖', command=self.remove, style='Compact.TButton')
        self.remove_button.pack(side='left', padx=self.px(3))
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, width=1, height=1,
                                yscrollincrement=self.px(22))
        scroll = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = tk.Frame(self.canvas, bg=BG)
        self._window = self.canvas.create_window(0, 0, anchor='nw', window=self.body)
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(self._window, width=max(1, event.width)))
        self.body.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))

        self._section('配置方式')
        modes = tk.Frame(self.body, bg=BG)
        modes.pack(fill='x', padx=self.px(13), pady=self.px(3))
        for label, value in (('自动配置', 'auto'), ('手动设置参数', 'manual')):
            tk.Radiobutton(modes, text=label, variable=self.mode_var, value=value,
                           command=lambda chosen=value: self.auto_configure() if chosen == 'auto' else self._mode_changed(),
                           bg=BG, fg=INK, selectcolor=INPUT, activebackground=BG,
                           activeforeground=INK, font=('Microsoft YaHei UI', 9)).pack(side='left', padx=self.px(5))
        auto_actions = tk.Frame(self.body, bg=BG)
        auto_actions.pack(fill='x', padx=self.px(14), pady=self.px(5))
        self.auto_button = ttk.Button(auto_actions, text='恢复自动推荐参数', command=self.auto_configure,
                                      style='Compact.TButton')
        self.auto_button.pack(side='left', padx=self.px(2))
        self.probe_button = ttk.Button(auto_actions, text='检查陀螺仪 / 镜头', command=self.request_probe,
                                       style='Compact.TButton')
        self.probe_button.pack(side='left', padx=self.px(5))
        self._label(self.body, variable=self.gyro_status_var).pack(fill='x', padx=self.px(17), pady=self.px(2))
        self._label(self.body, variable=self.lens_status_var).pack(fill='x', padx=self.px(17), pady=self.px(2))
        self._note('自动配置使用 Gyroflow 的默认平滑与动态缩放，识别嵌入数据或下方指定的数据。'
                   '普通视频没有陀螺仪数据时，不能凭空使用 Gyroflow 防抖。')

        self._section('输入数据（可选；缺少嵌入数据时需要指定）')
        self._path_row('project_path', 'Gyroflow 工程', [('Gyroflow 工程', '*.gyroflow'), ('所有文件', '*.*')])
        self._path_row('gyro_path', '陀螺仪文件', [('运动数据', '*.gcsv *.csv *.bbl *.bfl *.txt *.bin *.json'), ('所有文件', '*.*')])
        self._path_row('lens_path', '镜头配置', [('Gyroflow 镜头配置', '*.json'), ('所有文件', '*.*')])
        self._path_row('gyroflow_path', 'Gyroflow 程序', [('Gyroflow 程序', '*.exe'), ('所有文件', '*.*')])
        self._note('留空时使用软件自动发现的 Gyroflow 程序、源片内嵌运动数据和可识别的镜头配置。'
                   '开始防抖会生成所选范围的缓存；源视频和主音频保持原样。')

        self._section('手动防抖参数')
        self._number_row('smoothness', '常用 0.5；越大越平滑，也可能裁切更多画面。')
        self._number_row('fov', '常用 1.0；增大会显示更宽视野，可能露出画面边缘。')
        row = self._row('缩放方式')
        combo = ttk.Combobox(row, textvariable=self.zoom_var, state='readonly', values=list(ZOOM_NAMES), width=19)
        combo.pack(side='right')
        self._manual_widgets.append((combo, 'readonly'))
        self._number_row('zoom_speed')
        self._number_row('horizon_lock')
        self._number_row('horizon_roll')
        self._number_row('rolling_shutter_ms', '有符号读出时间；0 为关闭，需按相机真实读出时间设置。')
        self._section('陀螺仪与画面同步')
        tk.Checkbutton(self.body, text='自动同步陀螺仪与画面', variable=self.auto_sync_var,
                       bg=BG, fg=INK, activebackground=BG, activeforeground=INK, selectcolor=INPUT,
                       font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=self.px(14), pady=self.px(3))
        self._number_row('sync_search_size')
        self._number_row('sync_offset', '已有可靠偏移时可填写；自动同步仍会按搜索范围检查。')
        self._install_scroll(self.body)

    def _section(self, title):
        self._label(self.body, title, color=INK, bold=True).pack(fill='x', padx=self.px(16), pady=(self.px(13), self.px(5)))

    def _note(self, text):
        note = self._label(self.body, text)
        note.pack(fill='x', padx=self.px(17), pady=self.px(4))
        self.body.bind('<Configure>', lambda event: note.configure(wraplength=max(self.px(120), event.width-self.px(36))), add='+')

    def _row(self, title):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill='x', padx=self.px(17), pady=self.px(4))
        self._label(row, title).pack(side='left', padx=(0, self.px(8)))
        return row

    def _entry(self, parent, variable, width=17):
        return tk.Entry(parent, textvariable=variable, width=width, bg=INPUT, fg=INK,
                        insertbackground=INK, relief='flat', highlightthickness=1,
                        highlightbackground='#404247', highlightcolor=ACCENT,
                        font=('Cascadia Mono', 9))

    def _number_row(self, name, note=None):
        row = self._row(FIELD_LABELS[name])
        entry = self._entry(row, self.variables[name])
        entry.pack(side='right', ipady=self.px(3))
        self._manual_widgets.append((entry, 'normal'))
        if note:
            self._note(note)

    def _path_row(self, name, title, filters):
        row = self._row(title)
        ttk.Button(row, text='选择…', width=6,
                   command=lambda: self._choose_path(name, title, filters), style='Compact.TButton').pack(side='right')
        entry = self._entry(row, self.paths[name], width=27)
        entry.pack(side='right', fill='x', expand=True, ipady=self.px(3), padx=self.px(5))
        entry.bind('<KeyRelease>', self._inputs_changed)

    def _choose_path(self, name, title, filters):
        value = filedialog.askopenfilename(parent=self, title=title, filetypes=filters)
        if value:
            self.paths[name].set(value)
            self._inputs_changed()

    def _inputs_changed(self, event=None):
        self.gyro_status_var.set('陀螺仪数据：输入已变化，待重新检测')
        self.lens_status_var.set('镜头配置：输入已变化，待重新检测')

    def _path_token(self):
        return tuple(variable.get().strip() for variable in self.paths.values())

    def _install_scroll(self, parent):
        tag = f'GyroflowScroll{self.winfo_id()}'
        self.bind_class(tag, '<MouseWheel>', self._wheel)
        def install(widget):
            if not isinstance(widget, (tk.Entry, ttk.Combobox)):
                widget.bindtags((tag,)+widget.bindtags())
            for child in widget.winfo_children(): install(child)
        install(parent)
        self.canvas.bind('<MouseWheel>', self._wheel)

    def _wheel(self, event):
        amount = -int(event.delta/120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(amount, 'units')
        return 'break'

    def _existing_settings(self):
        getter = getattr(self.ws, 'get_selected_stabilization', None)
        value = getter() if callable(getter) else None
        value = value if isinstance(value, StabilizationSettings) else _get(value, 'settings')
        if isinstance(value, StabilizationSettings): return copy.deepcopy(value)
        if isinstance(value, dict):
            try: return StabilizationSettings(**value)
            except (ValueError, TypeError): pass
        return default_settings()

    def _load(self, settings):
        self._loading = True
        try:
            self.mode_var.set(settings.mode)
            for name, variable in self.variables.items(): variable.set(f'{float(getattr(settings, name)):g}')
            for name, variable in self.paths.items(): variable.set(getattr(settings, name))
            self.zoom_var.set(next(label for label, value in ZOOM_NAMES.items() if value == settings.zoom_mode))
            self.auto_sync_var.set(bool(settings.auto_sync))
        finally:
            self._loading = False
        self._mode_changed()

    def _mode_changed(self):
        manual = self.mode_var.get() == 'manual'
        for widget, state in self._manual_widgets:
            widget.configure(state=state if manual else 'disabled')
        self.status_var.set('手动参数将用于所选片段；开始时检查实际运动数据与镜头。' if manual else
                            '自动模式使用推荐平滑与缩放参数；开始时由 Gyroflow 检查陀螺仪与镜头。')

    def _describe_target(self):
        selected = self.selected_range
        if len(selected) != 2 or selected[1] <= selected[0]:
            self._target_error = '请先在节目时间线上选择一个 Cut 片段。'
            self.target_var.set('Gyroflow 防抖 · 未选择片段')
            self.source_var.set(self._target_error)
            self.start_button.configure(state='disabled')
            return
        first, last = selected
        spans = [span for span in program_spans(self.ws.project) if span.start < last-1e-8 and span.end > first+1e-8]
        cameras = {span.camera for span in spans}
        camera = next(iter(cameras)) if len(cameras) == 1 else None
        self.target_var.set(f'仅所选片段 · {format_time(first)} — {format_time(last)}'
                            + (f' · 机位 {camera}' if camera is not None else ' · 多机位范围'))
        if camera is None:
            self._target_error = '此范围包含多个机位，请选择单个 Cut 片段后设置防抖。'
            self.source_var.set(self._target_error)
            self.start_button.configure(state='disabled')
            return
        clips = [clip for clip in self.ws.project.clips if clip.camera == camera and clip.start < last and clip.end > first]
        if not clips:
            self._target_error = '所选机位在这一范围没有视频素材。'
            self.source_var.set(self._target_error)
            self.start_button.configure(state='disabled')
        elif len(clips) == 1:
            clip = clips[0]
            self.source_var.set(f'{Path(clip.path).name} · 原片 {format_time(max(0, first-clip.start))}'
                                f' — {format_time(min(clip.duration, last-clip.start))}')
        else:
            self.source_var.set(f'所选范围覆盖 {len(clips)} 个原片文件；开始时按实际素材范围检查。')

    def auto_configure(self):
        paths = {name: variable.get().strip() for name, variable in self.paths.items()}
        self._load(replace(default_settings(mode='auto'), **paths))
        self.request_probe()

    def request_probe(self):
        if self._closed or self._probe_pending: return
        callback = getattr(self.ws, 'probe_selected_stabilization', None)
        if not callable(callback):
            self.status_var.set('推荐参数已就绪；开始防抖时由 Gyroflow 实际检查陀螺仪与镜头，当前尚未完成检测。')
            return
        self._probe_pending = True
        self.probe_button.configure(state='disabled')
        self.status_var.set('正在检查所选原片的陀螺仪和镜头配置…')
        try:
            # A workspace callback must return results on Tk's thread.
            settings = self.read_settings()
            self._probe_token = self._path_token()
            token = self._probe_token
            result = callback(lambda info: self.show_probe_result(info, token=token), settings=settings)
            if isinstance(result, dict): self.show_probe_result(result, token=token)
            elif result is False:
                self.show_probe_result({'error': '当前无法检测，请等待正在运行的任务完成。'})
        except Exception as error:
            self.show_probe_result({'error': str(error)})

    def show_probe_result(self, info, *, token=None):
        if self._closed or not self.winfo_exists(): return
        self._probe_pending = False
        self.probe_button.configure(state='normal')
        if token is not None and token != self._path_token():
            self._inputs_changed()
            self.status_var.set('输入数据已变化，本次检查结果已过期，请重新检查。')
            return
        error = _get(info, 'error', '')
        if error:
            self.status_var.set(str(error))
            self.gyro_status_var.set('陀螺仪数据：检测未完成 / 不可用')
            self.lens_status_var.set('镜头配置：检测未完成 / 不可用')
            return
        has_gyro = _get(info, 'has_gyro', None)
        has_lens = _get(info, 'has_lens', None)
        samples = _get(info, 'motion_samples', 0)
        gyro_detail = f'已检测到 · {samples} 条样本' if has_gyro is True and samples else '已检测到'
        lens_name = _get(info, 'lens_name', '')
        lens_detail = '已检测到' + (f' · {lens_name}' if lens_name else '')
        self.gyro_status_var.set('陀螺仪数据：' + (gyro_detail if has_gyro is True else '未检测到，需要指定有效运动数据' if has_gyro is False else '检测结果未确认'))
        self.lens_status_var.set('镜头配置：' + (lens_detail if has_lens is True else '未检测到，需要指定匹配镜头配置' if has_lens is False else '检测结果未确认'))
        problems = _get(info, 'problems', ())
        message = _get(info, 'message', '') or ('；'.join(str(item) for item in problems) if problems else '')
        self.status_var.set(str(message or '数据检查完成；开始时仍会验证实际 Gyroflow 同步与防抖结果。'))

    def read_settings(self, *, check_files=True):
        numbers = {}
        for name, variable in self.variables.items():
            try:
                value = float(variable.get().strip())
                if not math.isfinite(value): raise ValueError
            except (ValueError, TypeError, tk.TclError):
                raise ValueError(f'{FIELD_LABELS[name]}必须是有效数字。') from None
            numbers[name] = value
        settings = StabilizationSettings(mode=self.mode_var.get(), **numbers,
                    zoom_mode=ZOOM_NAMES.get(self.zoom_var.get(), ''), auto_sync=self.auto_sync_var.get(),
                    **{name: variable.get().strip() for name, variable in self.paths.items()})
        validate_settings(settings, check_files=check_files)
        return settings

    def start(self):
        if self._target_error:
            messagebox.showerror('Gyroflow 防抖', self._target_error, parent=self); return
        if tuple(self.ws.selected_program_range or ()) != self.selected_range:
            messagebox.showerror('Gyroflow 防抖', '所选片段已变化，请关闭窗口并重新选择需要防抖的片段。', parent=self); return
        try:
            settings = self.read_settings()
            result = self.ws.start_stabilization(settings)
            if result is not False: self.close()
        except Exception as error:
            messagebox.showerror('Gyroflow 防抖', str(error), parent=self)

    def remove(self):
        if tuple(self.ws.selected_program_range or ()) != self.selected_range:
            messagebox.showerror('Gyroflow 防抖', '所选片段已变化，请关闭窗口并重新选择。', parent=self); return
        callback = getattr(self.ws, 'remove_selected_stabilization', None)
        if callable(callback) and callback() is not False: self.close()

    def close(self):
        if self._closed: return
        self._closed = True
        try: self.grab_release()
        except tk.TclError: pass
        if getattr(self.ws, '_stabilization_dialog', None) is self:
            self.ws._stabilization_dialog = None
        self.destroy()


def open_stabilization_dialog(workspace, mode=None):
    """Show this selection's dialog; caller owns the context-menu selection."""
    available = getattr(workspace, '_available', None)
    if callable(available) and not available(): return None
    current = getattr(workspace, '_stabilization_dialog', None)
    if current is not None and current.winfo_exists():
        current.lift()
        return current
    dialog = StabilizationDialog(workspace, mode=mode)
    workspace._stabilization_dialog = dialog
    return dialog
