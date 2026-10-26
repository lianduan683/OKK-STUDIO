"""Interactive per-target colour curves, with one transaction per gesture."""
from __future__ import annotations

import copy
import math
import tkinter as tk
from tkinter import ttk

from creative_core import ColorGrade, curve_points, replace_curve
from color_curves import evaluate_curve, MIN_CURVE_SPACING


BG, PANEL, INPUT = '#202124', '#292b2e', '#18191b'
INK, MUTED, ACCENT = '#dedfe3', '#969ba3', '#58a6d6'
CHANNELS = {'主亮度': 'master', '红色 R': 'r', '绿色 G': 'g', '蓝色 B': 'b'}
COLORS = {'master': '#e2e4e7', 'r': '#ec7773', 'g': '#85c295', 'b': '#7ba9df'}
IDENTITY = ((0.0, 0.0), (1.0, 1.0))
MAX_POINTS = 8


class CurveEditor(tk.Frame):
    def __init__(self, parent, owner):
        super().__init__(parent, bg=BG)
        self.owner = owner
        self.grade = ColorGrade()
        self.draft = None
        self._gesture_target = None
        self._drag_index = None
        self.selected_index = None
        self._plot = (0, 0, 1, 1)
        self.channel_var = tk.StringVar(self, value='主亮度')
        self.info_var = tk.StringVar(self, value='2 / 8 个控制点')
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill='x', padx=self.px(6), pady=(self.px(4), 0))
        tk.Label(bar, text='曲线', bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=self.px(8))
        self.channel_combo = ttk.Combobox(bar, textvariable=self.channel_var,
                                          values=list(CHANNELS), width=11, state='readonly')
        self.channel_combo.pack(side='left', padx=self.px(3), pady=self.px(4))
        self.channel_combo.bind('<<ComboboxSelected>>', self._channel_changed)
        ttk.Button(bar, text='重置全部曲线', command=self.reset_all,
                   style='Compact.TButton').pack(side='right', padx=self.px(4))
        ttk.Button(bar, text='重置当前通道', command=self.reset_channel,
                   style='Compact.TButton').pack(side='right', padx=self.px(4))
        self.canvas = tk.Canvas(self, bg=INPUT, width=self.px(600), height=self.px(190),
                                highlightthickness=0, cursor='crosshair', takefocus=True)
        self.canvas.pack(fill='both', expand=True, padx=self.px(6), pady=self.px(4))
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._motion)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self.canvas.bind('<Button-3>', self._delete_point)
        self.canvas.bind('<FocusOut>', self._focus_out)
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill='x', padx=self.px(12), pady=(0, self.px(5)))
        tk.Label(footer, textvariable=self.info_var, bg=BG, fg=ACCENT,
                 font=('Cascadia Mono', 9)).pack(side='left')
        self.hint = tk.Label(footer, text='点击加点 · 拖动实时预览，松开应用 · 右键删内部点 · 端点固定横坐标；'
                            '与上方调色目标共用，默认只调所选 Cut。', bg=BG, fg=MUTED,
                            justify='left', anchor='w', font=('Microsoft YaHei UI', 8))
        self.hint.pack(side='left', fill='x', expand=True, padx=self.px(12))
        footer.bind('<Configure>', lambda event: self.hint.configure(
            wraplength=max(self.px(140), event.width - self.px(210))))

    def px(self, value):
        return self.owner.px(value)

    @property
    def channel(self):
        return CHANNELS.get(self.channel_var.get(), 'master')

    def load_grade(self, grade):
        if self.draft is None:
            self.grade = copy.deepcopy(grade)
            self.draw()

    def cancel_draft(self, clear_preview=True):
        self.draft = None
        self._gesture_target = None
        self._drag_index = None
        self.selected_index = None
        self.grade = self.owner.get_grade()
        if clear_preview:
            self.owner.clear_preview()
        self.draw()

    def _channel_changed(self, event=None):
        self.owner.cancel_drafts()
        self.selected_index = None
        self.draw()

    def _to_screen(self, x, y):
        left, top, right, bottom = self._plot
        return left + float(x) * (right-left), bottom - float(y) * (bottom-top)

    def _to_value(self, x, y):
        left, top, right, bottom = self._plot
        return (min(1.0, max(0.0, (float(x)-left)/max(1, right-left))),
                min(1.0, max(0.0, (bottom-float(y))/max(1, bottom-top))))

    def _inside(self, event):
        left, top, right, bottom = self._plot
        return left <= event.x <= right and top <= event.y <= bottom

    def draw(self):
        self.canvas.delete('all')
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        available = max(1, height-self.px(38))
        plot_width = min(max(1, width-self.px(60)), available*3.5)
        left = max(self.px(34), (width-plot_width)/2)
        self._plot = left, self.px(10), left+plot_width, max(self.px(20), height-self.px(27))
        left, top, right, bottom = self._plot
        self.canvas.create_rectangle(left, top, right, bottom, fill='#202226', outline='#4b4f55')
        for index in range(1, 4):
            x = left + (right-left)*index/4
            y = top + (bottom-top)*index/4
            self.canvas.create_line(x, top, x, bottom, fill='#35383e')
            self.canvas.create_line(left, y, right, y, fill='#35383e')
        self.canvas.create_line(left, bottom, right, top, fill='#555a62', dash=(self.px(3), self.px(4)))
        for text, x, y, anchor in (('0', left-self.px(7), bottom, 'e'),
                                     ('1', left-self.px(7), top, 'e'),
                                     ('0', left, bottom+self.px(7), 'n'),
                                     ('1', right, bottom+self.px(7), 'n')):
            self.canvas.create_text(x, y, text=text, anchor=anchor, fill=MUTED,
                                      font=('Cascadia Mono', 8))
        self.canvas.create_text((left+right)/2, bottom+self.px(7), text='输入亮度 →',
                                  anchor='n', fill=MUTED, font=('Microsoft YaHei UI', 8))
        grade = self.draft or self.grade
        selected = self.channel
        for channel in [item for item in CHANNELS.values() if item != selected] + [selected]:
            points = curve_points(grade, channel)
            if channel != selected and points == IDENTITY:
                continue
            coordinates = []
            for index in range(257):
                x = index/256
                y = min(1.0, max(0.0, evaluate_curve(points, x)))
                coordinates.extend(self._to_screen(x, y))
            self.canvas.create_line(*coordinates, fill=COLORS[channel],
                                      width=max(1, self.px(2) if channel == selected else self.px(1)),
                                      tags='curve')
        points = curve_points(grade, selected)
        for index, (x, y) in enumerate(points):
            sx, sy = self._to_screen(x, y)
            radius = self.px(5 if index == self.selected_index else 4)
            shape = self.canvas.create_rectangle if index in (0, len(points)-1) else self.canvas.create_oval
            shape(sx-radius, sy-radius, sx+radius, sy+radius, fill=COLORS[selected],
                  outline=INK if index == self.selected_index else INPUT,
                  width=max(1, self.px(1)), tags=('point', f'point{index}'))
        if self.selected_index is not None and self.selected_index < len(points):
            x, y = points[self.selected_index]
            self.info_var.set(f'{len(points)} / 8 点 · 输入 {x:.3f} → 输出 {y:.3f}')
        else:
            self.info_var.set(f'{len(points)} / 8 个控制点')

    def _nearest(self, event, points):
        best = None
        distance = self.px(11)**2
        for index, point in enumerate(points):
            x, y = self._to_screen(*point)
            squared = (event.x-x)**2 + (event.y-y)**2
            if squared <= distance:
                best, distance = index, squared
        return best

    def _press(self, event):
        if not self.owner.available() or not self._inside(event):
            return 'break'
        self.owner.cancel_drafts()
        self.canvas.focus_set()
        self._gesture_target = self.owner.target_token()
        self.draft = self.owner.get_grade()
        points = list(curve_points(self.draft, self.channel))
        index = self._nearest(event, points)
        if index is None:
            if len(points) >= MAX_POINTS:
                self.info_var.set('最多 8 个控制点；右键删除内部点后可继续添加。')
                self.draft = None
                return 'break'
            x, y = self._to_value(event.x, event.y)
            if x <= .001 or x >= .999:
                self.draft = None
                return 'break'
            if any(abs(x-px) < .001 for px, _ in points):
                self.draft = None
                return 'break'
            points.append((x, y))
            points.sort()
            index = points.index((x, y))
            self.draft = replace_curve(self.draft, self.channel, tuple(points))
            self.owner.preview_grade(self.draft)
        self._drag_index = self.selected_index = index
        self.draw()
        return 'break'

    def _motion(self, event):
        if self.draft is None or self._drag_index is None:
            return 'break'
        if self._gesture_target != self.owner.target_token():
            self.cancel_draft()
            return 'break'
        points = list(curve_points(self.draft, self.channel))
        index = self._drag_index
        x, y = self._to_value(event.x, event.y)
        if index == 0:
            x = 0.0
        elif index == len(points)-1:
            x = 1.0
        else:
            gap = min(.001, (points[index+1][0]-points[index-1][0])/3)
            if gap < MIN_CURVE_SPACING*1.01:
                x = points[index][0]
            else:
                x = min(points[index+1][0]-gap, max(points[index-1][0]+gap, x))
        points[index] = x, y
        self.draft = replace_curve(self.draft, self.channel, tuple(points))
        self.owner.preview_grade(self.draft)
        self.draw()
        return 'break'

    def _release(self, event=None):
        if self.draft is not None:
            grade = self.draft
            token = self._gesture_target
            self.draft = None
            self._drag_index = self._gesture_target = None
            if token == self.owner.target_token():
                self._commit(grade)
            else:
                self.cancel_draft()
        return 'break'

    def _focus_out(self, event=None):
        if self.draft is not None:
            self.cancel_draft()

    def _delete_point(self, event):
        if not self.owner.available() or not self._inside(event):
            return 'break'
        self.owner.cancel_drafts()
        grade = self.owner.get_grade()
        points = list(curve_points(grade, self.channel))
        index = self._nearest(event, points)
        if index is not None and index not in (0, len(points)-1):
            del points[index]
            self.selected_index = None
            self._commit(replace_curve(grade, self.channel, tuple(points)))
        return 'break'

    def _commit(self, grade):
        try:
            if grade != self.owner.get_grade():
                self.owner.apply_grade(copy.deepcopy(grade))
            else:
                self.owner.clear_preview()
            self.owner.refresh()
        except (ValueError, TypeError, tk.TclError) as error:
            self.owner.show_error(error)
            self.cancel_draft()

    def reset_channel(self):
        if self.owner.available():
            self.owner.cancel_drafts()
            self._commit(replace_curve(self.owner.get_grade(), self.channel, IDENTITY))

    def reset_all(self):
        if self.owner.available():
            self.owner.cancel_drafts()
            grade = self.owner.get_grade()
            for channel in CHANNELS.values():
                grade = replace_curve(grade, channel, IDENTITY)
            self._commit(grade)
