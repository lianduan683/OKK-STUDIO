"""Compact, undoable grade, transition and SRT controls for the inspector."""
from __future__ import annotations

import copy
from dataclasses import replace
import math
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, font as tkfont, messagebox, ttk
import uuid

from creative_core import (ColorGrade, GRADE_RANGES, Transition, validate_grade,
                           validate_transitions)
from color_workspace import grade_target_label, grade_target_from_label
from editor_core import format_signed_time, format_time, parse_signed_timecode, parse_timecode
from subtitle_core import (SubtitleCue, parse_srt, validate_subtitle_track, write_srt)


PANEL, INPUT, INK, MUTED, ACCENT = '#292b2e', '#18191b', '#dedfe3', '#969ba3', '#58a6d6'
FONT = ('Microsoft YaHei UI', 9)
GRADE_LABELS = {'exposure': '曝光（档）', 'contrast': '对比度', 'saturation': '饱和度',
                'temperature': '色温', 'tint': '色调', 'gamma': '伽马',
                'shadows': '阴影', 'highlights': '高光'}
TRANSITIONS = {'叠化': 'fade', '颗粒溶解': 'dissolve', '向左擦除': 'wipeleft',
               '向右擦除': 'wiperight', '向左滑动': 'slideleft', '向右滑动': 'slideright',
               '圆形展开': 'circleopen', '淡入黑场': 'fadeblack', '淡入白场': 'fadewhite'}
ALIGNMENTS = {'下方居中': 2, '下方居左': 1, '下方居右': 3, '画面正中': 5,
              '中部居左': 4, '中部居右': 6, '上方居中': 8, '上方居左': 7, '上方居右': 9}


