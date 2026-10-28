"""Persistent selected-programme controls in the existing right inspector.

All commands use the workspace's existing undoable grade, transition and
stabilization pipelines. The panel never substitutes a source-wide edit for
the currently selected programme range.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import math
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from creative_core import ColorGrade, Transition, validate_grade
from creative_render import plan_creative_segments
from creative_panel import TRANSITIONS
from editor_core import format_time
from effects_library import load_transition_masks, transition_mask_path
from gyroflow_core import default_settings, validate_settings
from image_stabilization_panel import image_defaults, MODEL_NAMES, QUALITY_NAMES
from stabilization_panel import ZOOM_NAMES, open_stabilization_dialog
from program_edits import split_at, program_spans

PANEL, INPUT, INK, MUTED, ACCENT = '#292b2e', '#18191b', '#dedfe3', '#969ba3', '#58a6d6'
FONT = ('Microsoft YaHei UI', 9)
GRADE_FIELDS = {'exposure': '曝光（档）', 'contrast': '对比度', 'saturation': '饱和度', 'temperature': '色温'}


class ClipInspector(tk.Frame):
    def __init__(self, parent, workspace):
        super().__init__(parent, bg=PANEL)
        self.ws = workspace
        self._target = None
        self._probe_token = None
        self.sections = {}
        self.action_widgets = []
        self.target_text = tk.StringVar(self, value='请选择节目轨中的片段')
        self.camera_var = tk.StringVar(self)
        self.camera_choices = {}
        self.grade_vars = {name: tk.StringVar(self) for name in GRADE_FIELDS}
        self.image_smoothing = tk.StringVar(self, value='0.5')
        self.image_shakiness = tk.StringVar(self, value='5')
        self.image_quality = tk.StringVar(self, value=next(iter(QUALITY_NAMES)))
        self.image_model = tk.StringVar(self, value=next(iter(MODEL_NAMES)))
        self.image_zoom = tk.BooleanVar(self, value=True)
        self.image_tripod = tk.BooleanVar(self, value=False)
        self.gyro_smoothness = tk.StringVar(self, value='0.5')
        self.gyro_fov = tk.StringVar(self, value='1')
        self.gyro_zoom = tk.StringVar(self, value='动态缩放')
        self.gyro_horizon = tk.StringVar(self, value='0')
        self.gyro_sync = tk.BooleanVar(self, value=True)
        self.gyro_status = tk.StringVar(self, value='数据与镜头配置将在检测后显示。')
        self.stabilization_text = tk.StringVar(self)
        self.transition_query = tk.StringVar(self)
        self.transition_kind = tk.StringVar(self, value='叠化')
        self.transition_duration = tk.StringVar(self, value='0.5')
        self.transition_text = tk.StringVar(self)
        self.overlay_text = tk.StringVar(self)
        self.transition_choices = dict(TRANSITIONS)
        for mask in load_transition_masks():
            self.transition_choices[f'库遮罩 · {mask.category} · {mask.name}'] = mask

        heading = tk.Frame(self, bg=PANEL)
        heading.pack(fill='x', padx=self.px(10), pady=(self.px(9), self.px(5)))
        self._label(heading, self.target_text, color=INK, bold=True).pack(fill='x')
        navigation = tk.Frame(self, bg=PANEL)
        navigation.pack(fill='x', padx=self.px(8), pady=(0, self.px(7)))
        for i, (key, title) in enumerate((('camera', '机位'), ('color', '调色'), ('image', '画面防抖'),
                                         ('gyro', 'Gyroflow'), ('transition', '转场'), ('overlay', '叠加特效'))):
            navigation.columnconfigure(i % 3, weight=1, uniform='module')
            ttk.Button(navigation, text=title, command=lambda name=key: self.scroll_to(name),
                       style='Inspector.TButton').grid(row=i // 3, column=i % 3, sticky='ew',
                                                     padx=self.px(2), pady=self.px(2))
        outer = tk.Frame(self, bg=PANEL)
        outer.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(outer, bg=PANEL, width=1, height=1, highlightthickness=0,
                                yscrollincrement=self.px(22))
        scroll = ttk.Scrollbar(outer, orient='vertical', command=self.canvas.yview)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = tk.Frame(self.canvas, bg=PANEL)
        window = self.canvas.create_window(0, 0, window=self.body, anchor='nw')
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(window, width=max(1, event.width)))
        self.body.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self._build_camera()
        self._build_color()
        self._build_image()
        self._build_gyro()
        self._build_transition()
        self._build_overlay()
        self._bind_scroll(self.body)
        self.canvas.bind('<MouseWheel>', self._wheel)
        self.refresh(force=True)

    def px(self, value):
        return self.ws._px(value)

    def _label(self, parent, variable, *, color=MUTED, bold=False):
        label = tk.Label(parent, textvariable=variable if isinstance(variable, tk.Variable) else None,
                         text=variable if isinstance(variable, str) else None,
                         bg=PANEL, fg=color, anchor='w', justify='left', wraplength=self.px(300),
                         font=('Microsoft YaHei UI', 9, 'bold' if bold else 'normal'))
        label.bind('<Configure>', lambda event: label.configure(wraplength=max(80, event.width)), add='+')
        return label

    def _section(self, key, title, description=''):
        section = tk.Frame(self.body, bg=PANEL, highlightthickness=1, highlightbackground='#3b3e43')
        section.pack(fill='x', padx=self.px(8), pady=self.px(5))
        self.sections[key] = section
        self._label(section, title, color=INK, bold=True).pack(fill='x', padx=self.px(10), pady=(self.px(10), self.px(4)))
        if description:
            self._label(section, description).pack(fill='x', padx=self.px(10), pady=(0, self.px(6)))
        return section

    def _row(self, parent, label, variable, choices=None):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=self.px(10), pady=self.px(4))
        self._label(row, label).pack(side='left', padx=(0, self.px(8)))
        if choices is not None:
            field = ttk.Combobox(row, textvariable=variable, values=choices, state='readonly', width=12)
        else:
            field = tk.Entry(row, textvariable=variable, width=9, bg=INPUT, fg=INK,
                             insertbackground=INK, relief='flat', font=FONT)
        field.pack(side='right', ipady=self.px(4))
        return field

    def _actions(self, parent, pairs):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=self.px(8), pady=self.px(7))
        for title, callback in pairs:
            button = ttk.Button(row, text=title, command=callback, style='Inspector.TButton')
            button.pack(side='left', fill='x', expand=True, padx=self.px(2))
            self.action_widgets.append(button)

    def _check(self, parent, title, variable):
        tk.Checkbutton(parent, text=title, variable=variable, bg=PANEL, fg=INK,
            selectcolor=INPUT, activebackground=PANEL, activeforeground=INK, font=FONT).pack(
                anchor='w', padx=self.px(8), pady=self.px(2))

    def _build_camera(self):
        section = self._section('camera', '机位与片段', '只修改当前选中的节目片段，其他片段与原片保持不变。')
        self.camera_combo = ttk.Combobox(section, textvariable=self.camera_var, state='readonly', width=1)
        self.camera_combo.pack(fill='x', padx=self.px(10), pady=self.px(5))
        self._actions(section, [('应用机位', self.apply_camera), ('定位片段', self.seek_selection)])

    def _build_color(self):
        section = self._section('color', '片段调色', '常用参数直接调整；完整参数页包含阴影、高光、伽马与 LUT。')
        for name, title in GRADE_FIELDS.items():
            self._row(section, title, self.grade_vars[name])
        self._actions(section, [('应用调色', self.apply_color), ('重置片段', self.reset_color)])
        self._actions(section, [('完整调色参数', lambda: self.ws.show_inspector('color')),
                                ('精细调色工作台', lambda: self.ws.show_creative('color'))])

    def _build_image(self):
        section = self._section('image', '画面防抖', '无需陀螺仪，基于画面运动分析。适用于普通相机素材。')
        self._row(section, '平滑秒数', self.image_smoothing)
        self._row(section, '抖动程度 1–10', self.image_shakiness)
        self._row(section, '处理质量', self.image_quality, list(QUALITY_NAMES))
        self._row(section, '运动模型', self.image_model, list(MODEL_NAMES))
        self._check(section, '自动裁边缩放', self.image_zoom)
        self._check(section, '锁定机位（固定构图）', self.image_tripod)
        self._actions(section, [('自动推荐处理', lambda: self.start_image(True)),
                                ('按参数处理', self.start_image)])
        self._actions(section, [('移除防抖', self.remove_stabilization), ('原片 / 防抖对比', self.compare_stabilization)])
        self._label(section, self.stabilization_text).pack(fill='x', padx=self.px(10), pady=(0, self.px(8)))

    def _build_gyro(self):
        section = self._section('gyro', 'Gyroflow 防抖', '使用运动数据与镜头配置；先检测可确认素材是否支持。')
        self._row(section, '平滑程度', self.gyro_smoothness)
        self._row(section, '视野 FOV', self.gyro_fov)
        self._row(section, '缩放方式', self.gyro_zoom, list(ZOOM_NAMES))
        self._row(section, '地平线锁定 %', self.gyro_horizon)
        self._check(section, '自动同步陀螺仪', self.gyro_sync)
        self._actions(section, [('检测运动数据', self.probe_gyro), ('按参数处理', self.start_gyro)])
        self._actions(section, [('自动推荐处理', lambda: self.start_gyro(True)),
                                ('镜头与高级设置…', lambda: open_stabilization_dialog(self.ws, mode='manual'))])
        self._label(section, self.gyro_status).pack(fill='x', padx=self.px(10), pady=(0, self.px(8)))

    def _build_transition(self):
        section = self._section('transition', '片段末尾转场', '连接所选片段与下一段。支持内置效果与完整库遮罩。')
        self._row(section, '持续秒数', self.transition_duration)
        self._row(section, '搜索效果', self.transition_query)
        self.transition_combo = ttk.Combobox(section, textvariable=self.transition_kind,
            values=list(self.transition_choices), state='readonly', width=1, height=15)
        self.transition_combo.pack(fill='x', padx=self.px(10), pady=self.px(5))
        self.transition_query.trace_add('write', self._filter_transitions)
        self._label(section, f'{len(TRANSITIONS)} 种内置转场 + {len(self.transition_choices)-len(TRANSITIONS)} 种库遮罩').pack(
            fill='x', padx=self.px(10), pady=self.px(3))
        self._actions(section, [('应用到末尾', self.apply_transition), ('移除末尾转场', self.remove_transition)])
        self._actions(section, [('浏览遮罩缩略图', lambda: self.open_library('遮罩转场')),
                                ('全部切点参数', lambda: self.ws.show_inspector('transition'))])
        self._label(section, self.transition_text).pack(fill='x', padx=self.px(10), pady=(0, self.px(8)))

    def _build_overlay(self):
        section = self._section('overlay', '叠加与特效库', '在右侧特效库浏览全部素材、缩略图与强度参数，应用到当前选区。')
        self._label(section, self.overlay_text).pack(fill='x', padx=self.px(10), pady=self.px(5))
        self._actions(section, [('打开叠加特效库', lambda: self.open_library('叠加')),
                                ('移除所选叠加', self.ws.remove_library_overlay)])
        self._actions(section, [('预览当前效果片段', self.ws.preview_effects)])

    def _bind_scroll(self, widget):
        if not isinstance(widget, ttk.Combobox):
            widget.bind('<MouseWheel>', self._wheel, add='+')
        for child in widget.winfo_children():
            self._bind_scroll(child)

    def _wheel(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    def scroll_to(self, name):
        self.update_idletasks()
        self.canvas.yview_moveto(self.sections[name].winfo_y()/max(1, self.body.winfo_height()))

    def _ready(self):
        if not self.ws._available():
            return False
        if self.ws.selected_program_range is None:
            self.ws.status.set('请先选中节目轨中的片段。')
            return False
        return True

    def _number(self, variable, title):
        try:
            value = float(variable.get())
            if not math.isfinite(value): raise ValueError()
            return value
        except ValueError as error:
            raise ValueError(title+'请输入有效数字。') from error

    def _report(self, error):
        self.ws.status.set(str(error))
        messagebox.showerror('片段检查器', str(error), parent=self.ws)

    def apply_camera(self):
        if not self._ready(): return
        camera = self.camera_choices.get(self.camera_var.get())
        if camera is not None: self.ws._switch_selected_camera(camera)

    def seek_selection(self):
        if self._ready(): self.ws.seek(self.ws.selected_program_range[0])

    def apply_color(self):
        if not self._ready(): return
        try:
            grade = replace(self.ws.get_selected_grade(), **{key: self._number(value, GRADE_FIELDS[key])
                                                          for key, value in self.grade_vars.items()})
            validate_grade(grade)
            self.ws.apply_selected_grade(grade)
        except (ValueError, OSError) as error: self._report(error)

    def reset_color(self):
        if self._ready(): self.ws.apply_selected_grade(ColorGrade())

    def start_image(self, automatic=False):
        if not self._ready(): return
        try:
            settings = image_defaults('auto' if automatic else 'manual')
            if not automatic:
                shakiness = self._number(self.image_shakiness, '抖动程度')
                if not shakiness.is_integer(): raise ValueError('抖动程度须为 1–10 的整数。')
                settings = replace(settings, image_smoothing=self._number(self.image_smoothing, '平滑秒数'),
                    image_shakiness=int(shakiness), image_quality=QUALITY_NAMES[self.image_quality.get()],
                    image_model=MODEL_NAMES[self.image_model.get()], image_zoom=self.image_zoom.get(),
                    image_tripod=self.image_tripod.get())
            validate_settings(settings)
            return self.ws.start_stabilization(settings)
        except (ValueError, KeyError) as error: self._report(error)

    def _gyro_settings(self, automatic=False):
        record = self.ws.get_selected_stabilization()
        settings = copy.deepcopy(record.settings) if record and record.settings.engine == 'gyroflow' else default_settings()
        if automatic:
            return replace(settings, mode='auto')
        return replace(settings, mode='manual', engine='gyroflow',
            smoothness=self._number(self.gyro_smoothness, '平滑程度'), fov=self._number(self.gyro_fov, '视野'),
            zoom_mode=ZOOM_NAMES[self.gyro_zoom.get()], horizon_lock=self._number(self.gyro_horizon, '地平线锁定'),
            auto_sync=self.gyro_sync.get())

    def start_gyro(self, automatic=False):
        if not self._ready(): return
        try:
            settings = self._gyro_settings(automatic)
            validate_settings(settings)
            return self.ws.start_stabilization(settings)
        except (ValueError, KeyError) as error: self._report(error)

    def probe_gyro(self):
        if not self._ready(): return
        token = (id(self.ws.project), self.ws.selected_program_range)
        self._probe_token = token
        self.gyro_status.set('正在检测运动数据与镜头配置…')
        def finish(info):
            if self.winfo_exists() and self._probe_token == token and (id(self.ws.project), self.ws.selected_program_range) == token:
                self.gyro_status.set(info.get('message') or info.get('error') or '检测完成。')
        try:
            self.ws.probe_selected_stabilization(finish, self._gyro_settings())
        except (ValueError, KeyError) as error: self._report(error)

    def remove_stabilization(self):
        if self._ready(): self.ws.remove_selected_stabilization()

    def compare_stabilization(self):
        if not self._ready(): return
        if self.ws.get_selected_stabilization() is None:
            self.ws.status.set('当前片段尚未完成防抖。'); return
        self.ws.stabilization_compare_var.set(not self.ws.stabilization_compare_var.get())
        self.ws.toggle_stabilization_compare()

    def _filter_transitions(self, *_args):
        query = self.transition_query.get().strip().casefold()
        choices = [name for name in self.transition_choices if query in name.casefold()]
        self.transition_combo.configure(values=choices)
        if self.transition_kind.get() not in choices:
            self.transition_kind.set(choices[0] if choices else '')

    def apply_transition(self):
        if not self._ready(): return
        try:
            choice = self.transition_choices.get(self.transition_kind.get())
            if choice is None: raise ValueError('请选择转场效果。')
            duration = self._number(self.transition_duration, '转场时长')
            if not isinstance(choice, str): return self.ws.apply_library_mask(choice, duration)
            start, end = self.ws.selected_program_range
            if end >= self.ws.project.duration-.5/self.ws.project.fps:
                raise ValueError('所选片段后没有片段，不能添加末尾转场。')
            candidate = copy.deepcopy(self.ws.project)
            split_at(candidate, end)
            candidate.transitions = [item for item in candidate.transitions if round(item.time*candidate.fps) != round(end*candidate.fps)]
            candidate.transitions.append(Transition(end, choice, duration))
            candidate.transitions.sort(key=lambda item: item.time)
            candidate.validate()
            plan_creative_segments(candidate, candidate.segments())
            self.ws._remember(); self.ws.project = candidate; self.ws._refresh()
            self.ws.status.set('片段末尾转场已应用，可撤销。')
        except (ValueError, OSError) as error: self._report(error)

    def remove_transition(self):
        if not self._ready(): return
        end, fps = self.ws.selected_program_range[1], self.ws.project.fps
        kept = [item for item in self.ws.project.transitions if round(item.time*fps) != round(end*fps)]
        if len(kept) == len(self.ws.project.transitions): return
        self.ws._remember(); self.ws.project.transitions = kept; self.ws._refresh()

    def open_library(self, category):
        browser = self.ws.show_effects_library()
        browser.category.set(category)
        browser.query.set('')
        return browser

    def refresh(self, force=False):
        selected = self.ws.selected_program_range
        self.camera_choices = {self.ws._label_camera(camera): camera for camera in self.ws.project.cameras}
        self.camera_combo.configure(values=list(self.camera_choices))
        for button in self.action_widgets:
            button.configure(state='normal' if selected else 'disabled')
        if selected is None:
            self.target_text.set('请选择节目轨中的片段\n机位、调色、防抖和转场将在这里显示。')
            self._target = None
            return
        start, end = selected
        camera = self.ws.project.camera_at(start)
        clip = self.ws.project.clip_at(camera, start)
        self.target_text.set(f'{format_time(start)} — {format_time(end)}\n'
                             + self.ws._label_camera(camera, start)
                             + (' · '+Path(clip.path).name if clip else ' · 此处无画面'))
        self.camera_var.set(next((name for name, value in self.camera_choices.items() if value == camera), ''))
        record = self.ws.get_selected_stabilization()
        self.stabilization_text.set('当前：'+('画面防抖' if record.settings.engine == 'image' else 'Gyroflow 防抖') if record else '当前片段未应用防抖。')
        overlays = [item for item in self.ws.project.library_overlays if item.start < end and item.end > start]
        self.overlay_text.set(f'当前选区已应用 {len(overlays)} 个库叠加效果。' if overlays else '当前选区未应用库叠加效果。')
        transition = next((item for item in self.ws.project.transitions if round(item.time*self.ws.project.fps) == round(end*self.ws.project.fps)), None)
        self.transition_text.set('当前末尾：'+('库遮罩' if getattr(transition, 'mask_path', '') else next((label for label, kind in TRANSITIONS.items() if kind == transition.kind), transition.kind))
                                 +f' · {transition.duration:g} 秒' if transition else '当前片段末尾未添加转场。')
        token = (id(self.ws.project), selected)
        if not force and token == self._target: return
        self._target = token
        grade = self.ws.get_selected_grade()
        for key, variable in self.grade_vars.items(): variable.set(f'{getattr(grade, key):g}')
        image = record.settings if record and record.settings.engine == 'image' else image_defaults()
        self.image_smoothing.set(f'{image.image_smoothing:g}'); self.image_shakiness.set(str(image.image_shakiness))
        self.image_quality.set(next(name for name, value in QUALITY_NAMES.items() if value == image.image_quality))
        self.image_model.set(next(name for name, value in MODEL_NAMES.items() if value == image.image_model))
        self.image_zoom.set(image.image_zoom); self.image_tripod.set(image.image_tripod)
        gyro = record.settings if record and record.settings.engine == 'gyroflow' else default_settings()
        self.gyro_smoothness.set(f'{gyro.smoothness:g}'); self.gyro_fov.set(f'{gyro.fov:g}')
        self.gyro_horizon.set(f'{gyro.horizon_lock:g}'); self.gyro_sync.set(gyro.auto_sync)
        self.gyro_zoom.set(next(name for name, value in ZOOM_NAMES.items() if value == gyro.zoom_mode))
        self.gyro_status.set('当前片段已有 Gyroflow 防抖设置。' if record and record.settings.engine == 'gyroflow' else '数据与镜头配置将在检测后显示。')
        if transition:
            label = next((name for name, value in self.transition_choices.items()
                          if (isinstance(value, str) and not getattr(transition, 'mask_path', '') and value == transition.kind)
                          or (not isinstance(value, str) and transition_mask_path(value) == getattr(transition, 'mask_path', ''))), '叠化')
            self.transition_kind.set(label); self.transition_duration.set(f'{transition.duration:g}')
        else:
            self.transition_kind.set('叠化'); self.transition_duration.set('0.5')
