"""Selection-based primary colour wheels for the native multicamera workspace.

The wheel canvas only edits a draft during a gesture.  A release sends one
grade to the workspace, which owns undo, selected-range persistence and render
invalidation.  No global shortcuts are bound here: the editor owns those.
"""
from __future__ import annotations

import colorsys
import copy
import math
import tkinter as tk
from tkinter import messagebox, ttk

from creative_core import ColorGrade, replace_wheel, wheel_values, WHEEL_RANGES
from editor_core import format_time, parse_timecode
from program_edits import program_spans
from color_curve_editor import CurveEditor
from auto_color_panel import AutoColorPanel


BG, PANEL, INPUT = '#202124', '#292b2e', '#18191b'
INK, MUTED, ACCENT = '#dedfe3', '#969ba3', '#58a6d6'
WHEELS = (('lift', 'Lift · 暗部'), ('gamma', 'Gamma · 中间调'),
          ('gain', 'Gain · 亮部'), ('offset', 'Offset · 整体'))
CAMERA_COLORS = ('#496e87', '#4b8289', '#667e9e', '#64867c', '#7c718d',
                 '#7e8870', '#807a65', '#557f92', '#8c7374')


def grade_target_label(workspace, target):
    if target == 'selection':
        selected = getattr(workspace, 'selected_program_range', None)
        if not selected:
            return '所选片段'
        cameras = {span.camera for span in program_spans(workspace.project)
                   if span.start < selected[1] - 1e-8 and span.end > selected[0] + 1e-8}
        suffix = f'机位 {next(iter(cameras))}' if len(cameras) == 1 else '多机位'
        return f'所选片段 · {suffix}'
    return '全片' if target is None else f'整机位 {target}'


def grade_target_from_label(text):
    if text.startswith('所选片段'):
        return 'selection'
    if text == '全片':
        return None
    if text.startswith(('整机位 ', '机位 ')):
        return int(text.split()[-1])
    raise ValueError('请选择有效的调色目标。')


def balance_from_point(x: float, y: float) -> tuple[float, float, float]:
    """Map a unit wheel position to zero-mean RGB offsets, bounded by ±0.5."""
    radius = min(1.0, math.hypot(x, y))
    hue = (math.atan2(-y, x) / (2.0 * math.pi)) % 1.0
    raw = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
    mean = sum(raw) / 3.0
    return tuple((component - mean) * 0.75 * radius for component in raw)


def point_from_balance(r: float, g: float, b: float) -> tuple[float, float]:
    """Show the chromatic part of independently entered RGB values on a wheel."""
    lower, upper = min(r, g, b), max(r, g, b)
    if upper - lower < 1e-12:
        return 0.0, 0.0
    hue, _, _ = colorsys.rgb_to_hsv(r - lower, g - lower, b - lower)
    radius = min(1.0, (upper - lower) / 0.75)
    return math.cos(hue * math.tau) * radius, -math.sin(hue * math.tau) * radius


def _wheel_ppm(size: int) -> bytes:
    """Generate one reusable, display-resolution wheel without external assets."""
    radius = max(1.0, (size - 4) / 2.0)
    center = (size - 1) / 2.0
    pixels = bytearray()
    for y in range(size):
        dy = (y - center) / radius
        for x in range(size):
            dx = (x - center) / radius
            distance = math.hypot(dx, dy)
            if distance > 1.0:
                pixels.extend((41, 43, 46))
            else:
                hue = (math.atan2(-dy, dx) / math.tau) % 1.0
                rgb = colorsys.hsv_to_rgb(hue, distance ** 0.75, 0.82)
                pixels.extend(round(component * 255) for component in rgb)
    return f'P6\n{size} {size}\n255\n'.encode('ascii') + pixels


