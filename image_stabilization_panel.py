"""Selected-cut image-motion stabilization settings; no gyro data required."""
from __future__ import annotations

import copy
from dataclasses import replace
import math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from editor_core import format_time
from gyroflow_core import StabilizationSettings, validate_settings
from program_edits import program_spans


BG, PANEL, INPUT = '#202124', '#292b2e', '#18191b'
INK, MUTED, ACCENT = '#dedfe3', '#969ba3', '#58a6d6'
MODEL_NAMES = {'相似（平移 / 旋转）': 'similarity', '仅平移': 'translation'}
QUALITY_NAMES = {'快速（推荐）': 'fast', '精细': 'fine'}


def image_defaults(mode='auto'):
    return StabilizationSettings(mode=mode, engine='image', image_smoothing=.5,
        image_shakiness=5, image_zoom=True, image_tripod=False, image_model='similarity', image_quality='fast')


class ImageStabilizationDialog(tk.Toplevel):
    def __init__(self, workspace, *, mode=None):
        super().__init__(workspace.winfo_toplevel())
        self.withdraw()
        self.ws = workspace
        self._closed = False
        self.ui_scale = max(1., float(getattr(workspace, 'ui_scale', 1.)))
        self.selected_range = tuple(workspace.selected_program_range or ())
        self._target_error = ''
        self._target_token = None
        self._manual_widgets = []
        self.configure(bg=BG)
        self.title('画面防抖 · 所选片段')
        self.transient(workspace.winfo_toplevel())
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.mode_var = tk.StringVar(self, value='auto')
        self.model_var = tk.StringVar(self)
        self.quality_var = tk.StringVar(self)
        self.variables = {name: tk.StringVar(self) for name in ('image_smoothing', 'image_shakiness')}
        self.zoom_var = tk.BooleanVar(self, value=True)
        self.tripod_var = tk.BooleanVar(self, value=False)
        self.target_var = tk.StringVar(self)
        self.source_var = tk.StringVar(self)
        self.status_var = tk.StringVar(self)
        self._build()
        settings = self._existing_settings()
        if mode == 'auto':
            settings = image_defaults()
        elif mode == 'manual':
            settings = replace(settings, mode='manual')
        self._load(settings)
        self._describe_target()
        self.bind('<Escape>', lambda event: self.close())
        self.update_idletasks()
        width = min(self.px(650), max(self.px(440), self.winfo_screenwidth()-self.px(60)))
        height = min(self.px(650), max(self.px(360), self.winfo_screenheight()-self.px(90)))
        root = workspace.winfo_toplevel()
        x = max(0, root.winfo_rootx()+(root.winfo_width()-width)//2)
        y = max(0, root.winfo_rooty()+(root.winfo_height()-height)//2)
        self.geometry(f'{width}x{height}+{x}+{y}')
        self.minsize(min(width, self.px(470)), min(height, self.px(350)))
        self.deiconify()
        self.grab_set()
        self.focus_set()

    def px(self, value):
        return max(1, round(value*self.ui_scale))

    def _label(self, parent, text=None, variable=None, *, color=MUTED, bold=False):
        return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget('bg'), fg=color,
            anchor='w', justify='left', font=('Microsoft YaHei UI', 9, 'bold' if bold else 'normal'))

    def _build(self):
        header = tk.Frame(self, bg=PANEL, padx=self.px(14), pady=self.px(10))
        header.pack(fill='x')
        target = self._label(header, variable=self.target_var, color=INK, bold=True)
        target.pack(fill='x')
        source = self._label(header, variable=self.source_var)
        source.pack(fill='x', pady=(self.px(4), 0))
        header.bind('<Configure>', lambda event: [label.configure(wraplength=max(1, event.width-self.px(30)))
                                                 for label in (target, source)])
        self.footer = tk.Frame(self, bg=PANEL, padx=self.px(12), pady=self.px(9))
        self.footer.pack(side='bottom', fill='x')
        status = self._label(self.footer, variable=self.status_var, color=ACCENT)
        status.pack(fill='x', pady=(0, self.px(8)))
        self.footer.bind('<Configure>', lambda event: status.configure(wraplength=max(1, event.width-self.px(28))))
        actions = tk.Frame(self.footer, bg=PANEL)
        actions.pack(fill='x')
        self.close_button = ttk.Button(actions, text='关闭', command=self.close, style='Compact.TButton')
        self.close_button.pack(side='right', padx=self.px(3))
        self.start_button = ttk.Button(actions, text='开始画面防抖', command=self.start, style='Compact.TButton')
        self.start_button.pack(side='right', padx=self.px(3))
        self.remove_button = ttk.Button(actions, text='移除所选片段防抖', command=self.remove, style='Compact.TButton')
        self.remove_button.pack(side='left', padx=self.px(3))
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, width=1, height=1,
                                yscrollincrement=self.px(20))
        self.scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.scrollbar.pack(side='right', fill='y')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=BG)
        self._window = self.canvas.create_window(0, 0, anchor='nw', window=self.body)
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(self._window, width=max(1, event.width)))
        self.body.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))

        self._note('基于画面特征匹配，双遍分析与运动补偿；无需陀螺仪或镜头配置。', color=INK)
        modes = tk.Frame(self.body, bg=BG)
        modes.pack(fill='x', padx=self.px(14), pady=self.px(9))
        for title, value in (('自动推荐', 'auto'), ('手动设置', 'manual')):
            tk.Radiobutton(modes, text=title, value=value, variable=self.mode_var,
                command=lambda chosen=value: self.auto_configure() if chosen == 'auto' else self._mode_changed(),
                bg=BG, fg=INK, selectcolor=INPUT, activebackground=BG, activeforeground=INK,
                font=('Microsoft YaHei UI', 9)).pack(side='left', padx=self.px(5))
        self.auto_button = ttk.Button(self.body, text='恢复自动推荐参数', command=self.auto_configure,
                                     style='Compact.TButton')
        self.auto_button.pack(anchor='w', padx=self.px(18), pady=(0, self.px(12)))
        row = self._row('处理质量')
        self.quality_combo = ttk.Combobox(row, textvariable=self.quality_var, values=list(QUALITY_NAMES),
                                        state='readonly', width=24)
        self.quality_combo.pack(side='right')
        self._note('快速使用双线性插值，处理更快，画面可能略柔；精细使用双三次插值，处理较慢。两者均保留最终分辨率。')
        row = self._row('运动模型')
        self.model_combo = ttk.Combobox(row, textvariable=self.model_var, values=list(MODEL_NAMES),
                                        state='readonly', width=24)
        self.model_combo.pack(side='right')
        self._manual_widgets.append((self.model_combo, 'readonly'))
        self._number_row('image_smoothing', '平滑时长（秒）')
        self._note('0–5 秒；数值越大越平稳，也会修正更多原有镜头运动。')
        self._number_row('image_shakiness', '抖动程度（1–10）')
        self._note('轻微手持先用 5；更明显的抖动可以逐步调高。')
        for title, variable in (('自动裁边缩放', self.zoom_var), ('锁定机位（摄影机锁定）', self.tripod_var)):
            check = tk.Checkbutton(self.body, text=title, variable=variable, bg=BG, fg=INK,
                selectcolor=INPUT, activebackground=BG, activeforeground=INK, font=('Microsoft YaHei UI', 9))
            check.pack(anchor='w', padx=self.px(16), pady=self.px(5))
            self._manual_widgets.append((check, 'normal'))
        self._note('锁定机位适合固定构图；跟拍或摇镜头时保持关闭。人物或前景占满画面时，'
                   '可降低抖动程度、缩短平滑时长，再检查预览。')
        self._note('画面防抖使用图像运动分析，滤镜内部为8位；10位/HDR素材需要保留精度时优先用Gyroflow。')
        self._install_scroll()

    def _note(self, text, *, color=MUTED):
        label = self._label(self.body, text, color=color)
        label.pack(fill='x', padx=self.px(18), pady=self.px(7))
        self.body.bind('<Configure>', lambda event: label.configure(wraplength=max(1, event.width-self.px(38))), add='+')

    def _row(self, title):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill='x', padx=self.px(18), pady=self.px(7))
        self._label(row, title).pack(side='left', padx=(0, self.px(8)))
        return row

    def _number_row(self, name, title):
        row = self._row(title)
        entry = tk.Entry(row, textvariable=self.variables[name], width=12, bg=INPUT, fg=INK,
            insertbackground=INK, relief='flat', highlightthickness=1, highlightbackground='#404247',
            highlightcolor=ACCENT, font=('Cascadia Mono', 9))
        entry.pack(side='right', ipady=self.px(3))
        self._manual_widgets.append((entry, 'normal'))

    def _install_scroll(self):
        self._scroll_tag = f'ImageStabilizationScroll{self.winfo_id()}'
        self.bind_class(self._scroll_tag, '<MouseWheel>', self._wheel)
        def install(widget):
            if not isinstance(widget, (tk.Entry, ttk.Combobox)):
                widget.bindtags((self._scroll_tag,)+widget.bindtags())
            for child in widget.winfo_children():
                install(child)
        install(self.body)
        self.canvas.bind('<MouseWheel>', self._wheel)

    def _wheel(self, event):
        delta = event.delta
        if delta:
            self.canvas.yview_scroll(-int(delta/120) if abs(delta) >= 120 else (-1 if delta > 0 else 1), 'units')
        return 'break'

    def _existing_settings(self):
        getter = getattr(self.ws, 'get_selected_stabilization', None)
        record = getter() if callable(getter) else None
        value = record if isinstance(record, StabilizationSettings) else (
            record.get('settings') if isinstance(record, dict) else getattr(record, 'settings', None))
        if isinstance(value, dict):
            try: value = StabilizationSettings(**value)
            except (TypeError, ValueError): value = None
        if isinstance(value, StabilizationSettings) and value.engine == 'image':
            return copy.deepcopy(value)
        return image_defaults()

    def _load(self, settings):
        self.mode_var.set(settings.mode)
        self.variables['image_smoothing'].set(f'{settings.image_smoothing:g}')
        self.variables['image_shakiness'].set(str(settings.image_shakiness))
        self.model_var.set(next((title for title, model in MODEL_NAMES.items() if model == settings.image_model), ''))
        self.quality_var.set(next((title for title, quality in QUALITY_NAMES.items() if quality == settings.image_quality), ''))
        self.zoom_var.set(settings.image_zoom)
        self.tripod_var.set(settings.image_tripod)
        self._mode_changed()

    def _mode_changed(self):
        manual = self.mode_var.get() == 'manual'
        for widget, state in self._manual_widgets:
            widget.configure(state=state if manual else 'disabled')
        self.status_var.set('手动参数仅用于上方所选 Cut；先分析画面运动，再生成防抖缓存。' if manual else
                            '自动模式仅处理上方所选 Cut；可恢复推荐参数，或切换手动调整。')

    def auto_configure(self):
        self._load(image_defaults())

    def _selection_token(self):
        selected = tuple(self.ws.selected_program_range or ())
        if len(selected) != 2: return selected, (), ()
        first, last = selected
        spans = tuple((span.start, span.end, span.camera) for span in program_spans(self.ws.project)
                      if span.start < last-1e-8 and span.end > first+1e-8)
        cameras = {span[2] for span in spans}
        clips = tuple(sorted((clip.id, clip.camera, clip.path, clip.start, clip.duration)
            for clip in self.ws.project.clips if clip.camera in cameras and clip.start < last and clip.end > first))
        return selected, spans, clips

    def _describe_target(self):
        first, last = self.selected_range if len(self.selected_range) == 2 else (0., 0.)
        if (not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (first, last))
                or first < 0 or last <= first or last > self.ws.project.duration+1e-6):
            self._target_error = '请先在节目时间线上选择一个 Cut 片段。'
        else:
            self._target_token = self._selection_token()
            cameras = {span[2] for span in self._target_token[1]}
            if len(cameras) != 1:
                self._target_error = '请选中单个机位的 Cut 片段后设置画面防抖。'
            elif not self._target_token[2]:
                self._target_error = '所选范围没有可防抖的视频画面。'
        if self._target_error:
            self.target_var.set('画面防抖 · 未选择有效片段')
            self.source_var.set(self._target_error)
            self.start_button.configure(state='disabled')
            self.remove_button.configure(state='disabled')
            return
        camera = next(iter(cameras))
        self.target_var.set(f'仅所选片段 · {format_time(first)} — {format_time(last)} · 机位 {camera}')
        clips = self._target_token[2]
        self.source_var.set(Path(clips[0][2]).name if len(clips) == 1 else f'覆盖 {len(clips)} 个同机位原片文件')

    def read_settings(self):
        try:
            smoothing = float(self.variables['image_smoothing'].get().strip())
            shakiness = float(self.variables['image_shakiness'].get().strip())
        except (ValueError, TypeError, tk.TclError):
            raise ValueError('平滑时长和抖动程度必须是有效数字。') from None
        if not math.isfinite(smoothing) or not 0 <= smoothing <= 5:
            raise ValueError('平滑时长必须在 0 到 5 秒之间。')
        if not math.isfinite(shakiness) or not shakiness.is_integer() or not 1 <= shakiness <= 10:
            raise ValueError('抖动程度必须是 1 到 10 的整数。')
        settings = StabilizationSettings(mode=self.mode_var.get(), engine='image',
            image_smoothing=smoothing, image_shakiness=int(shakiness),
            image_zoom=self.zoom_var.get(), image_tripod=self.tripod_var.get(),
            image_model=MODEL_NAMES.get(self.model_var.get(), ''),
            image_quality=QUALITY_NAMES.get(self.quality_var.get(), ''))
        validate_settings(settings, check_files=False)
        return settings

    def _current_target(self):
        if self._target_error:
            messagebox.showerror('画面防抖', self._target_error, parent=self)
            return False
        if tuple(self.ws.selected_program_range or ()) != self.selected_range or self._selection_token() != self._target_token:
            messagebox.showerror('画面防抖', '所选片段或机位已变化，请关闭窗口并重新选择需要防抖的片段。', parent=self)
            return False
        return True

    def start(self):
        if self._closed or not self._current_target(): return
        try:
            settings = self.read_settings()
            if self.ws.start_stabilization(settings) is not False:
                self.close()
        except Exception as error:
            messagebox.showerror('画面防抖', str(error), parent=self)

    def remove(self):
        if self._closed or not self._current_target(): return
        if self.ws.remove_selected_stabilization() is not False:
            self.close()

    def close(self):
        if self._closed: return
        self._closed = True
        try:
            self.grab_release()
            self.unbind_class(self._scroll_tag, '<MouseWheel>')
        except tk.TclError: pass
        if getattr(self.ws, '_image_stabilization_dialog', None) is self:
            self.ws._image_stabilization_dialog = None
        self.destroy()


def open_image_stabilization_dialog(workspace, mode=None):
    available = getattr(workspace, '_available', None)
    if callable(available) and not available(): return None
    current = getattr(workspace, '_image_stabilization_dialog', None)
    if current is not None and current.winfo_exists():
        current.lift()
        return current
    dialog = ImageStabilizationDialog(workspace, mode=mode)
    workspace._image_stabilization_dialog = dialog
    return dialog