class CreativePanel(tk.Frame):
    def __init__(self, parent, workspace):
        super().__init__(parent, bg=PANEL)
        self.ws = workspace
        self.loading = False
        self._cut_choices = {}
        self.grade_vars = {name: tk.StringVar(self) for name in GRADE_RANGES}
        self.grade_scope_var = tk.StringVar(self, value='全片')
        self.grade_enabled_var = tk.BooleanVar(self, value=True)
        self.lut_var = tk.StringVar(self)
        self.transition_cut_var = tk.StringVar(self)
        self.transition_kind_var = tk.StringVar(self, value='叠化')
        self.transition_duration_var = tk.StringVar(self, value='0.5')
        self.subtitle_start_var = tk.StringVar(self, value='00:00:00.000')
        self.subtitle_end_var = tk.StringVar(self, value='00:00:02.000')
        self.subtitle_offset_var = tk.StringVar(self, value='00:00:00.000')
        self.subtitle_enabled_var = tk.BooleanVar(self, value=True)
        self.subtitle_burn_var = tk.BooleanVar(self, value=True)
        self.subtitle_source_var = tk.StringVar(self, value='尚未导入 SRT')
        self.style_vars = {}
        self._selected_cue_id = None
        self._last_ws_selected_cut = None
        self._grade_entry_target = None
        self._canvases = []
        footer = tk.Frame(self, bg=PANEL)
        footer.pack(side='bottom', fill='x')
        ttk.Button(footer, text='预览当前效果片段', command=self.ws.preview_effects,
                   style='Compact.TButton').pack(fill='x', padx=self.px(10), pady=(self.px(6), self.px(2)))
        self._note(footer, '调色调整会实时显示；此按钮用于转场、字幕和 LUT 的准确短片预览。').pack(
            fill='x', padx=self.px(10), pady=(0, self.px(6)))
        self.tabs = ttk.Notebook(self, style='Multicam.TNotebook')
        self.tabs.pack(fill='both', expand=True)
        self.pages = {}
        self._build_color()
        self._build_transition()
        self._build_subtitle()
        self.refresh()
        for canvas, body in self._canvases:
            tag = f'CreativeScroll{canvas.winfo_id()}'
            self.bind_class(tag, '<MouseWheel>', lambda event, target=canvas: self._wheel(event, target))
            self._scroll_tag(body, tag)

    def px(self, value):
        return self.ws._px(value)

    def _wheel(self, event, canvas):
        canvas.yview_scroll(-int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1), 'units')
        return 'break'

    def _scroll_tag(self, widget, tag):
        widget.bindtags((tag,) + widget.bindtags())
        for child in widget.winfo_children():
            self._scroll_tag(child, tag)

    def _page(self, name, title):
        outer = tk.Frame(self.tabs, bg=PANEL)
        self.tabs.add(outer, text=title)
        self.pages[name] = outer
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0, bd=0,
                           width=1, height=1, yscrollincrement=max(1, self.px(18)))
        scroll = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body = tk.Frame(canvas, bg=PANEL)
        window = canvas.create_window(0, 0, window=body, anchor='nw')
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=max(1, event.width)))
        body.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<MouseWheel>', lambda event: self._wheel(event, canvas))
        self._canvases.append((canvas, body))
        return body

    def _note(self, parent, text):
        label = tk.Label(parent, text=text, bg=PANEL, fg=MUTED, font=FONT,
                         justify='left', anchor='w', wraplength=self.px(250))
        parent.bind('<Configure>', lambda event: label.configure(wraplength=max(self.px(80), event.width - self.px(18))), add='+')
        return label

    def _section(self, parent, title):
        tk.Label(parent, text=title, bg=PANEL, fg=INK, font=('Microsoft YaHei UI', 9, 'bold'),
                 anchor='w').pack(fill='x', padx=self.px(10), pady=(self.px(12), self.px(5)))

    def _entry(self, parent, variable, width=10):
        return tk.Entry(parent, textvariable=variable, width=width, bg=INPUT, fg=INK,
                        insertbackground=INK, relief='flat', highlightthickness=1,
                        highlightbackground='#3a3c40', highlightcolor=ACCENT, font=FONT)

    def _row(self, parent, label, variable, *, choices=None, color=False):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=self.px(10), pady=self.px(3))
        tk.Label(row, text=label, bg=PANEL, fg=MUTED, font=FONT, anchor='w').pack(side='left')
        if choices is not None:
            widget = ttk.Combobox(row, textvariable=variable, values=choices,
                                  state='readonly', width=12)
        else:
            if color:
                ttk.Button(row, text='选色', width=4, command=lambda: self._choose_color(variable),
                           style='Compact.TButton').pack(side='right', padx=(self.px(3), 0))
            widget = self._entry(row, variable, width=10 if not color else 8)
        widget.pack(side='right', ipady=self.px(3))
        return widget

    def _check(self, parent, title, variable):
        tk.Checkbutton(parent, text=title, variable=variable, bg=PANEL, fg=INK,
                       activebackground=PANEL, activeforeground=INK, selectcolor=INPUT,
                       font=FONT).pack(anchor='w', padx=self.px(7), pady=self.px(2))

    def _actions(self, parent, actions):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=self.px(8), pady=self.px(6))
        for title, callback in actions:
            ttk.Button(row, text=title, command=callback, style='Compact.TButton').pack(
                side='left', fill='x', expand=True, padx=self.px(2))

    def _error(self, error):
        messagebox.showerror('效果与字幕', str(error), parent=self.winfo_toplevel())

    def _commit(self, callback, status):
        self.ws._remember()
        callback()
        self.ws._refresh()
        if hasattr(self.ws, 'status'):
            self.ws.status.set(status)

    def _number(self, variable, label, *, integer=False):
        text = variable.get().strip()
        try:
            value = float(text)
            if not math.isfinite(value) or (integer and not value.is_integer()):
                raise ValueError
            return int(value) if integer else value
        except (ValueError, TypeError, tk.TclError):
            raise ValueError(f'{label}必须是有效的' + ('整数。' if integer else '数字。')) from None

    def _build_color(self):
        body = self._page('color', '调色')
        self._section(body, '调色目标')
        self.scope_combo = ttk.Combobox(body, textvariable=self.grade_scope_var,
                                        state='readonly', values=['全片'])
        self.scope_combo.pack(fill='x', padx=self.px(10), pady=self.px(4))
        self.scope_combo.bind('<<ComboboxSelected>>', self._scope_changed)
        self._check(body, '启用这一组调色', self.grade_enabled_var)
        self._note(body, '全片调色会叠加在机位调色之后。').pack(fill='x', padx=self.px(10))
        for name, label in GRADE_LABELS.items():
            entry = self._row(body, label, self.grade_vars[name])
            entry.bind('<FocusIn>', self._grade_focus_in)
            entry.bind('<FocusOut>', self._grade_focus_out)
            entry.bind('<KeyRelease>', self.preview_grade)
        self._section(body, '3D LUT')
        self._entry(body, self.lut_var).pack(fill='x', padx=self.px(10), ipady=self.px(4))
        self._actions(body, [('导入 LUT…', self.choose_lut), ('清空路径', lambda: self.lut_var.set(''))])
        ttk.Button(body, text='本机达芬奇 LUT…', command=self.choose_resolve_lut,
                   style='Compact.TButton').pack(fill='x', padx=self.px(10), pady=self.px(3))
        self._note(body, '.cube / .3dl · 需有合法 LUT 文件。').pack(fill='x', padx=self.px(10))
        self._actions(body, [('应用调色', self.apply_grade), ('重置此目标', self.reset_grade)])

    def _scope(self):
        text = self.grade_scope_var.get()
        target = grade_target_from_label(text)
        if target is None:
            return None
        if target == 'selection':
            selected = getattr(self.ws, 'selected_program_range', None)
            selected = selected() if callable(selected) else selected
            if selected is None or not callable(getattr(self.ws, 'get_selected_grade', None)):
                raise ValueError('请先选择片段或时间范围。')
            return 'selection'
        camera = target
        if camera not in self.ws.project.cameras:
            raise ValueError('此机位已被删除，请重新选择。')
        return camera

    def _grade_for_scope(self, camera):
        getter = getattr(self.ws, 'get_active_grade', None)
        if callable(getter) and getattr(self.ws, 'grade_target', object()) == camera:
            return copy.deepcopy(getter())
        return copy.deepcopy(self.ws.get_selected_grade() if camera == 'selection' else
                             self.ws.project.global_grade if camera is None else
                             self.ws.project.camera_grades.get(camera, ColorGrade()))

    def _scope_changed(self, event=None):
        if self.loading or not self.ws._available():
            return
        try:
            camera = self._scope()
            self._grade_entry_target = None
            clear = getattr(self.ws, 'clear_grade_preview', None)
            if callable(clear): clear()
            setter = getattr(self.ws, 'set_grade_target', None)
            if callable(setter): setter(camera)
            self._load_grade()
        except (ValueError, tk.TclError) as error:
            self._error(error)

    def _grade_focus_in(self, event=None):
        try:
            self._grade_entry_target = self._scope()
        except ValueError:
            self._grade_entry_target = None

    def _grade_focus_out(self, event=None):
        clear = getattr(self.ws, 'clear_grade_preview', None)
        if callable(clear): clear()

    def cancel_grade_preview(self, clear_preview=True):
        self._grade_entry_target = None
        clear = getattr(self.ws, 'clear_grade_preview', None)
        if clear_preview and callable(clear): clear()

    def _read_grade(self, camera):
        previous = self._grade_for_scope(camera)
        grade = replace(previous, **{name: self._number(variable, GRADE_LABELS[name])
                                     for name, variable in self.grade_vars.items()},
                        lut_path=self.lut_var.get().strip(), enabled=self.grade_enabled_var.get())
        validate_grade(grade, check_lut=False)
        return grade

    def preview_grade(self, event=None):
        if self.loading or not self.ws._available():
            return
        preview = getattr(self.ws, 'preview_active_grade', None)
        if not callable(preview):
            return
        try:
            camera = self._scope()
            if getattr(self.ws, 'grade_target', camera) != camera:
                return
            if self._grade_entry_target is not None and self._grade_entry_target != camera:
                return
            preview(self._read_grade(camera))
        except (ValueError, TypeError, tk.TclError):
            pass  # Partially typed numbers are not valid preview settings yet.

    def _load_grade(self):
        try:
            camera = self._scope()
        except ValueError:
            return
        grade = self._grade_for_scope(camera)
        previous_loading = self.loading
        self.loading = True
        try:
            self._grade_entry_target = None
            self.grade_scope_var.set(grade_target_label(self.ws, camera))
            for name, variable in self.grade_vars.items():
                variable.set(f'{getattr(grade, name):g}')
            self.grade_enabled_var.set(grade.enabled)
            self.lut_var.set(grade.lut_path)
        finally:
            self.loading = previous_loading

    def choose_lut(self):
        if not self.ws._available():
            return
        path = filedialog.askopenfilename(parent=self.winfo_toplevel(), title='导入 3D LUT',
                                           filetypes=[('3D LUT', '*.cube *.3dl'), ('所有文件', '*.*')])
        if path:
            self.lut_var.set(path)

    def choose_resolve_lut(self):
        if not self.ws._available(): return
        directory = Path('C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/LUT')
        if not directory.is_dir():
            messagebox.showinfo('本机达芬奇 LUT', '未找到本机达芬奇 LUT 目录，可使用“导入 LUT”选择自己的文件。',
                                parent=self.winfo_toplevel())
            return
        path = filedialog.askopenfilename(parent=self.winfo_toplevel(), title='选择本机达芬奇 LUT',
            initialdir=str(directory), filetypes=[('Cube LUT', '*.cube'), ('3D LUT', '*.cube *.3dl')])
        if path: self.lut_var.set(path)

    def apply_grade(self):
        if not self.ws._available():
            return
        try:
            camera = self._scope()
            grade = self._read_grade(camera)
            validate_grade(grade)
            active_apply = getattr(self.ws, 'apply_active_grade', None)
            setter = getattr(self.ws, 'set_grade_target', None)
            if callable(active_apply):
                if callable(setter) and getattr(self.ws, 'grade_target', camera) != camera:
                    setter(camera)
                active_apply(grade)
                return
            if camera == 'selection':
                self.ws.apply_selected_grade(grade)
                return
            def change():
                if camera is None:
                    self.ws.project.global_grade = grade
                else:
                    self.ws.project.camera_grades[camera] = grade
            self._commit(change, '调色已应用。')
        except (ValueError, OSError, tk.TclError) as error:
            self._error(error)

    def reset_grade(self):
        if not self.ws._available():
            return
        try:
            camera = self._scope()
            active_apply = getattr(self.ws, 'apply_active_grade', None)
            setter = getattr(self.ws, 'set_grade_target', None)
            if callable(active_apply):
                if callable(setter) and getattr(self.ws, 'grade_target', camera) != camera:
                    setter(camera)
                active_apply(ColorGrade())
                return
            if camera == 'selection':
                self.ws.apply_selected_grade(ColorGrade())
                return
            def change():
                if camera is None:
                    self.ws.project.global_grade = ColorGrade()
                else:
                    self.ws.project.camera_grades.pop(camera, None)
            self._commit(change, '当前目标的调色已重置。')
        except ValueError as error:
            self._error(error)

    def _build_transition(self):
        body = self._page('transition', '转场')
        self._section(body, '选择机位切点')
        self.cut_combo = ttk.Combobox(body, textvariable=self.transition_cut_var, state='readonly')
        self.cut_combo.pack(fill='x', padx=self.px(10), pady=self.px(4))
        self.cut_combo.bind('<<ComboboxSelected>>', lambda event: self._load_transition())
        self._row(body, '转场效果', self.transition_kind_var, choices=list(TRANSITIONS))
        self._row(body, '持续秒数', self.transition_duration_var)
        self._note(body, '范围 0.05–5 秒，以切点居中，不改变成片总时长。相邻转场区间不能重叠。').pack(
            fill='x', padx=self.px(10), pady=self.px(6))
        self._actions(body, [('应用转场', self.apply_transition), ('删除转场', self.remove_transition)])
        mask_button = ttk.Button(body, text='浏览全部库遮罩与缩略图…', command=self._open_mask_library,
                                  style='Compact.TButton')
        mask_button.pack(fill='x', padx=self.px(10), pady=self.px(5))
        if not callable(getattr(self.ws, 'show_effects_library', None)):
            mask_button.configure(state='disabled')
        self.transition_summary_var = tk.StringVar(self)
        tk.Label(body, textvariable=self.transition_summary_var, bg=PANEL, fg=MUTED,
                 font=FONT, anchor='nw', justify='left', wraplength=self.px(240)).pack(
                     fill='x', padx=self.px(10), pady=self.px(8))

    def _open_mask_library(self):
        callback = getattr(self.ws, 'show_effects_library', None)
        if callable(callback):
            browser = callback()
            browser.category.set('遮罩转场')
            browser.query.set('')

    def _selected_cut_time(self):
        if self.transition_cut_var.get() not in self._cut_choices:
            raise ValueError('请先选择一个非零机位切点。')
        return self._cut_choices[self.transition_cut_var.get()]

    def _at_cut(self, transition, time):
        fps = self.ws.project.fps
        return math.floor(transition.time * fps + 0.5) == math.floor(time * fps + 0.5)

    def _load_transition(self):
        try:
            time = self._selected_cut_time()
        except ValueError:
            return
        transition = next((item for item in self.ws.project.transitions if self._at_cut(item, time)), None)
        if transition:
            self.transition_kind_var.set(next(label for label, kind in TRANSITIONS.items() if kind == transition.kind))
            self.transition_duration_var.set(f'{transition.duration:g}')

    def apply_transition(self):
        if not self.ws._available():
            return
        try:
            time = self._selected_cut_time()
            kind = TRANSITIONS.get(self.transition_kind_var.get())
            if kind is None:
                raise ValueError('请选择转场效果。')
            item = Transition(time, kind, self._number(self.transition_duration_var, '转场时长'))
            transitions = [value for value in self.ws.project.transitions if not self._at_cut(value, time)] + [item]
            transitions.sort(key=lambda value: value.time)
            validate_transitions(transitions, self.ws.project)
            self._commit(lambda: setattr(self.ws.project, 'transitions', transitions), '转场已应用。')
        except (ValueError, tk.TclError) as error:
            self._error(error)

    def remove_transition(self):
        if not self.ws._available():
            return
        try:
            time = self._selected_cut_time()
            transitions = [item for item in self.ws.project.transitions if not self._at_cut(item, time)]
            if len(transitions) != len(self.ws.project.transitions):
                self._commit(lambda: setattr(self.ws.project, 'transitions', transitions), '转场已删除。')
        except ValueError as error:
            self._error(error)

    def _build_subtitle(self):
        body = self._page('subtitle', '字幕')
        self._section(body, 'S1 字幕轨道')
        self._actions(body, [('导入 / 替换 SRT…', self.import_srt), ('导出 SRT…', self.export_srt)])
        tk.Label(body, textvariable=self.subtitle_source_var, bg=PANEL, fg=MUTED, font=FONT,
                 anchor='w', wraplength=self.px(240), justify='left').pack(fill='x', padx=self.px(10))
        self._check(body, '启用字幕轨道', self.subtitle_enabled_var)
        self._check(body, '烧录到成片', self.subtitle_burn_var)
        self._row(body, '整轨偏移', self.subtitle_offset_var)
        self._note(body, 'SRT 只含文字与时间。样式保存在工程中；勾选烧录后进入成片。也可单独导出 SRT。').pack(fill='x', padx=self.px(10))
        frame = tk.Frame(body, bg=PANEL)
        frame.pack(fill='x', padx=self.px(10), pady=self.px(7))
        self.subtitle_tree = ttk.Treeview(frame, columns=('start', 'end'), show='tree headings',
                                          height=5, selectmode='browse', style='Multicam.Treeview')
        self.subtitle_tree.heading('#0', text='字幕文字')
        self.subtitle_tree.heading('start', text='开始')
        self.subtitle_tree.heading('end', text='结束')
        self.subtitle_tree.column('#0', width=self.px(104), minwidth=self.px(40))
        self.subtitle_tree.column('start', width=self.px(66), minwidth=self.px(36), stretch=False)
        self.subtitle_tree.column('end', width=self.px(66), minwidth=self.px(36), stretch=False)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.subtitle_tree.yview)
        self.subtitle_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.subtitle_tree.pack(fill='x', expand=True)
        self.subtitle_tree.bind('<<TreeviewSelect>>', self._load_cue)
        self.subtitle_tree.bind('<Double-1>', self._seek_cue)
        self._note(body, '双击字幕跳到时间线；以下时间为文件时间，另加整轨偏移。').pack(fill='x', padx=self.px(10))
        self._row(body, '字幕开始', self.subtitle_start_var)
        self._row(body, '字幕结束', self.subtitle_end_var)
        self.subtitle_text = tk.Text(body, height=3, width=1, wrap='word', bg=INPUT, fg=INK,
                                     insertbackground=INK, relief='flat', font=FONT, undo=True)
        self.subtitle_text.pack(fill='x', padx=self.px(10), pady=self.px(4))
        self._actions(body, [('添加', self.add_cue), ('保存文字', self.save_cue), ('删除', self.delete_cue)])
        self._section(body, '整轨样式（按 1080p 设计）')
        string_fields = ('font_family', 'font_size', 'color', 'outline_color', 'outline_width',
                         'shadow', 'alignment', 'margin_x', 'margin_y', 'background_color',
                         'background_opacity')
        self.style_vars = {name: tk.StringVar(self) for name in string_fields}
        for name in ('bold', 'italic', 'box'):
            self.style_vars[name] = tk.BooleanVar(self)
        fonts = sorted(set(tkfont.families(self)))
        self._row(body, '字体', self.style_vars['font_family'], choices=fonts).configure(state='normal')
        for label, name in [('字号', 'font_size'), ('描边宽度', 'outline_width'), ('阴影', 'shadow')]:
            self._row(body, label, self.style_vars[name])
        for label, name in [('文字颜色', 'color'), ('描边颜色', 'outline_color')]:
            self._row(body, label, self.style_vars[name], color=True)
        self._check(body, '粗体', self.style_vars['bold'])
        self._check(body, '斜体', self.style_vars['italic'])
        self._row(body, '画面位置', self.style_vars['alignment'], choices=list(ALIGNMENTS))
        self._row(body, '水平边距', self.style_vars['margin_x'])
        self._row(body, '垂直边距', self.style_vars['margin_y'])
        self._check(body, '使用字幕底框', self.style_vars['box'])
        self._row(body, '底框颜色', self.style_vars['background_color'], color=True)
        self._row(body, '底框不透明 %', self.style_vars['background_opacity'])
        self._actions(body, [('应用样式与轨道', self.apply_subtitle_style)])

    def _choose_color(self, variable):
        if not self.ws._available():
            return
        initial = variable.get().strip()
        try:
            self.winfo_rgb(initial)
        except tk.TclError:
            initial = '#FFFFFF'
        chosen = colorchooser.askcolor(initial, parent=self.winfo_toplevel(), title='字幕颜色')[1]
        if chosen:
            variable.set(chosen.upper())

    def _track_copy(self):
        return copy.deepcopy(self.ws.project.subtitles)

    def _commit_track(self, track, status):
        validate_subtitle_track(track)
        self._commit(lambda: setattr(self.ws.project, 'subtitles', track), status)

    def import_srt(self):
        if not self.ws._available():
            return
        path = filedialog.askopenfilename(parent=self.winfo_toplevel(), title='导入 / 替换字幕轨道',
                                           filetypes=[('SRT 字幕', '*.srt'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            track = self._track_copy()
            track.cues = parse_srt(path)
            track.source_path = path
            track.offset = 0.0
            self._commit_track(track, f'已导入 {len(track.cues)} 条字幕。')
        except (ValueError, OSError, UnicodeError) as error:
            self._error(error)

    def export_srt(self):
        if not self.ws._available():
            return
        try:
            validate_subtitle_track(self.ws.project.subtitles)
            path = filedialog.asksaveasfilename(parent=self.winfo_toplevel(), title='另存字幕 SRT',
                                               defaultextension='.srt', filetypes=[('SRT 字幕', '*.srt')])
            if path:
                track = self._track_copy()
                track.enabled = True  # Explicit save also works for a hidden track.
                write_srt(track, path)
                if hasattr(self.ws, 'status'):
                    self.ws.status.set(f'字幕已另存：{Path(path).name}')
        except (ValueError, OSError) as error:
            self._error(error)

    def _load_cue(self, event=None):
        if self.loading:
            return
        selection = self.subtitle_tree.selection()
        self._selected_cue_id = selection[0] if selection else None
        cue = next((item for item in self.ws.project.subtitles.cues if item.id == self._selected_cue_id), None)
        if cue:
            self.subtitle_start_var.set(format_time(cue.start))
            self.subtitle_end_var.set(format_time(cue.end))
            self.subtitle_text.delete('1.0', 'end')
            self.subtitle_text.insert('1.0', cue.text)

    def _seek_cue(self, event=None):
        self._load_cue()
        cue = next((item for item in self.ws.project.subtitles.cues if item.id == self._selected_cue_id), None)
        if cue and hasattr(self.ws, 'seek'):
            self.ws.seek(max(0, cue.start + self.ws.project.subtitles.offset))

    def add_cue(self):
        if not self.ws._available():
            return
        try:
            track = self._track_copy()
            start = max(0, float(getattr(self.ws, 'current', 0)) - track.offset)
            cue = SubtitleCue(uuid.uuid4().hex, start, start + 2, '新字幕')
            track.cues.append(cue)
            track.cues.sort(key=lambda item: (item.start, item.end))
            self._selected_cue_id = cue.id
            self._commit_track(track, '已添加字幕；编辑文字与时间后保存。')
            self.subtitle_tree.selection_set(cue.id)
            self.subtitle_tree.see(cue.id)
            self._load_cue()
        except (ValueError, tk.TclError) as error:
            self._error(error)

    def save_cue(self):
        if not self.ws._available():
            return
        try:
            track = self._track_copy()
            cue = next((item for item in track.cues if item.id == self._selected_cue_id), None)
            if cue is None:
                raise ValueError('请先选择一条字幕，或点击添加。')
            cue.start = parse_timecode(self.subtitle_start_var.get())
            cue.end = parse_timecode(self.subtitle_end_var.get())
            cue.text = self.subtitle_text.get('1.0', 'end-1c')
            track.cues.sort(key=lambda item: (item.start, item.end))
            self._commit_track(track, '字幕文字与时间已保存。')
        except (ValueError, tk.TclError) as error:
            self._error(error)

    def delete_cue(self):
        if not self.ws._available():
            return
        track = self._track_copy()
        track.cues = [item for item in track.cues if item.id != self._selected_cue_id]
        if len(track.cues) != len(self.ws.project.subtitles.cues):
            self._commit_track(track, '字幕已删除。')

    def apply_subtitle_style(self):
        if not self.ws._available():
            return
        try:
            track = self._track_copy()
            track.offset = parse_signed_timecode(self.subtitle_offset_var.get())
            track.enabled = self.subtitle_enabled_var.get()
            track.burn_in = self.subtitle_burn_var.get()
            for name in ('font_family', 'color', 'outline_color', 'background_color'):
                setattr(track.style, name, self.style_vars[name].get().strip())
            labels = {'font_size': '字幕字号', 'outline_width': '描边宽度', 'shadow': '字幕阴影',
                      'margin_x': '水平边距', 'margin_y': '垂直边距'}
            for name in ('font_size', 'outline_width', 'shadow'):
                setattr(track.style, name, self._number(self.style_vars[name], labels[name]))
            for name in ('margin_x', 'margin_y'):
                setattr(track.style, name, self._number(self.style_vars[name], labels[name], integer=True))
            for name in ('bold', 'italic', 'box'):
                setattr(track.style, name, self.style_vars[name].get())
            if self.style_vars['alignment'].get() not in ALIGNMENTS:
                raise ValueError('请选择字幕画面位置。')
            track.style.alignment = ALIGNMENTS[self.style_vars['alignment'].get()]
            track.style.background_opacity = self._number(self.style_vars['background_opacity'], '底框不透明度') / 100
            self._commit_track(track, '字幕样式与轨道设置已应用。')
        except (ValueError, tk.TclError) as error:
            self._error(error)

    def refresh(self):
        self.loading = True
        try:
            scopes = ['全片'] + [f'整机位 {camera}' for camera in self.ws.project.cameras]
            selected = getattr(self.ws, 'selected_program_range', None)
            selected = selected() if callable(selected) else selected
            if selected is not None and callable(getattr(self.ws, 'get_selected_grade', None)):
                scopes.insert(0, grade_target_label(self.ws, 'selection'))
            self.scope_combo.configure(values=scopes)
            if hasattr(self.ws, 'grade_target'):
                target = self.ws.grade_target
                self.grade_scope_var.set(grade_target_label(self.ws, target))
            else:
                # Read old labels that callers and pre-realtime stubs may set.
                try:
                    self.grade_scope_var.set(grade_target_label(self.ws, grade_target_from_label(self.grade_scope_var.get())))
                except ValueError:
                    pass
            if self.grade_scope_var.get() not in scopes:
                self.grade_scope_var.set('全片')
            self._load_grade()
            boundaries = {cut.time for cut in self.ws.project.cuts}
            boundaries.update(getattr(self.ws.project, 'edit_points', []))
            self._cut_choices = {}
            for time in sorted(boundaries):
                if math.floor(time * self.ws.project.fps + 0.5) <= 0:
                    continue
                camera_at = getattr(self.ws.project, 'camera_at', None)
                if callable(camera_at):
                    camera = camera_at(time)
                else:
                    preceding = [cut for cut in self.ws.project.cuts if cut.time <= time]
                    camera = max(preceding, key=lambda cut: cut.time).camera if preceding else 1
                self._cut_choices[f'{format_time(time)} · 机位 {camera}'] = time
            self.cut_combo.configure(values=list(self._cut_choices))
            selected = getattr(self.ws, 'selected_cut', None)
            if self.transition_cut_var.get() not in self._cut_choices or selected != self._last_ws_selected_cut:
                if isinstance(selected, int) and 0 <= selected < len(self.ws.project.cuts):
                    time = self.ws.project.cuts[selected].time
                    label = next((key for key, value in self._cut_choices.items() if abs(value-time) < 1e-6), '')
                else:
                    label = next(iter(self._cut_choices), '')
                self.transition_cut_var.set(label)
            self._last_ws_selected_cut = selected
            self._load_transition()
            names = {kind: label for label, kind in TRANSITIONS.items()}
            self.transition_summary_var.set('\n'.join(f'{format_time(item.time)} · {names.get(item.kind, item.kind)} {item.duration:g}s'
                                                       for item in self.ws.project.transitions) or '当前没有转场')
            track = self.ws.project.subtitles
            self.subtitle_offset_var.set(format_signed_time(track.offset))
            self.subtitle_enabled_var.set(track.enabled)
            self.subtitle_burn_var.set(track.burn_in)
            self.subtitle_source_var.set((Path(track.source_path).name if track.source_path else '手动字幕')
                                         + f' · {len(track.cues)} 条')
            self.subtitle_tree.delete(*self.subtitle_tree.get_children())
            for cue in sorted(track.cues, key=lambda item: (item.start, item.end)):
                self.subtitle_tree.insert('', 'end', iid=cue.id, text=cue.text.replace('\n', ' '),
                                          values=(format_time(cue.start), format_time(cue.end)))
            if self._selected_cue_id and self.subtitle_tree.exists(self._selected_cue_id):
                self.subtitle_tree.selection_set(self._selected_cue_id)
            else:
                self._selected_cue_id = None
                self.subtitle_text.delete('1.0', 'end')
            for name, variable in self.style_vars.items():
                value = getattr(track.style, name)
                if name == 'alignment':
                    value = next(label for label, number in ALIGNMENTS.items() if number == value)
                elif name == 'background_opacity':
                    value = f'{value * 100:g}'
                elif isinstance(value, (float, int)) and not isinstance(value, bool):
                    value = f'{value:g}'
                variable.set(value)
        finally:
            self.loading = False
        self._load_cue()

    refresh_from_project = refresh

    def select(self, tab='color'):
        if tab in self.pages:
            self.tabs.select(self.pages[tab])


def build_creative_panel(workspace, inspector_tabs):
    """Attach one outer inspector tab and expose refresh/select hooks."""
    # A common workspace ancestor lets the outer panel dock into another
    # notebook without replacing its widgets or losing edited field values.
    parent = workspace if isinstance(workspace, tk.Misc) else inspector_tabs
    outer = tk.Frame(parent, bg=PANEL)
    inspector_tabs.add(outer, text='效果与字幕')
    panel = CreativePanel(outer, workspace)
    panel.pack(fill='both', expand=True)
    workspace.creative_tab = outer
    workspace.creative_panel = panel
    workspace.creative_panel_refresh = panel.refresh
    return outer