class PrimaryWheel(tk.Frame):
    def __init__(self, parent, owner, name, title):
        super().__init__(parent, bg=PANEL, highlightthickness=1,
                         highlightbackground='#37393d')
        self.owner, self.name = owner, name
        self.loading = False
        self.grade = ColorGrade()
        self.draft = None
        self._gesture_target = None
        self._entry_target = None
        self._y_gesture = False
        self._radius = 1.0
        self._center = (1.0, 1.0)
        self._photo = None
        self.values = {key: tk.StringVar(self, value='0.000') for key in 'rgby'}
        self.y_value = tk.DoubleVar(self, value=0.0)
        header = tk.Frame(self, bg=PANEL)
        header.pack(fill='x', padx=self.px(9), pady=(self.px(6), 0))
        tk.Label(header, text=title, bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left')
        ttk.Button(header, text='重置', width=5, command=self.reset,
                   style='Compact.TButton').pack(side='right')
        self.canvas = tk.Canvas(self, bg=PANEL, highlightthickness=0,
                                width=self.px(170), height=self.px(158),
                                cursor='crosshair', takefocus=True)
        self.canvas.pack(fill='both', expand=True, padx=self.px(4))
        self.canvas.bind('<Configure>', self._resize)
        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._motion)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self.canvas.bind('<Double-Button-1>', self._double_click)
        self.canvas.bind('<FocusOut>', self._focus_out)
        yrow = tk.Frame(self, bg=PANEL)
        yrow.pack(fill='x', padx=self.px(7), pady=self.px(2))
        tk.Label(yrow, text='Y', bg=PANEL, fg=INK,
                 font=('Cascadia Mono', 9)).pack(side='left')
        low, high = WHEEL_RANGES[f'{name}_y']
        self.y_scale = tk.Scale(yrow, from_=low, to=high, resolution=0.005,
                               orient='horizontal', showvalue=False, variable=self.y_value,
                               bg=PANEL, fg=INK, troughcolor=INPUT, highlightthickness=0,
                               activebackground=ACCENT, sliderrelief='flat', bd=0,
                               length=self.px(120), sliderlength=self.px(15),
                               command=self._y_changed)
        self.y_scale.pack(side='left', fill='x', expand=True, padx=self.px(3))
        self.y_scale.bind('<ButtonPress-1>', self._begin_y)
        self.y_scale.bind('<KeyPress>', self._begin_y)
        self.y_scale.bind('<FocusOut>', self._focus_out)
        self._entry(yrow, 'y', width=6).pack(side='right')
        numbers = tk.Frame(self, bg=PANEL)
        numbers.pack(fill='x', padx=self.px(7), pady=(0, self.px(8)))
        for index, (key, color) in enumerate((('r', '#df7770'), ('g', '#83b692'), ('b', '#75a1d2'))):
            cell = tk.Frame(numbers, bg=PANEL)
            cell.grid(row=0, column=index, sticky='ew', padx=self.px(2))
            numbers.columnconfigure(index, weight=1)
            tk.Label(cell, text=key.upper(), bg=PANEL, fg=color,
                     font=('Cascadia Mono', 8)).pack(side='left')
            self._entry(cell, key, width=6).pack(side='left', fill='x', expand=True)

    def px(self, value):
        return self.owner.px(value)

    def _entry(self, parent, key, width):
        entry = tk.Entry(parent, textvariable=self.values[key], width=width, bg=INPUT,
                         fg=INK, insertbackground=INK, relief='flat',
                         highlightthickness=1, highlightbackground='#404247',
                         highlightcolor=ACCENT, font=('Cascadia Mono', 9))
        entry.bind('<Return>', self.commit_entries)
        entry.bind('<FocusIn>', self._entry_focus)
        entry.bind('<FocusOut>', self._focus_out)
        entry.bind('<KeyRelease>', self._preview_entries)
        return entry

    def _resize(self, event):
        size = max(12, min(event.width - self.px(6), event.height - self.px(6)))
        self._center = (event.width / 2.0, event.height / 2.0)
        self._radius = max(1.0, (size - 4) / 2.0)
        self._photo = self.owner.wheel_image(size)
        self.canvas.delete('all')
        cx, cy = self._center
        self.canvas.create_image(cx, cy, image=self._photo)
        self.canvas.create_oval(cx - self._radius, cy - self._radius,
                                cx + self._radius, cy + self._radius, outline='#686b70')
        self.canvas.create_line(cx - self.px(4), cy, cx + self.px(4), cy, fill='#777a7f')
        self.canvas.create_line(cx, cy - self.px(4), cx, cy + self.px(4), fill='#777a7f')
        self._draw_cursor()

    def _draw_cursor(self):
        self.canvas.delete('cursor')
        values = wheel_values(self.draft or self.grade, self.name)
        x, y = point_from_balance(values['r'], values['g'], values['b'])
        cx = self._center[0] + x * self._radius
        cy = self._center[1] + y * self._radius
        radius = self.px(4)
        self.canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                                outline=INK, fill=INPUT, width=max(1, self.px(1)), tags='cursor')

    def load(self, grade):
        self.loading = True
        self.grade = copy.deepcopy(grade)
        self.draft = None
        self._gesture_target = None
        self._entry_target = None
        self._y_gesture = False
        for key, value in wheel_values(grade, self.name).items():
            self.values[key].set(f'{value:.3f}')
        self.y_value.set(float(getattr(grade, f'{self.name}_y')))
        self.loading = False
        self._draw_cursor()

    def _show_draft(self):
        if self.draft is None:
            return
        for key, value in wheel_values(self.draft, self.name).items():
            self.values[key].set(f'{value:.3f}')
        self._draw_cursor()

    def _point_draft(self, event):
        x = (event.x - self._center[0]) / self._radius
        y = (event.y - self._center[1]) / self._radius
        rgb = balance_from_point(x, y)
        values = wheel_values(self.draft or self.grade, self.name)
        # A low gamma/gain Y reduces available negative channel headroom.
        lower = -0.95 - values['y'] if self.name == 'gamma' else -1.0 - values['y']
        if self.name in ('gamma', 'gain'):
            rgb = tuple(max(lower, channel) for channel in rgb)
        self.draft = replace_wheel(self.draft or self.grade, self.name,
                                   r=rgb[0], g=rgb[1], b=rgb[2], y=values['y'])
        self._show_draft()
        self.owner.preview_grade(self.draft)

    def _press(self, event):
        if not self.owner.available():
            return 'break'
        self.owner.cancel_drafts()
        self.canvas.focus_set()
        self._gesture_target = self.owner.target_token()
        self.draft = self.owner.get_grade()
        self._point_draft(event)
        return 'break'

    def _motion(self, event):
        if self.draft is not None:
            self._point_draft(event)
        return 'break'

    def _release(self, event=None):
        if self.draft is not None:
            self._commit(self.draft)
        return 'break'

    def _double_click(self, event):
        self.draft = None
        self.reset()
        return 'break'

    def _y_changed(self, value):
        if self.loading:
            return
        self.values['y'].set(f'{float(value):.3f}')
        if self._y_gesture:
            self._preview_entries()

    def _begin_y(self, event=None):
        if not self.owner.available():
            return
        self.owner.cancel_drafts()
        self._gesture_target = self.owner.target_token()
        self._y_gesture = True
        self.draft = self.owner.get_grade()

    def _entry_focus(self, event=None):
        self._entry_target = self.owner.target_token()

    def _focus_out(self, event=None):
        if self.draft is not None:
            self.owner.cancel_drafts()

    def _preview_entries(self, event=None):
        if self.loading or not self.owner.available():
            return
        if event is not None and getattr(event, 'keysym', '') in ('Return', 'Tab', 'Escape'):
            return
        target = self._entry_target if self._entry_target is not None else self._gesture_target
        if target is not None and target != self.owner.target_token():
            self.owner.cancel_drafts()
            return
        try:
            values = {key: float(variable.get()) for key, variable in self.values.items()}
            if not all(math.isfinite(value) for value in values.values()):
                return
            self.draft = replace_wheel(self.owner.get_grade(), self.name, **values)
            self._gesture_target = self.owner.target_token()
            self._draw_cursor()
            self.owner.preview_grade(self.draft)
        except (ValueError, TypeError, tk.TclError):
            pass  # Incomplete numeric input is expected while typing.

    def commit_y(self, event=None):
        self.values['y'].set(f'{self.y_value.get():.3f}')
        result = self.commit_entries(event)
        self._y_gesture = False
        return result

    def commit_entries(self, event=None):
        if self.loading or not self.owner.available():
            return 'break'
        target = self._entry_target if self._entry_target is not None else self._gesture_target
        if target is not None and target != self.owner.target_token():
            self.owner.cancel_drafts()
            return 'break'
        try:
            current = self.owner.get_grade()
            shown = wheel_values(current, self.name)
            if all(self.values[key].get() == f'{value:.3f}' for key, value in shown.items()):
                return None
            values = {key: float(variable.get()) for key, variable in self.values.items()}
            if not all(math.isfinite(value) for value in values.values()):
                raise ValueError('调色数值必须为有限数字。')
            grade = replace_wheel(current, self.name, **values)
            self._commit(grade)
        except (TypeError, ValueError, tk.TclError) as error:
            self.owner.show_error(error)
            self.load(self.owner.get_grade())
        return 'break' if event is not None and getattr(event, 'keysym', '') == 'Return' else None

    def _commit(self, grade):
        if self._gesture_target is not None and self._gesture_target != self.owner.target_token():
            self.owner.cancel_drafts()
            return
        self.draft = None
        self._gesture_target = None
        try:
            if grade != self.owner.get_grade():
                self.owner.apply_grade(copy.deepcopy(grade))
            else:
                self.owner.clear_preview()
            self.owner.refresh()
        except (TypeError, ValueError, tk.TclError) as error:
            self.owner.show_error(error)
            self.owner.refresh()

    def reset(self):
        if self.owner.available():
            self._commit(replace_wheel(self.owner.get_grade(), self.name,
                                      r=0.0, g=0.0, b=0.0, y=0.0))


class ColorWorkspace(tk.Frame):
    """Bottom colour page, replacing the editing timeline while it is active."""
    def __init__(self, parent, workspace):
        super().__init__(parent, bg=BG)
        self.ws = workspace
        self._images = {}
        self._columns = 0
        self._drag = None
        self._draft_range = None
        self._refreshing = False
        self._span_records = []
        self._active_target_token = None
        self.scope_var = tk.StringVar(self, value='所选片段')
        self.range_start_var = tk.StringVar(self, value=format_time(0))
        self.range_end_var = tk.StringVar(self, value=format_time(0))
        self.selection_var = tk.StringVar(self, value='点击片段或拖动选择调色范围')
        header = tk.Frame(self, bg=PANEL)
        header.pack(fill='x')
        tk.Label(header, text='调色 · COLOR', bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=self.px(10), pady=self.px(6))
        tk.Label(header, text='目标', bg=PANEL, fg=MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(self.px(3), self.px(4)))
        self.scope_combo = ttk.Combobox(header, textvariable=self.scope_var,
                                         state='readonly', width=18)
        self.scope_combo.pack(side='left', padx=self.px(3))
        self.scope_combo.bind('<<ComboboxSelected>>', self._scope_changed)
        ttk.Button(header, text='返回剪辑', command=workspace.exit_color_workspace,
                   style='Compact.TButton').pack(side='right', padx=self.px(6))
        ttk.Button(header, text='预览当前效果', command=workspace.preview_effects,
                   style='Compact.TButton').pack(side='right', padx=self.px(3))
        ttk.Button(header, text='剪切 C', command=workspace.split_program_at_playhead,
                   style='Compact.TButton').pack(side='right', padx=self.px(3))
        selection_row = tk.Frame(self, bg=BG)
        selection_row.pack(fill='x', padx=self.px(8), pady=self.px(3))
        tk.Label(selection_row, text='范围', bg=BG, fg=MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(side='left')
        for label, variable in (('入', self.range_start_var), ('出', self.range_end_var)):
            tk.Label(selection_row, text=label, bg=BG, fg=MUTED,
                     font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(self.px(8), self.px(3)))
            entry = tk.Entry(selection_row, textvariable=variable, width=13, bg=INPUT,
                             fg=INK, insertbackground=INK, relief='flat',
                             font=('Cascadia Mono', 9), highlightthickness=1,
                             highlightbackground='#404247', highlightcolor=ACCENT)
            entry.pack(side='left')
            entry.bind('<Return>', lambda event: self.apply_range_entries())
        ttk.Button(selection_row, text='选择范围', command=self.apply_range_entries,
                   style='Compact.TButton').pack(side='left', padx=self.px(5))
        tk.Label(selection_row, textvariable=self.selection_var, bg=BG, fg=ACCENT,
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(side='left', fill='x', expand=True, padx=self.px(6))
        self.strip = tk.Canvas(self, bg=INPUT, height=self.px(64), highlightthickness=0,
                               cursor='crosshair', takefocus=True)
        self.strip.pack(fill='x', padx=self.px(8), pady=(0, self.px(3)))
        self.strip.bind('<Configure>', lambda event: self._draw_strip())
        self.strip.bind('<ButtonPress-1>', self._strip_press)
        self.strip.bind('<B1-Motion>', self._strip_motion)
        self.strip.bind('<ButtonRelease-1>', self._strip_release)
        self.grading_tabs = ttk.Notebook(self, style='Multicam.TNotebook')
        self.grading_tabs.pack(fill='both', expand=True)
        self.wheels_tab = tk.Frame(self.grading_tabs, bg=BG)
        self.curves_tab = tk.Frame(self.grading_tabs, bg=BG)
        self.grading_tabs.add(self.wheels_tab, text='主色轮')
        self.grading_tabs.add(self.curves_tab, text='曲线')
        self.auto_panel=AutoColorPanel(self.grading_tabs,self)
        self.grading_tabs.add(self.auto_panel,text='自动 / 人像')
        self.grading_tabs.bind('<<NotebookTabChanged>>', self._grading_tab_changed)
        # The controls scroll vertically when the bottom pane is short.  The
        # selected time range stays visible above them.
        self.controls_canvas = tk.Canvas(self.wheels_tab, bg=BG, highlightthickness=0,
                                          width=1, height=self.px(225),
                                          yscrollincrement=max(1, self.px(20)))
        self.scroll = ttk.Scrollbar(self.wheels_tab, orient='vertical', command=self.controls_canvas.yview)
        self.scroll.pack(side='right', fill='y')
        self.controls_canvas.pack(fill='both', expand=True)
        self.controls_canvas.configure(yscrollcommand=self.scroll.set)
        self.controls = tk.Frame(self.controls_canvas, bg=BG)
        self._window = self.controls_canvas.create_window(0, 0, anchor='nw', window=self.controls)
        self.controls.bind('<Configure>', lambda event: self.controls_canvas.configure(
            scrollregion=self.controls_canvas.bbox('all')))
        self.controls_canvas.bind('<Configure>', self._controls_resize)
        self.wheels = {}
        for name, title in WHEELS:
            wheel = PrimaryWheel(self.controls, self, name, title)
            wheel.y_scale.bind('<ButtonRelease-1>', wheel.commit_y)
            wheel.y_scale.bind('<KeyRelease>', wheel.commit_y)
            self.wheels[name] = wheel
        self.note = tk.Label(self.controls, text='单击片段选择；拖动框选范围（Shift 也可）；上方标尺定位播放头。'
                            ' C 剪切 · ←/→ 逐帧 · 1–9 切换所选片段机位。拖动色轮实时预览，松开应用；数字输入回车应用。',
                             bg=BG, fg=MUTED, justify='left', anchor='w',
                             font=('Microsoft YaHei UI', 8))
        self._layout_controls(self.px(1000))
        self._install_scroll(self.controls)
        self.curve_editor = CurveEditor(self.curves_tab, self)
        self.curve_editor.pack(fill='both', expand=True)
        self.refresh()

    def px(self, value):
        return self.ws._px(value)

    def available(self):
        return self.ws._available()

    def target_token(self):
        target = getattr(self.ws, 'grade_target', 'selection')
        selected = self.ws.selected_program_range if target == 'selection' else None
        return target, tuple(selected) if selected else None

    def get_grade(self):
        getter = getattr(self.ws, 'get_active_grade', None)
        return copy.deepcopy(getter() if callable(getter) else self.ws.get_selected_grade())

    def apply_grade(self, grade):
        apply = getattr(self.ws, 'apply_active_grade', None)
        return apply(grade) if callable(apply) else self.ws.apply_selected_grade(grade)

    def preview_grade(self, grade):
        preview = getattr(self.ws, 'preview_active_grade', None)
        if callable(preview):
            preview(copy.deepcopy(grade))

    def clear_preview(self):
        clear = getattr(self.ws, 'clear_grade_preview', None)
        if callable(clear):
            clear()

    def cancel_drafts(self, clear_preview=True):
        grade = self.get_grade()
        for wheel in self.wheels.values():
            wheel.load(grade)
        editor = vars(self).get('curve_editor')
        if editor is not None:
            editor.cancel_draft(clear_preview=False)
        if clear_preview:
            self.clear_preview()

    def _grading_tab_changed(self, event=None):
        if vars(self).get('curve_editor') is not None:
            self.cancel_drafts()

    def _scope_changed(self, event=None):
        if self._refreshing or not self.available():
            return
        target = grade_target_from_label(self.scope_var.get())
        self.cancel_drafts()
        setter = getattr(self.ws, 'set_grade_target', None)
        if callable(setter):
            setter(target)
        else:
            self.ws.grade_target = target
        self.refresh()

    def show_error(self, error):
        messagebox.showerror('片段调色', str(error), parent=self.winfo_toplevel())

    def wheel_image(self, size):
        if size not in self._images:
            # Resize callbacks can produce temporary dimensions.  Keep a small
            # cache so continuous sash dragging does not retain old bitmaps.
            if len(self._images) > 8:
                self._images.clear()
            self._images[size] = tk.PhotoImage(master=self, data=_wheel_ppm(size), format='PPM')
        return self._images[size]

    def _install_scroll(self, widget):
        tag = f'ColorWheelScroll{self.winfo_id()}'
        self.bind_class(tag, '<MouseWheel>', self._scroll_controls)
        def apply(target):
            # Do not intercept the wheel over Scale/Entry: users should keep
            # their native numeric controls and focus behaviour.
            if not isinstance(target, (tk.Scale, tk.Entry)):
                target.bindtags((tag,) + target.bindtags())
            for child in target.winfo_children():
                apply(child)
        apply(widget)

    def _scroll_controls(self, event):
        if event.delta:
            steps = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
            self.controls_canvas.yview_scroll(steps, 'units')
        return 'break'

    def _controls_resize(self, event):
        self.controls_canvas.itemconfigure(self._window, width=max(1, event.width))
        self._layout_controls(event.width)

    def _layout_controls(self, width):
        columns = 4 if width >= self.px(900) else (2 if width >= self.px(460) else 1)
        if columns != self._columns:
            for index in range(4):
                self.controls.columnconfigure(index, weight=1 if index < columns else 0,
                                               uniform='wheel' if index < columns else '')
            for index, wheel in enumerate(self.wheels.values()):
                wheel.grid(row=index // columns, column=index % columns, sticky='nsew',
                            padx=self.px(4), pady=self.px(3))
            self.note.grid(row=math.ceil(4 / columns), column=0, columnspan=columns,
                           sticky='ew', padx=self.px(9), pady=self.px(5))
            self._columns = columns
        self.note.configure(wraplength=max(self.px(100), width - self.px(20)))

    def _duration(self):
        return max(0.0, float(self.ws.project.duration))

    def _snap(self, time):
        fps = max(0.001, float(self.ws.project.fps))
        snapped = math.floor(max(0.0, time) * fps + 0.5) / fps
        return min(self._duration(), snapped)

    def _time_from_x(self, x):
        width = max(1, self.strip.winfo_width() - self.px(16))
        return self._snap((x - self.px(8)) / width * self._duration())

    def _x_from_time(self, time):
        width = max(1, self.strip.winfo_width() - self.px(16))
        return self.px(8) + width * float(time) / max(self._duration(), 1e-9)

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            if vars(self).get('auto_panel'):self.auto_panel.refresh_reference_choices()
            token = self.target_token()
            if self._active_target_token is not None and token != self._active_target_token:
                self.cancel_drafts()
            self._active_target_token = token
            target = token[0]
            self.scope_combo.configure(values=[grade_target_label(self.ws, 'selection')]
                + [f'整机位 {camera}' for camera in self.ws.project.cameras] + ['全片'])
            self.scope_var.set(grade_target_label(self.ws, target))
            selection = self.ws.selected_program_range
            if selection is not None:
                start, end = selection
                self.range_start_var.set(format_time(start))
                self.range_end_var.set(format_time(end))
                self.selection_var.set(f'已选 {end - start:.3f} 秒 · ' +
                    ('仅修改所选片段' if target == 'selection' else '当前目标：' + grade_target_label(self.ws, target)))
            else:
                self.range_start_var.set(format_time(0))
                self.range_end_var.set(format_time(self._duration()))
                self.selection_var.set('点击片段或拖动选择调色范围')
            grade = self.get_grade()
            for wheel in self.wheels.values():
                if wheel.draft is None:
                    wheel.load(grade)
            self.curve_editor.load_grade(grade)
            self._draw_strip()
        finally:
            self._refreshing = False

    def _draw_strip(self):
        self.strip.delete('all')
        self._span_records = []
        width, height = self.strip.winfo_width(), self.strip.winfo_height()
        if width <= 1:
            return
        duration = self._duration()
        y1, y2 = self.px(24), max(self.px(30), height - self.px(6))
        if duration <= 0:
            self.strip.create_text(self.px(12), height / 2, anchor='w', fill=MUTED,
                                    text='导入视频后选择片段调色', font=('Microsoft YaHei UI', 9))
            return
        for index in range(6):
            time = duration * index / 5.0
            x = self._x_from_time(time)
            self.strip.create_line(x, self.px(15), x, self.px(21), fill='#60646a')
            self.strip.create_text(x, self.px(7), anchor='n', fill=MUTED,
                                    text=format_time(time), font=('Cascadia Mono', 8))
        for index, span in enumerate(program_spans(self.ws.project)):
            x1, x2 = self._x_from_time(span.start), self._x_from_time(span.end)
            self.strip.create_rectangle(x1, y1, x2, y2, fill=CAMERA_COLORS[(span.camera - 1) % len(CAMERA_COLORS)],
                                         outline=INPUT, width=max(1, self.px(1)))
            if x2 - x1 > self.px(42):
                self.strip.create_text(x1 + self.px(7), (y1 + y2) / 2, anchor='w',
                                        text=f'{index + 1} · 机位 {span.camera}', fill=INK,
                                        font=('Microsoft YaHei UI', 8))
            self._span_records.append(span)
        selection = self._draft_range or self.ws.selected_program_range
        if selection is not None:
            start, end = sorted(selection)
            x1, x2 = self._x_from_time(start), self._x_from_time(end)
            self.strip.create_rectangle(x1, y1 - self.px(2), x2, y2 + self.px(2),
                                         outline='#e5bc6a', width=max(2, self.px(2)), tags='range')
            for x in (x1, x2):
                self.strip.create_rectangle(x - self.px(2), y1, x + self.px(2), y2,
                                             fill='#e5bc6a', outline='', tags='range')
        self.update_playhead()

    def update_playhead(self):
        self.strip.delete('playhead')
        if self._duration() <= 0:
            return
        x = self._x_from_time(self.ws.current)
        self.strip.create_line(x, self.px(16), x, self.strip.winfo_height(), fill='#ef675d',
                                width=max(1, self.px(2)), tags='playhead')
        self.strip.create_polygon(x - self.px(4), self.px(14), x + self.px(4), self.px(14),
                                   x, self.px(20), fill='#ef675d', outline='', tags='playhead')

    def _strip_press(self, event):
        if not self.available() or self._duration() <= 0:
            return 'break'
        self.strip.focus_set()
        time = self._time_from_x(event.x)
        if event.y < self.px(23):
            self._drag = {'mode': 'seek'}
            self.ws.seek(time)
            return 'break'
        selection = self.ws.selected_program_range
        mode = 'select'
        if selection and not (event.state & 0x0001):
            if abs(event.x - self._x_from_time(selection[0])) <= self.px(5):
                mode = 'left'
            elif abs(event.x - self._x_from_time(selection[1])) <= self.px(5):
                mode = 'right'
        self._drag = {'mode': mode, 'anchor': time, 'x': event.x,
                      'selection': selection, 'shift': bool(event.state & 0x0001), 'moved': False}
        return 'break'

    def _strip_motion(self, event):
        if not self._drag:
            return 'break'
        time = self._time_from_x(event.x)
        if self._drag['mode'] == 'seek':
            self.ws.seek(time)
            return 'break'
        self._drag['moved'] |= abs(event.x - self._drag['x']) > self.px(3)
        if not self._drag['moved']:
            return 'break'
        if self._drag['mode'] == 'left':
            self._draft_range = tuple(sorted((time, self._drag['selection'][1])))
        elif self._drag['mode'] == 'right':
            self._draft_range = tuple(sorted((self._drag['selection'][0], time)))
        else:
            self._draft_range = tuple(sorted((self._drag['anchor'], time)))
        self._draw_strip()
        return 'break'

    def _strip_release(self, event):
        drag, self._drag = self._drag, None
        if not drag or drag['mode'] == 'seek':
            return 'break'
        time = self._time_from_x(event.x)
        selection = self._draft_range
        self._draft_range = None
        if not drag['moved']:
            span = next((span for span in self._span_records
                         if span.start <= time < span.end or
                         (time == self._duration() and span.end == time)), None)
            if span is not None and not drag['shift']:
                selection = (span.start, span.end)
            else:
                selection = (time, min(self._duration(), time + 1.0 / self.ws.project.fps))
        if selection and selection[1] - selection[0] >= 0.5 / self.ws.project.fps:
            self.ws.select_program_range(*selection, seek=bool(drag['moved']))
            if not drag['moved']:
                # Select the containing shot while keeping the playhead at
                # the mouse position, ready for C to cut that exact frame.
                self.ws.seek(time)
        self.refresh()
        return 'break'

    def apply_range_entries(self):
        if not self.available():
            return
        try:
            start = self._snap(parse_timecode(self.range_start_var.get()))
            end = self._snap(parse_timecode(self.range_end_var.get()))
            if end <= start:
                raise ValueError('结束时间必须晚于开始时间。')
            self.ws.select_program_range(start, end, seek=True)
            self.refresh()
        except (TypeError, ValueError, tk.TclError) as error:
            self.show_error(error)
