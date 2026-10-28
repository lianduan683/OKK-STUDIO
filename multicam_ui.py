"""Native multi-camera editing workspace for RTX Video Render Studio."""
from __future__ import annotations

import copy
import math
import os
import json
import subprocess
import queue
import threading
import time
import uuid
import tempfile
import shutil
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

from editor_core import (align_audio_segments, format_time, format_signed_time,
                         parse_timecode, parse_signed_timecode, probe_media_duration,
                         normalize_video_bitrate)
from exporter_core import CREATE_NO_WINDOW, ExportCancelled, ExportSettings, find_binary, probe_media
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from multicam_player import MulticamPlayer
from delivery_panel import init_delivery_variables, build_delivery_panel, make_delivery_settings
from creative_panel import build_creative_panel
from subtitle_core import subtitle_at
from multicam_frame_clock import canonical_frame_rate
from creative_core import ColorGrade, Transition, validate_grade, validate_transitions
from creative_render import plan_creative_segments
from program_edits import program_spans, split_at, set_camera_on_range, set_grade_on_range, grade_at
from color_workspace import ColorWorkspace
from effect_workspace import EffectsWorkspaceMixin
from stabilization_panel import open_stabilization_dialog
from image_stabilization_panel import open_image_stabilization_dialog
from cache_paths import cache_dir
from camera_metadata import CameraMetadata, read_camera_metadata
from auto_color_workspace import AutoColorWorkspaceMixin
from reference_sync import align_to_camera
from camera_import import batch_import
from timeline_interaction import TimelineInteractionMixin
from proxy_media import validate_proxy_media
from proxy_cache import register_proxy
from library_apply import LibraryApplyMixin
from multicam_proxy import MulticamProxyMixin
from clip_inspector import ClipInspector
from transport_controls import TransportControlsMixin
from workspace_docking import WorkspaceDockingMixin
from video_stack_ui import VideoStackUIMixin
from stack_preview_ui import StackPreviewUIMixin

BG = '#202124'
PANEL = '#292b2e'
INPUT = '#18191b'
INK = '#dedfe3'
MUTED = '#969ba3'
ACCENT = '#58a6d6'
PINK = '#ef675d'  # Playback and recording indicator, kept for compatibility.
GREEN = '#73aa9c'
COLORS = ['#496e87', '#4b8289', '#667e9e', '#64867c', '#7c718d', '#7e8870', '#807a65', '#557f92', '#8c7374']
VIDEO_TYPES = [('视频', '*.mp4 *.mov *.mkv *.mxf *.avi *.mts *.m2ts *.webm'), ('所有文件', '*.*')]


class MulticamWorkspace(StackPreviewUIMixin, VideoStackUIMixin, WorkspaceDockingMixin, TransportControlsMixin, TimelineInteractionMixin, MulticamProxyMixin, LibraryApplyMixin, AutoColorWorkspaceMixin, EffectsWorkspaceMixin, tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.ui_scale = max(1.0, self.winfo_fpixels('1i') / 96.0)
        self._media_width = self._px(304)
        self._inspector_width = self._px(400)
        self._timeline_left = max(self._px(88), tkfont.Font(self, family='Microsoft YaHei UI', size=8).measure('A1 主音轨') + self._px(16))
        self._timeline_label_font = tkfont.Font(self, family='Microsoft YaHei UI', size=8)
        self._timeline_title_font = tkfont.Font(self, family='Microsoft YaHei UI', size=9)
        self._timeline_ruler_font = tkfont.Font(self, family='Cascadia Mono', size=8)
        self.project = MulticamProject()
        self.project_path = ''
        self.dirty = False
        self.undo_stack = []
        self.redo_stack = []
        self.current = 0.0
        self.playing = False
        self.busy = False
        self.sync_running = False
        self.sync_results = {}
        self.player = None
        self.player_queue = queue.Queue()
        self.jobs = queue.Queue()
        self.selected_cut = None
        self.selected_program_range = None
        self.grade_target = None
        self._grade_target_chosen = False
        self._grade_preview_after = None
        self._grade_preview_pending = None
        self._grade_preview_active = False
        self._color_mode = False
        self._timeline_saved_sash = None
        self.timeline_program_items = {}
        self.zoom = 1.0
        self.view_start = 0.0
        self.timeline_drag = None
        self.subtitle_drag = None
        self._init_timeline_interaction()
        self.timeline_subtitle_items = {}
        self.timeline_transition_items = {}
        self._creative_preview_folder = None
        self._creative_preview_worker = None
        self._creative_preview_sequence = 0
        self._camera_metadata_cancel = threading.Event()
        self._camera_metadata_attempted = set()
        self._camera_metadata_worker = None
        self._displayed_camera = None
        self.camera_page=0
        self.camera_page_var=tk.StringVar(value='1/1')
        self.camera_rows = self.camera_columns = 3
        self.camera_rows_var = tk.StringVar(self, value='3')
        self.camera_columns_var = tk.StringVar(self, value='3')
        self._camera_grid_layout = None
        self._visible_camera_ids=()
        self._retired_camera_frames={}
        self._init_effects()
        self._init_auto_color()
        self.alive = True
        self.record_var = tk.BooleanVar(value=True)
        self.sync_mode_var = tk.StringVar(value='中段波形（推荐）')
        self.sync_reference_var=tk.StringVar(value='主音频')
        self._sync_reference_choices={'主音频':None}
        self._sync_reference_combos=[]
        self._alignment_reference_label='主音频'
        self.stabilization_compare_var=tk.BooleanVar(value=False)
        self._comparison_layout_enabled=False
        self._comparison_requested=None
        self.proxy_var = tk.BooleanVar(value=True)
        self.gpu_live_var = tk.BooleanVar(value=True)
        self._gpu_live_active = False
        self._gpu_live_failed = False
        self.status = tk.StringVar(value='添加机位开始剪辑 · 空格播放 / 暂停 · 数字键 1–9 切换机位')
        self.time_var = tk.StringVar(value='00:00:00.000 / 00:00:00.000')
        self.master_var = tk.StringVar(value='主音频：随首个机位导入')
        self.offset_var = tk.StringVar(value='00:00:00.000')
        self.seek_var = tk.StringVar(value='00:00:00.000')
        self.start_var = tk.StringVar(value='00:00:00.000')
        self.end_var = tk.StringVar()
        self.cut_time_var = tk.StringVar()
        self.cut_camera_var = tk.StringVar(value='1')
        self.bitrate_var = tk.StringVar(value='80M')
        self.export_mode = tk.StringVar(value='自动（沿用统一编码设置）')
        self.fps_var = tk.StringVar(value='30')
        self.output_var = tk.StringVar()
        self.program_var = tk.StringVar(value='成片监看 · 机位 —')
        self.export_metrics_var = tk.StringVar(value='等待导出')
        self.export_speed_var = tk.StringVar(value='近3秒：— FPS · —×')
        self.export_elapsed_var = tk.StringVar(value='00:00:00')
        self.export_eta_var = tk.StringVar(value='—')
        self.export_phase_var = tk.StringVar(value='就绪')
        self.export_progress_var = tk.DoubleVar(value=0)
        self._export_started_at = None
        self._export_last_snapshot = {}
        init_delivery_variables(self)
        self._init_quick_proxy()
        self._init_video_stack_ui()
        self._init_stack_preview()
        self._init_transport_controls()
        self._build()
        self._init_workspace_docking()
        self._bind_timeline_interaction()
        self._bind_transport_controls()
        self._loading_fields = False
        for variable in (self.start_var, self.end_var, self.fps_var):
            variable.trace_add('write', self._field_changed)
        self.app.bind('<KeyPress>', self._key, add='+')
        self.after(80, self._poll)

    def _field_changed(self, *args):
        if not self._loading_fields:
            self._mark_dirty()

    def _px(self, value):
        return max(0, round(value * self.ui_scale))

    def _button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=command, style='Compact.TButton')
        button.pack(side='left', padx=self._px(2), pady=self._px(2), **pack)
        return button

    def _label(self, parent, text=None, variable=None):
        return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget('bg'), fg=MUTED,
                        font=('Microsoft YaHei UI', 9), anchor='w')

    def _entry(self, parent, variable, width=15):
        return tk.Entry(parent, textvariable=variable, width=width, bg=INPUT, fg=INK,
                        insertbackground=INK, relief='flat', highlightthickness=1,
                        highlightbackground='#3a3c40', highlightcolor=ACCENT,
                        font=('Cascadia Mono', 10))

    def _clip_camera_label(self,clip):
        return CameraMetadata(clip.camera_make,clip.camera_model,clip.camera_metadata_source).camera_label

    def _label_camera(self,camera,time=None):
        if camera is None: return '机位 —'
        clip = self.project.clip_at(camera,self.current if time is None else time)
        if clip is None: clip = next((clip for clip in self.project.clips if clip.camera==camera),None)
        label = self._clip_camera_label(clip) if clip else '未知相机'
        return f'机位 {camera} · {label}'

    def _camera_button_label(self,camera):
        visible=getattr(self,'_visible_camera_ids',())
        if camera in visible and (self.camera_columns > 3 or self.camera_rows > 3):
            index = visible.index(camera)+1
            return f'{index}:{camera}' if index <= 9 and index != camera else str(camera)
        if camera in visible and len(visible)>4:
            model=self._label_camera(camera).split(' · ',1)[-1]
            if model=='未知相机':model='未知'
            shortcut=visible.index(camera)+1
            return (f'{shortcut}:{camera}' if shortcut!=camera else str(camera))+' '+model
        if camera in visible and visible.index(camera)+1 != camera:
            return f'{visible.index(camera)+1} · {camera}/'+self._label_camera(camera).split(' · ',1)[-1]
        return self._label_camera(camera)

    def _set_program_label(self,camera,preview=False):
        if self.project.video_stack_enabled and not preview:
            self.program_var.set(f'图层合成 · {len(self.project.stacked_cameras())} 层 · 选择 V{self._displayed_camera or camera or "—"}')
            return
        self._displayed_camera = camera
        if preview:
            text = self._label_camera(camera)+' · 效果预览'
        elif self._color_mode:
            scope = '所选片段' if self.grade_target=='selection' else '全片' if self.grade_target is None else '整机位'
            text = self._label_camera(camera)+f' · {scope}调色'
        else: text = self._label_camera(camera)+' · 成片监看'
        self.program_var.set(text)

    def _refresh_camera_labels(self):
        for camera,button in self.camera_buttons.items():
            label = self._camera_button_label(camera)
            if button.cget('text') != label: button.configure(text=label)
        for camera in self.project.cameras:
            parent = f'camera_{camera}'
            if self.media_tree.exists(parent): self.media_tree.item(parent,text=self._label_camera(camera))
        for clip in self.project.clips:
            if self.media_tree.exists(clip.id):
                text = f'{Path(clip.path).name} · {self._clip_camera_label(clip)}'
                result = self.sync_results.get(clip.id)
                if result is not None: text += ' · 已对齐' if result.confidence >= .2 else ' · 待检查'
                self.media_tree.item(clip.id,text=text)
        camera=self._displayed_camera if self._displayed_camera in self.project.cameras else self.project.camera_at(self.current)
        self._set_program_label(camera)
        self._refresh_sync_reference_choices()
        workspace=vars(self).get('color_workspace')
        panel=vars(workspace).get('auto_panel') if workspace is not None else None
        if panel is not None:panel.refresh_reference_choices()

    def _refresh_sync_reference_choices(self):
        self._sync_reference_choices={'主音频':None}
        self._sync_reference_choices.update({self._label_camera(camera):camera for camera in self.project.cameras})
        reference=self.project.alignment_reference_camera
        if reference not in self.project.cameras: self.project.alignment_reference_camera=reference=None
        label=next((label for label,value in self._sync_reference_choices.items() if value==reference),'主音频')
        self.sync_reference_var.set(label)
        for combo in self._sync_reference_combos:combo.configure(values=list(self._sync_reference_choices))

    def _sync_reference_changed(self,event=None):
        if self.busy or self.sync_running:
            self._refresh_sync_reference_choices();return
        reference=self._sync_reference_choices.get(self.sync_reference_var.get())
        if reference!=self.project.alignment_reference_camera:
            self.project.alignment_reference_camera=reference;self._mark_dirty()

    def _schedule_camera_metadata(self):
        if not self.alive or (self._camera_metadata_worker and self._camera_metadata_worker.is_alive()): return
        ffprobe = self.app.caps.ffprobe_path
        if not ffprobe or not Path(ffprobe).is_file(): return
        sources = []
        for clip in self.project.clips:
            if clip.camera_make or clip.camera_model: continue
            try:
                stat = Path(clip.path).stat();key=(clip.path,stat.st_size,stat.st_mtime_ns)
            except OSError: continue
            if key in self._camera_metadata_attempted: continue
            self._camera_metadata_attempted.add(key);sources.append((clip.id,clip.path))
        if not sources: return
        def worker():
            for identifier,path in sources:
                if self._camera_metadata_cancel.is_set(): return
                try: metadata=read_camera_metadata(path,ffprobe,cancel_event=self._camera_metadata_cancel)
                except Exception: metadata=CameraMetadata()
                self.jobs.put(('camera_metadata',(identifier,path,metadata)))
        self._camera_metadata_worker=threading.Thread(target=worker,name='camera-metadata',daemon=False)
        self._camera_metadata_worker.start()

    def _build(self):
        style = ttk.Style(self)
        font = tkfont.Font(self, family='Microsoft YaHei UI', size=10)
        style.configure('Multicam.Treeview', background=INPUT, fieldbackground=INPUT,
                        foreground=INK, borderwidth=0, font=('Microsoft YaHei UI', 10),
                        rowheight=font.metrics('linespace') + self._px(10))
        style.configure('Multicam.Treeview.Heading', background=PANEL, foreground=MUTED,
                        font=('Microsoft YaHei UI', 9))
        style.map('Multicam.Treeview', background=[('selected', '#3c5263')],
                  foreground=[('selected', INK)])
        style.configure('Multicam.TNotebook', background=PANEL, borderwidth=0)
        style.configure('Multicam.TNotebook.Tab', padding=(self._px(14), self._px(6)))
        style.configure('Multicam.TPanedwindow', background=INPUT, sashwidth=5)
        style.configure('Inspector.TButton', font=('Microsoft YaHei UI', 9), padding=(self._px(7), self._px(6)))
        style.configure('Inspector.Multicam.TNotebook.Tab', padding=(self._px(8), self._px(6)))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._media_visible = True
        self._inspector_visible = False
        self._layout_initialized = False

        bar = tk.Frame(self, bg=PANEL, height=self._px(40))
        self.workspace_bar = bar
        bar.grid(row=0, column=0, sticky='ew')
        project_menu = tk.Menu(self, tearoff=False, bg=PANEL, fg=INK,
                               activebackground='#3c5263', activeforeground=INK)
        for title, action in [('新建工程', self.new), ('打开工程…', self.open), ('保存工程', self.save)]:
            project_menu.add_command(label=title, command=action)
        project_menu.add_separator()
        project_menu.add_command(label='操作说明', command=self.help)
        file_button = ttk.Menubutton(bar, text='文件 ▾', menu=project_menu, style='Compact.TMenubutton')
        file_button.pack(side='left', padx=(self._px(8), self._px(4)), pady=self._px(5))
        self._button(bar, '媒体池', self.toggle_media_pool)
        self.project_label = tk.Label(bar, text='未保存工程', bg=PANEL, fg=INK,
                                      font=('Microsoft YaHei UI', 10), anchor='w')
        self.project_label.pack(side='left', padx=self._px(14))
        self._button(bar, '撤销', self.undo)
        self._button(bar, '重做', self.redo)
        self.inspector_button = ttk.Button(bar, text='检查器', command=self.show_inspector,
                                           style='Compact.TButton')
        self.inspector_button.pack(side='right', padx=(self._px(2), self._px(8)), pady=self._px(5))
        ttk.Button(bar, text='导出…', command=self.show_delivery,
                   style='Compact.TButton').pack(side='right', padx=self._px(2), pady=self._px(5))
        ttk.Button(bar, text='特效库', command=self.show_effects_library,
                   style='Compact.TButton').pack(side='right', padx=self._px(2), pady=self._px(5))
        for title, section in [('字幕', 'subtitle'), ('转场', 'transition'), ('调色', 'color')]:
            ttk.Button(bar, text=title, command=lambda key=section: self.show_inspector_tool(key),
                       style='Compact.TButton').pack(side='right', padx=self._px(2), pady=self._px(5))

        self.workspace_split = ttk.Panedwindow(self, orient='vertical', style='Multicam.TPanedwindow')
        self.workspace_split.grid(row=1, column=0, sticky='nsew', pady=(self._px(1), self._px(0)))
        self.body = ttk.Panedwindow(self.workspace_split, orient='horizontal', style='Multicam.TPanedwindow')
        self.workspace_split.add(self.body, weight=3)
        self.sidebar = tk.Frame(self.body, bg=PANEL, width=self._media_width)
        self.sidebar.pack_propagate(False)
        self.body.add(self.sidebar, weight=0)
        self.tools = ttk.Notebook(self.sidebar, style='Multicam.TNotebook')
        self.tools.pack(fill='both', expand=True, padx=self._px(1), pady=(self._px(0), self._px(1)))
        media_tab = self.media_tab = tk.Frame(self, bg=PANEL)
        edit_tab = self.cut_tab = tk.Frame(self, bg=PANEL)
        self.tools.add(media_tab, text='素材')
        self.tools.add(edit_tab, text='切点')

        line = tk.Frame(media_tab, bg=PANEL)
        line.pack(fill='x', padx=self._px(5), pady=self._px(5))
        ttk.Button(line, text='＋ 添加机位', command=self.add_camera,
                   style='Compact.TButton').pack(side='left', padx=self._px(2))
        self._button(line, '追加', self.append_clips)
        self._button(line, '移除', self.remove_clip)
        tree_frame = tk.Frame(media_tab, bg=INPUT)
        tree_frame.pack(fill='both', expand=True, padx=self._px(6))
        self.media_tree = ttk.Treeview(tree_frame, columns=('start', 'duration'), show='tree headings',
                                       height=5, selectmode='browse', style='Multicam.Treeview')
        self.media_tree.heading('#0', text='机位 / 素材')
        self.media_tree.heading('start', text='起点')
        self.media_tree.heading('duration', text='时长')
        self.media_tree.column('#0', width=self._px(140), minwidth=self._px(95))
        self.media_tree.column('start', width=self._px(78), minwidth=self._px(65))
        self.media_tree.column('duration', width=self._px(65), minwidth=self._px(55))
        self.media_tree.pack(side='left', fill='both', expand=True)
        ttk.Scrollbar(tree_frame, orient='vertical', command=self.media_tree.yview).pack(side='right', fill='y')
        self.media_tree.configure(yscrollcommand=tree_frame.winfo_children()[-1].set)
        self.media_tree.bind('<<TreeviewSelect>>', self._select_clip)
        self.media_tree.bind('<Double-1>', self._jump_clip)
        reference_row=tk.Frame(media_tab,bg=PANEL)
        reference_row.pack(fill='x',padx=self._px(8),pady=self._px(3))
        tk.Label(reference_row,text='对齐参考',bg=PANEL,fg=MUTED,font=('Microsoft YaHei UI',9)).pack(side='left')
        reference_combo=ttk.Combobox(reference_row,textvariable=self.sync_reference_var,state='readonly',width=18)
        reference_combo.pack(side='left',fill='x',expand=True,padx=self._px(5))
        reference_combo.bind('<<ComboboxSelected>>',self._sync_reference_changed)
        self._sync_reference_combos.append(reference_combo)
        self.media_tree.tag_configure('synced', foreground=GREEN)
        self.media_tree.tag_configure('review', foreground='#d3b880')
        self._label(media_tab, variable=self.master_var).pack(fill='x', padx=self._px(9), pady=(self._px(7), self._px(2)))
        line = tk.Frame(media_tab, bg=PANEL)
        line.pack(fill='x', padx=self._px(5), pady=(self._px(0), self._px(6)))
        self._button(line, '主音频…', self.choose_master)
        self._button(line, '同步并对齐', self.align)
        self._button(line, '精调', lambda: self.show_inspector('clip'))

        cut_frame = tk.Frame(edit_tab, bg=INPUT)
        cut_frame.pack(fill='both', expand=True, padx=self._px(6), pady=self._px(6))
        self.cut_tree = ttk.Treeview(cut_frame, columns=('time', 'camera'), show='headings',
                                     height=5, selectmode='browse', style='Multicam.Treeview')
        self.cut_tree.heading('time', text='切换时间')
        self.cut_tree.heading('camera', text='机位')
        self.cut_tree.column('time', width=self._px(190), minwidth=self._px(120))
        self.cut_tree.column('camera', width=self._px(55), minwidth=self._px(40), anchor='center')
        self.cut_tree.pack(side='left', fill='both', expand=True)
        cut_scroll = ttk.Scrollbar(cut_frame, orient='vertical', command=self.cut_tree.yview)
        cut_scroll.pack(side='right', fill='y')
        self.cut_tree.configure(yscrollcommand=cut_scroll.set)
        self.cut_tree.bind('<<TreeviewSelect>>', self._select_cut)
        self.cut_tree.bind('<Double-1>', self._jump_cut)
        line = tk.Frame(edit_tab, bg=PANEL)
        line.pack(fill='x', padx=self._px(8), pady=(self._px(2), self._px(5)))
        self._entry(line, self.cut_time_var, 15).pack(side='left', fill='x', expand=True, ipady=self._px(5))
        self.cut_camera_combo = ttk.Combobox(line, textvariable=self.cut_camera_var, values=(),
                     width=4, state='readonly')
        self.cut_camera_combo.pack(side='left', padx=(self._px(6), self._px(0)))
        line = tk.Frame(edit_tab, bg=PANEL)
        line.pack(fill='x', padx=self._px(5), pady=(self._px(0), self._px(6)))
        self._button(line, '更新切点', self.edit_cut)
        self._button(line, '删除', self.delete_cut)

        self.monitors = tk.Frame(self.body, bg=BG)
        self.body.add(self.monitors, weight=1)
        self.monitors.grid_columnconfigure(0, weight=1)
        self.monitors.grid_rowconfigure(0, weight=1)
        monitor_body = self.monitor_body = tk.Frame(self.monitors, bg=INPUT)
        monitor_body.grid(row=0, column=0, sticky='nsew')
        monitor_body.grid_columnconfigure(0, weight=1, uniform='monitor')
        monitor_body.grid_columnconfigure(1, weight=1, uniform='monitor')
        monitor_body.grid_rowconfigure(0, weight=1)
        source = self.source_panel = tk.Frame(self, bg=BG)
        source.grid(in_=monitor_body, row=0, column=0, sticky='nsew', padx=(self._px(0), self._px(2)))
        source.grid_columnconfigure(0, weight=1)
        source.grid_rowconfigure(1, weight=1)
        source_title = self.source_title = tk.Frame(source, bg=PANEL, height=self._px(30))
        source_title.grid(row=0, column=0, sticky='ew')
        tk.Label(source_title, text='多机位', bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10)).pack(side='left', padx=self._px(10), pady=self._px(5))
        self.release_cache_button = ttk.Button(source_title, text='释放缓存',
            command=self.unload_preview_cache, style='Compact.TButton')
        self.release_cache_button.pack(side='right', padx=self._px(3))
        self.camera_pager=tk.Frame(source_title,bg=PANEL)
        self.camera_prev=ttk.Button(self.camera_pager,text='‹',width=2,command=lambda:self._change_camera_page(-1),style='Compact.TButton')
        self.camera_prev.pack(side='left')
        tk.Label(self.camera_pager,textvariable=self.camera_page_var,bg=PANEL,fg=MUTED,font=('Cascadia Mono',8)).pack(side='left',padx=self._px(2))
        self.camera_next=ttk.Button(self.camera_pager,text='›',width=2,command=lambda:self._change_camera_page(1),style='Compact.TButton')
        self.camera_next.pack(side='left')
        self.camera_shortcut_hint=tk.Label(source_title,text='1–9',bg=PANEL,fg=MUTED,font=('Microsoft YaHei UI',9))
        self.camera_shortcut_hint.pack(side='right',padx=self._px(4))
        proxy_tools = tk.Frame(source, bg=PANEL)
        proxy_tools.grid(row=1, column=0, sticky='ew')
        self._build_quick_proxy_controls(proxy_tools)
        self.camera_grid_toolbar = tk.Frame(source, bg=PANEL)
        self.camera_grid_toolbar.grid(row=2, column=0, sticky='ew')
        tk.Label(self.camera_grid_toolbar, text='网格', bg=PANEL, fg=MUTED,
                 font=('Microsoft YaHei UI', 8)).pack(side='left', padx=(self._px(5), self._px(3)))
        for label, variable in (('行', self.camera_rows_var), ('列', self.camera_columns_var)):
            tk.Label(self.camera_grid_toolbar, text=label, bg=PANEL, fg=INK,
                     font=('Microsoft YaHei UI', 8)).pack(side='left', padx=self._px(2))
            choice = ttk.Combobox(self.camera_grid_toolbar, textvariable=variable,
                                 values=tuple(str(value) for value in range(1, 7)), width=2, state='readonly')
            choice.pack(side='left', padx=self._px(2), pady=self._px(2))
            choice.bind('<<ComboboxSelected>>', self._apply_camera_grid)
        tk.Label(self.camera_grid_toolbar, text='1–9 前9格 · 其余点选', bg=PANEL, fg=MUTED,
                 font=('Microsoft YaHei UI', 8)).pack(side='left', padx=self._px(5))
        self.camera_grid = tk.Frame(source, bg=INPUT)
        source.grid_rowconfigure(1, weight=0)
        source.grid_rowconfigure(2, weight=0)
        source.grid_rowconfigure(3, weight=1)
        self.camera_grid.grid(row=3, column=0, sticky='nsew')
        program = self.program_panel = tk.Frame(self, bg=BG)
        program.grid(in_=monitor_body, row=0, column=1, sticky='nsew', padx=(self._px(2), self._px(0)))
        program.grid_columnconfigure(0, weight=1)
        program.grid_rowconfigure(1, weight=1)
        program_title=self.program_title=tk.Frame(program,bg=PANEL)
        program_title.grid(row=0,column=0,sticky='ew')
        self.compare_button=tk.Checkbutton(program_title,text='防抖对比',variable=self.stabilization_compare_var,
            command=self.toggle_stabilization_compare,bg=PANEL,fg=INK,activebackground=PANEL,activeforeground=INK,
            selectcolor=INPUT,font=('Microsoft YaHei UI',9),state='disabled')
        self.compare_button.pack(side='right',padx=self._px(4))
        tk.Checkbutton(program_title,text='GPU 实时',variable=self.gpu_live_var,
            command=self._toggle_gpu_live,bg=PANEL,fg=ACCENT,activebackground=PANEL,
            activeforeground=INK,selectcolor=INPUT,font=('Microsoft YaHei UI',9)).pack(side='right')
        tk.Label(program_title, textvariable=self.program_var, bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10), anchor='w', padx=self._px(10), pady=self._px(5)).pack(side='left',fill='x',expand=True)
        self.program_surface = tk.Frame(program, bg='#0e0f10', width=self._px(280), height=self._px(180))
        self.program_surface.grid(row=1, column=0, sticky='nsew')
        self.program_views = [tk.Frame(self.program_surface, bg='#0e0f10') for _ in range(2)]
        for view in self.program_views:
            view.place(x=0, y=0, relwidth=1, relheight=1)
        self._program_front_window = self.program_views[0].winfo_id()
        self.program_views[0].lift()
        self.comparison_views=[tk.Frame(self.program_surface,bg='#0e0f10') for _ in range(2)]
        self._comparison_front_window=self.comparison_views[0].winfo_id()
        self.comparison_original_label=tk.Label(self.program_surface,text='原片 · 完整视野',bg=PANEL,fg=INK,
            font=('Microsoft YaHei UI',9),anchor='center')
        self.comparison_stabilized_label=tk.Label(self.program_surface,text='防抖后 · 含裁切',bg=PANEL,fg=ACCENT,
            font=('Microsoft YaHei UI',9),anchor='center')
        self._build_stack_preview()
        self.audio_surface = tk.Frame(program, bg=BG, width=1, height=1)
        self.audio_surface.place(x=0, y=0, width=1, height=1)
        self.camera_frames = {}
        self.camera_surfaces = {}
        self.camera_buttons = {}
        self._show_empty_media()

        transport = tk.Frame(self.monitors, bg=PANEL)
        transport.grid(row=1, column=0, sticky='ew', pady=(self._px(1), self._px(0)))
        self._button(transport, '监看', self.start_monitors)
        self._button(transport, '◀|', lambda: self.step_frame(-1))
        self._button(transport, '▶ / Ⅱ', self.toggle)
        self._button(transport, '|▶', lambda: self.step_frame(1))
        self._button(transport, '回放', self.replay)
        tk.Checkbutton(transport, text='记录', variable=self.record_var, bg=PANEL, fg=PINK,
                       activebackground=PANEL, activeforeground=PINK, selectcolor=INPUT,
                       font=('Microsoft YaHei UI', 9), borderwidth=0).pack(side='left', padx=(self._px(8), self._px(4)))
        self._label(transport, variable=self.time_var).pack(side='right', padx=self._px(9))

        timeline_section = tk.Frame(self, bg=BG, height=self._px(230))
        self.timeline_section = timeline_section
        timeline_section.grid_columnconfigure(0, weight=1)
        timeline_section.grid_rowconfigure(1, weight=1)
        self.workspace_split.add(timeline_section, weight=2)
        timeline_bar = self.timeline_bar = tk.Frame(timeline_section, bg=PANEL)
        timeline_bar.grid(row=0, column=0, sticky='ew', pady=(self._px(1), self._px(0)))
        tk.Label(timeline_bar, text='时间线', bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10)).pack(side='left', padx=self._px(10))
        self._entry(timeline_bar, self.seek_var, 13).pack(side='left', padx=(self._px(5), self._px(2)), pady=self._px(5), ipady=self._px(2))
        self._button(timeline_bar, '定位', self.seek_text)
        self._button(timeline_bar, '−', lambda: self.change_zoom(0.5))
        self._button(timeline_bar, '适配', self.fit_timeline)
        self._button(timeline_bar, '＋', lambda: self.change_zoom(2))
        self._build_video_stack_control(timeline_bar)
        tk.Label(timeline_bar, text='素材可拖入 · 中键横移 · Ctrl滚轮缩放', bg=PANEL, fg=MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(side='right', padx=self._px(10))
        self.timeline = tk.Canvas(timeline_section, bg='#222427', height=self._px(200), highlightthickness=0)
        self.timeline.grid(row=1, column=0, sticky='nsew')
        self.timeline.bind('<Configure>', lambda e: self.draw_timeline())
        self.timeline.bind('<ButtonPress-1>', self.timeline_press)
        self.timeline.bind('<B1-Motion>', self.timeline_motion)
        self.timeline.bind('<ButtonRelease-1>', self.timeline_release)
        self.timeline.bind('<Button-3>', self.timeline_context)
        self.timeline.configure(takefocus=True)
        self.timeline.bind('<Control-MouseWheel>', lambda e: self.change_zoom(2 if e.delta > 0 else 0.5))
        self.timeline.bind('<Shift-MouseWheel>', self.pan_timeline)
        self.scroll = ttk.Scrollbar(timeline_section, orient='horizontal', command=self._scroll_timeline)
        self.scroll.grid(row=2, column=0, sticky='ew')
        self.timeline.configure(xscrollcommand=self.scroll.set)
        self.timeline_vscroll = ttk.Scrollbar(timeline_section, orient='vertical', command=self.timeline.yview)
        self.timeline_vscroll.grid(row=1, column=1, sticky='ns')
        self.timeline.configure(yscrollcommand=self.timeline_vscroll.set)
        self.timeline_vscroll.grid_remove()
        self._label(self, variable=self.status).grid(row=3, column=0, sticky='ew', padx=self._px(8), pady=(self._px(4), self._px(3)))
        self._build_inspector()
        self.color_workspace = ColorWorkspace(self, self)
        self._build_export_monitor()
        self.bind('<Configure>', self._initial_layout, add='+')
        self.after_idle(self._initial_layout)

    def _show_empty_media(self):
        empty = tk.Frame(self.camera_grid, bg=INPUT)
        self._empty_camera_widget=empty
        empty.place(relx=.5, rely=.5, anchor='center')
        self.empty_label = tk.Label(empty, text='添加机位', bg=INPUT, fg=INK,
                                     font=('Microsoft YaHei UI', 12))
        self.empty_label.pack(pady=(self._px(0), self._px(8)))
        ttk.Button(empty, text='＋ 导入相机素材', command=self.add_camera,
                   style='Compact.TButton').pack()
        tk.Label(empty, text='可多选视频，每个文件一个机位', bg=INPUT, fg=MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(pady=(self._px(8), self._px(0)))

    def _initial_layout(self, event=None):
        if self._layout_initialized or not self.winfo_ismapped() or self.winfo_width() < 600:
            return
        self._layout_initialized = True
        self.update_idletasks()
        try:
            self.body.sashpos(0, min(self._media_width, max(self._px(180), round(self.body.winfo_width() * .28))))
            available = self.workspace_split.winfo_height()
            self.workspace_split.sashpos(0, max(self._px(160), round(available * .59)))
        except tk.TclError:
            pass

    def toggle_media_pool(self):
        if self._media_visible:
            self.body.forget(self.sidebar)
        else:
            self.body.insert(0, self.sidebar, weight=0)
            self.update_idletasks()
            self.body.sashpos(0, min(self._media_width, max(self._px(180), round(self.body.winfo_width() * .28))))
        self._media_visible = not self._media_visible

    def _inspector_panel(self, name):
        outer = tk.Frame(self, bg=PANEL)
        self.inspector_tabs.add(outer, text=name)
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0, width=self._px(278))
        scrollbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = tk.Frame(canvas, bg=PANEL)
        window = canvas.create_window(0, 0, window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        return outer, inner

    def _section(self, parent, title):
        tk.Label(parent, text=title, bg=PANEL, fg=INK, anchor='w',
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(fill='x', padx=self._px(12), pady=(self._px(16), self._px(7)))

    def _setting_row(self, parent, title, variable, width=14):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=self._px(12), pady=self._px(5))
        tk.Label(row, text=title, bg=PANEL, fg=MUTED, anchor='w',
                 font=('Microsoft YaHei UI', 9)).pack(side='left')
        field = self._entry(row, variable, width)
        field.pack(side='right', ipady=self._px(5))
        return field

    def _build_inspector(self):
        self.inspector = tk.Frame(self.body, bg=PANEL, width=self._inspector_width)
        self.inspector.pack_propagate(False)
        title = tk.Frame(self.inspector, bg=PANEL)
        title.pack(fill='x')
        tk.Label(title, text='检查器', bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10)).pack(side='left', padx=self._px(12), pady=self._px(7))
        ttk.Button(title, text='关闭', command=self.hide_inspector,
                   style='Compact.TButton').pack(side='right', padx=self._px(4))
        self.inspector_tabs = ttk.Notebook(self.inspector, style='Inspector.Multicam.TNotebook')
        self.inspector_tabs.pack(fill='both', expand=True)
        self.program_inspector_tab = tk.Frame(self, bg=PANEL)
        self.inspector_tabs.add(self.program_inspector_tab, text='片段')
        self.library_tab = tk.Frame(self, bg=PANEL)
        self.inspector_tabs.add(self.library_tab, text='特效库')
        self.clip_tab, clip = self._inspector_panel('同步')
        self.delivery_tab = tk.Frame(self, bg=PANEL)
        self.inspector_tabs.add(self.delivery_tab, text='交付')
        self._section(clip, '同步精调')
        tk.Label(clip,text='对齐参考（固定该轨道）',bg=PANEL,fg=MUTED,font=('Microsoft YaHei UI',9)).pack(
            anchor='w',padx=self._px(12))
        reference_combo=ttk.Combobox(clip,textvariable=self.sync_reference_var,state='readonly')
        reference_combo.pack(fill='x',padx=self._px(12),pady=self._px(4))
        reference_combo.bind('<<ComboboxSelected>>',self._sync_reference_changed)
        self._sync_reference_combos.append(reference_combo)
        ttk.Combobox(clip, textvariable=self.sync_mode_var, state='readonly',
                     values=['中段波形（推荐）', '完整波形（手动选择）']).pack(
                         fill='x', padx=self._px(12), pady=self._px(5))
        tk.Label(clip, text='先在媒体池中选择一个片段', bg=PANEL, fg=MUTED,
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', padx=self._px(12))
        self._setting_row(clip, '主时间起点', self.offset_var)
        line = tk.Frame(clip, bg=PANEL)
        line.pack(fill='x', padx=self._px(8), pady=self._px(3))
        self._button(line, '− 1帧', lambda: self.nudge(-1))
        self._button(line, '＋ 1帧', lambda: self.nudge(1))
        self._button(line, '应用', self.apply_offset)
        ttk.Button(clip, text='同步选中片段', command=lambda: self.align(True),
                   style='Compact.TButton').pack(fill='x', padx=self._px(12), pady=self._px(5))
        self._section(clip, '主音频')
        ttk.Button(clip, text='选择连续主录音…', command=self.choose_master,
                   style='Compact.TButton').pack(fill='x', padx=self._px(12), pady=self._px(4))
        ttk.Button(clip, text='选中片段作为主音频', command=self.master_from_clip,
                   style='Compact.TButton').pack(fill='x', padx=self._px(12), pady=self._px(4))
        self._section(clip, '预览代理')
        tk.Checkbutton(clip, text='优先使用代理', variable=self.proxy_var,
                       command=self._configure_player, bg=PANEL, fg=INK, selectcolor=INPUT,
                       activebackground=PANEL, activeforeground=INK,
                       font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=self._px(8))
        ttk.Button(clip, text='指定选中片段代理…', command=self.choose_proxy,
                   style='Compact.TButton').pack(fill='x', padx=self._px(12), pady=self._px(5))
        tk.Label(clip, text='预览使用代理，导出使用原片', bg=PANEL, fg=MUTED,
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', padx=self._px(12), pady=(self._px(2), self._px(14)))

        build_delivery_panel(self, self.delivery_tab)
        self.creative_tab = build_creative_panel(self, self.inspector_tabs)
        self.inspector_tabs.tab(self.creative_tab, text='创作')
        for position, page in enumerate((self.program_inspector_tab, self.creative_tab,
                                         self.library_tab, self.clip_tab, self.delivery_tab)):
            self.inspector_tabs.insert(position, page)
        self.clip_inspector = ClipInspector(self.program_inspector_tab, self)
        self.clip_inspector.pack(fill='both', expand=True)
        self.inspector_tabs.bind('<<NotebookTabChanged>>', self._inspector_tab_changed, add='+')
        self.library_tab.bind('<Map>', self._ensure_effects_library, add='+')

    def _inspector_tab_changed(self, _event=None):
        if self.inspector_tabs.select() == str(self.library_tab) and vars(self).get('effects_browser') is None:
            self.show_effects_library()

    def show_inspector(self, section=None):
        if self._inspector_visible and section is None:
            self.hide_inspector()
            return
        if vars(self).get('dock_manager') is None:
            self._show_inspector_host()
        if section in {'color', 'transition', 'subtitle'}:
            if section == 'color' and self.selected_program_range is not None and not self._color_mode:
                self.set_grade_target('selection')
            self._select_workspace_page(self.inspector_tabs, self.creative_tab)
            self.creative_panel.select(section)
        elif section == 'library':
            self._select_workspace_page(self.inspector_tabs, self.library_tab)
        else:
            self._select_workspace_page(self.inspector_tabs,
                self.delivery_tab if section == 'delivery' else self.clip_tab
                if section == 'clip' else self.program_inspector_tab)
            if vars(self).get('clip_inspector'): self.clip_inspector.refresh()
        self.inspector_button.configure(text='检查器 ●' if self._inspector_visible else '检查器')

    def hide_inspector(self):
        if self._inspector_visible:
            self.body.forget(self.inspector)
            self._inspector_visible = False
        self.inspector_button.configure(text='检查器')

    def show_delivery(self):
        self.exit_color_workspace()
        self.show_inspector('delivery')
        self._ensure_delivery_height()

    def show_creative(self, section='color'):
        self.show_inspector(section)
        if section == 'color':
            self.enter_color_workspace()
        else:
            self.exit_color_workspace()
            self._ensure_delivery_height()

    def show_inspector_tool(self, section):
        self.exit_color_workspace()
        if section == 'transition':
            self.show_inspector('edit')
            self.clip_inspector.scroll_to('transition')
        else:
            self.show_inspector(section)

    def show_effects_library(self):
        from effects_browser import show_effects_browser
        browser = show_effects_browser(self)
        self._tag_transport_widgets(browser)
        return browser

    def _ensure_effects_library(self, _event=None):
        if not self.alive or vars(self).get('effects_browser') is not None: return
        from effects_browser import ensure_effects_browser
        browser = ensure_effects_browser(self)
        self._tag_transport_widgets(browser)

    def _program_span_at(self, time):
        spans = program_spans(self.project)
        if not spans: return None
        frame = math.floor(float(time) * self.project.fps + .5)
        if frame < 0: return spans[0]
        return next((span for span in spans
            if math.floor(span.start*self.project.fps+.5) <= frame < math.floor(span.end*self.project.fps+.5)), spans[-1])

    def select_program_range(self, start, end, seek=True):
        if not self._available() or self.project.duration <= 0: return
        fps = self.project.fps
        last = math.floor(self.project.duration * fps + .5)
        first = max(0, min(last-1, math.floor(float(start)*fps+.5)))
        stop = max(first+1, min(last, math.floor(float(end)*fps+.5)))
        if self._color_mode:
            self._cancel_grade_drafts()
            self.grade_target, self._grade_target_chosen = 'selection', True
        self.selected_program_range = (first/fps, stop/fps)
        self.pause()
        selected = next((i for i, cut in enumerate(self.project.cuts)
                         if math.floor(cut.time*fps+.5) == first), None)
        self.selected_cut = selected
        if selected is not None and self.cut_tree.exists(str(selected)):
            self.cut_tree.selection_set(str(selected)); self._select_cut()
        elif self.cut_tree.selection():
            self.cut_tree.selection_remove(*self.cut_tree.selection())
        if self._color_mode and self.grade_target == 'selection':
            self.creative_panel.grade_scope_var.set('所选片段')
        self.creative_panel_refresh()
        self.draw_timeline()
        if vars(self).get('color_workspace'):
            self.color_workspace.refresh()
        if seek: self.seek(first/fps)
        if self._color_mode: self._audition_grade_target()
        if vars(self).get('clip_inspector'):
            self.clip_inspector.refresh()
            if not self._color_mode and (not self._inspector_visible or self.inspector_tabs.select() == str(self.clip_tab)):
                self.show_inspector('edit')
        self.status.set(f'已选片段 {format_time(first/fps)} — {format_time(stop/fps)} · C切开 · 数字键切换机位')

    def get_selected_grade(self):
        selected = self.selected_program_range
        position = selected[0] if selected else self.current
        return copy.deepcopy(grade_at(self.project, position) or ColorGrade())

    def get_active_grade(self):
        if self.grade_target == 'selection':
            return self.get_selected_grade()
        if self.grade_target is None:
            return copy.deepcopy(self.project.global_grade)
        return copy.deepcopy(self.project.camera_grades.get(self.grade_target, ColorGrade()))

    def _cancel_grade_drafts(self, publishing=False):
        workspace = vars(self).get('color_workspace')
        if workspace is not None:
            workspace.cancel_drafts(clear_preview=False)
        panel = vars(self).get('creative_panel')
        cancel = getattr(panel, 'cancel_grade_preview', None)
        if callable(cancel): cancel(clear_preview=False)
        if publishing:
            if self._grade_preview_after is not None:
                self.after_cancel(self._grade_preview_after)
                self._grade_preview_after = None
            self._grade_preview_pending = None
            self._grade_preview_active = False
        else:
            self.clear_grade_preview()

    def set_grade_target(self, target):
        if not self._available(): return False
        if target not in (None, 'selection') and (type(target) is not int or target not in self.project.cameras):
            return False
        if target == 'selection' and self.selected_program_range is None:
            span = self._program_span_at(self.current)
            if span is None: return False
            self.selected_program_range = (span.start, span.end)
        self._cancel_grade_drafts()
        self.grade_target, self._grade_target_chosen = target, True
        label = '所选片段' if target == 'selection' else '全片' if target is None else f'机位 {target}'
        self.creative_panel.grade_scope_var.set(label)
        self.creative_panel._load_grade()
        self.color_workspace.refresh()
        if self._color_mode:
            self._audition_grade_target()
        self.status.set(f'调色目标：{label} · 拖动色轮实时显示 · 松开保存')
        return True

    def _audition_grade_target(self):
        if self.player:
            if self.project.video_stack_enabled:
                self.player.submit('audition', camera=None)
                return
            camera = (self.project.camera_at(self.selected_program_range[0])
                      if self.grade_target == 'selection' and self.selected_program_range else
                      self.grade_target if isinstance(self.grade_target, int) else None)
            self.player.submit('audition', camera=camera)
            if camera is not None:
                self._highlight(camera)
                label = '所选片段' if self.grade_target == 'selection' else '整机位'
                self.program_var.set(f'{self._label_camera(camera)} · {label}调色实时监看')

    def _grade_preview_target(self):
        if self.grade_target == 'selection':
            return ('selection', *self.selected_program_range) if self.selected_program_range else None
        if self.grade_target is None:
            return ('global',)
        return ('camera', self.grade_target)

    def preview_active_grade(self, grade):
        if not self._available() or not self.project.clips: return False
        target = self._grade_preview_target()
        if target is None: return False
        try: validate_grade(grade, check_lut=False)
        except ValueError: return False
        self._grade_preview_pending = (target, copy.deepcopy(grade))
        if self._grade_preview_after is None:
            self._grade_preview_after = self.after(33, self._flush_grade_preview)
        return True

    def _flush_grade_preview(self):
        if self._grade_preview_after is not None:
            self.after_cancel(self._grade_preview_after)
        self._grade_preview_after = None
        pending, self._grade_preview_pending = self._grade_preview_pending, None
        if pending is None or not self.alive or not self._available(): return
        if self.player:
            self.player.submit('grade_preview', target=pending[0], grade=pending[1])
            self._grade_preview_active = True

    def clear_grade_preview(self):
        if self._grade_preview_after is not None:
            self.after_cancel(self._grade_preview_after)
            self._grade_preview_after = None
        self._grade_preview_pending = None
        if self._grade_preview_active and self.player:
            self.player.submit('grade_preview', grade=None)
        self._grade_preview_active = False

    def apply_active_grade(self, grade):
        if not self._available(): return False
        if self.grade_target == 'selection':
            return self.apply_selected_grade(grade)
        if self.grade_target is not None and self.grade_target not in self.project.cameras:
            return False
        try:
            validate_grade(grade)
            if grade == self.get_active_grade():
                self.clear_grade_preview()
                return True
            candidate = copy.deepcopy(self.project)
            if self.grade_target is None: candidate.global_grade = copy.deepcopy(grade)
            else: candidate.camera_grades[self.grade_target] = copy.deepcopy(grade)
            candidate.validate()
        except Exception as exc:
            messagebox.showerror('机位调色', str(exc), parent=self); return False
        # A configure publishes the saved grade and clears its preview in one
        # worker action, so release does not flash the old colour in between.
        if self._grade_preview_after is not None:
            self.after_cancel(self._grade_preview_after)
            self._grade_preview_after = None
        self._grade_preview_pending = None
        self._grade_preview_active = False
        self._remember(); self.project = candidate; self._refresh()
        label = '全片' if self.grade_target is None else f'机位 {self.grade_target}'
        self.status.set(f'{label} 调色已保存 · 其他机位参数保持独立')
        return True

    def apply_selected_grade(self, grade):
        if not self._available(): return False
        if self.selected_program_range is None:
            span = self._program_span_at(self.current)
            if span is None:
                self.status.set('请先导入视频并选择调色片段。'); return False
            self.selected_program_range = (span.start, span.end)
        try:
            validate_grade(grade)
            candidate = copy.deepcopy(self.project)
            set_grade_on_range(candidate, *self.selected_program_range, grade)
            candidate.validate()
        except Exception as exc:
            messagebox.showerror('片段调色', str(exc), parent=self); return False
        self._remember(); self.project = candidate
        self._refresh()
        self.status.set('所选片段调色已应用，监看实时更新。')
        return True

    def enter_color_workspace(self):
        if not self._available(): return
        if self.selected_program_range is None:
            span = self._program_span_at(self.current)
            if span is not None:
                self.selected_program_range = (span.start, span.end)
        if not self._color_mode:
            self._timeline_saved_sash = (self.workspace_split.sashpos(0)
                if len(self.workspace_split.panes()) > 1 else None)
            self._color_mode = True
            manager = vars(self).get('dock_manager')
            if manager is not None:
                manager.show('grading')
            else:
                self.workspace_split.add(self.color_workspace, weight=3)
            self._color_replaced_timeline = (str(self.color_workspace) in self.workspace_split.panes()
                and str(self.timeline_section) in self.workspace_split.panes())
            if self._color_replaced_timeline:
                self.workspace_split.forget(self.timeline_section)
        self.grade_target = 'selection' if self.selected_program_range else None
        self.set_grade_target(self.grade_target)
        self.creative_panel_refresh()
        self.color_workspace.refresh()
        self.update_idletasks()
        height = self.workspace_split.winfo_height()
        color_page = self.color_workspace
        chrome_height = max(0, color_page.winfo_height() - color_page.controls_canvas.winfo_height())
        requested_height = max(self._px(350),
            chrome_height + color_page.controls.winfo_reqheight() + self._px(4))
        sash_height = max(0, height - self.body.winfo_height() - color_page.winfo_height())
        # Fit the numeric rows as well as the wheel canvases at the active DPI.
        # Short windows retain a useful viewer and scroll the colour controls.
        if str(self.color_workspace) in self.workspace_split.panes() and len(self.workspace_split.panes()) > 1:
            self.workspace_split.sashpos(0, max(self._px(130), height-requested_height-sash_height))
        if not self.player and self.project.clips and self.app.mpv_path:
            self.start_monitors()
        self._audition_grade_target()
        self.status.set('调色页 · 默认只调所选Cut片段 · 自动识别机位 · 拖动色轮实时显示 · C切开 · ←/→逐帧')

    def exit_color_workspace(self):
        if not self._color_mode: return
        self._cancel_grade_drafts()
        if self.player: self.player.submit('audition', camera=None)
        self._color_mode = False
        manager = vars(self).get('dock_manager')
        if manager is not None:
            manager.hide('grading')
        elif str(self.color_workspace) in self.workspace_split.panes():
            self.workspace_split.forget(self.color_workspace)
        if vars(self).get('_color_replaced_timeline', True) and not self.timeline_section.winfo_manager():
            self.workspace_split.add(self.timeline_section, weight=2)
        self.update_idletasks()
        if self._timeline_saved_sash is not None and len(self.workspace_split.panes()) > 1:
            self.workspace_split.sashpos(0, self._timeline_saved_sash)
        self.draw_timeline()

    def split_program_at_playhead(self):
        if not self._available(): return False
        span = self._program_span_at(self.current)
        if span is None: return False
        try:
            candidate = copy.deepcopy(self.project)
            changed = split_at(candidate, self.current)
            if not changed:
                self.status.set('播放头已在片段边界；请逐帧移动后再按 C。'); return False
            candidate.validate()
        except Exception as exc:
            messagebox.showerror('切开片段', str(exc), parent=self); return False
        self._remember(); self.project = candidate
        cut = math.floor(self.current*self.project.fps+.5)/self.project.fps
        self.selected_program_range = (cut, span.end)
        if self._color_mode: self.grade_target = 'selection'
        self._refresh()
        self.status.set(f'已在 {format_time(cut)} 切开；右侧片段已选中。')
        return True

    def _switch_selected_camera(self, camera):
        try:
            candidate = copy.deepcopy(self.project)
            set_camera_on_range(candidate, *self.selected_program_range, camera)
            candidate.validate()
        except Exception as exc:
            messagebox.showerror('切换片段机位', str(exc), parent=self); return
        self._remember(); self.project = candidate; self._refresh()
        self._set_program_label(self.project.camera_at(self.current))
        self.status.set(f'所选片段已切换到机位 {camera}。')

    def add_transition_at_selected_end(self, kind='fade'):
        if not self._available() or self.selected_program_range is None: return
        start, end = self.selected_program_range
        next_span = next((span for span in program_spans(self.project)
                          if math.floor(span.start*self.project.fps+.5) == math.floor(end*self.project.fps+.5)), None)
        if next_span is None:
            self.status.set('此片段已到成片结尾，没有可连接的后续片段。'); return
        duration = min(.5, 2*(end-start), 2*next_span.duration)
        try:
            candidate = copy.deepcopy(self.project)
            split_at(candidate, end)
            candidate.transitions = [item for item in candidate.transitions
                if math.floor(item.time*candidate.fps+.5) != math.floor(end*candidate.fps+.5)]
            candidate.transitions.append(Transition(end, kind, duration))
            candidate.transitions.sort(key=lambda item: item.time)
            candidate.validate()
            plan_creative_segments(candidate, candidate.segments())
        except Exception as exc:
            messagebox.showerror('片段末尾转场', str(exc), parent=self); return
        self._remember(); self.project = candidate; self._refresh()
        self.status.set(f'已在片段末尾 {format_time(end)} 添加转场。')

    def _ensure_delivery_height(self):
        tabs = {str(self.delivery_tab), str(vars(self).get('creative_tab', self.delivery_tab))}
        if (not self._inspector_visible or self.inspector_tabs.select() not in tabs
                or len(self.workspace_split.panes()) < 2):
            return
        self.update_idletasks()
        available = self.workspace_split.winfo_height()
        # Opening delivery or revealing its status uses a little more of the
        # vertical split, while keeping the timeline available below it.
        target = min(self._px(360), max(self._px(180), available - self._px(100)))
        if self.workspace_split.sashpos(0) < target:
            self.workspace_split.sashpos(0, target)

    def _build_export_monitor(self):
        self.export_monitor = tk.Frame(self, bg=PANEL, padx=self._px(10), pady=self._px(6))
        self.export_monitor.grid(row=2, column=0, sticky='ew', pady=(self._px(2), 0))
        self.export_monitor.columnconfigure(0, weight=1)
        tk.Label(self.export_monitor, textvariable=self.export_phase_var, bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 10, 'bold'), anchor='w').grid(row=0, column=0, sticky='ew')
        self.export_percent_label = tk.Label(self.export_monitor, text='0.0%', bg=PANEL, fg=INK,
                                            font=('Cascadia Mono', 10))
        self.export_percent_label.grid(row=0, column=1, sticky='e')
        self.export_speed_label = tk.Label(self.export_monitor, textvariable=self.export_speed_var,
            bg=PANEL, fg=ACCENT, font=('Microsoft YaHei UI', 10), anchor='w')
        self.export_speed_label.grid(row=1, column=0, sticky='ew', pady=self._px(3))
        timing = tk.Frame(self.export_monitor, bg=PANEL)
        timing.grid(row=1, column=1, sticky='e')
        for label, variable in (('已用 ', self.export_elapsed_var), ('  剩余 ', self.export_eta_var)):
            tk.Label(timing, text=label, bg=PANEL, fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='left')
            tk.Label(timing, textvariable=variable, bg=PANEL, fg=INK,
                     font=('Cascadia Mono', 9)).pack(side='left')
        style = ttk.Style(self)
        style.configure('Delivery.Horizontal.TProgressbar', thickness=self._px(7),
                        background=ACCENT, troughcolor=INPUT)
        self.export_progress_bar = ttk.Progressbar(self.export_monitor,
            variable=self.export_progress_var, maximum=100, style='Delivery.Horizontal.TProgressbar')
        self.export_progress_bar.grid(row=2, column=0, columnspan=2, sticky='ew')
        self.export_monitor.grid_remove()

    @staticmethod
    def _duration_text(seconds):
        try: value = float(seconds)
        except (TypeError, ValueError): return '—'
        if not math.isfinite(value) or value < 0: return '—'
        value = round(value)
        return f'{value//3600:02d}:{value%3600//60:02d}:{value%60:02d}'

    def _update_export_metrics(self, snapshot):
        path=snapshot.get('diagnostic_path')
        if path and path!=getattr(self,'_export_diagnostic_path',''):
            self._export_diagnostic_path=path
            self.app._log('多机位导出诊断日志：'+str(path))
        self._export_last_snapshot = dict(snapshot)
        first_show = not self.export_monitor.winfo_manager()
        self.export_monitor.grid()
        if first_show:
            self._ensure_delivery_height()
        stage = str(snapshot.get('stage', 'encoding'))
        phase = str(snapshot.get('phase', '正在导出'))
        total_duration = float(snapshot.get('total_seconds') or 0)
        if total_duration > 0:
            phase += f' · 成片 {self._duration_text(total_duration)}'
        percent = max(0.0, min(100.0 if stage == 'done' else 99.9,
                             float(snapshot.get('progress_percent', 0))))
        fps = max(0.0, float(snapshot.get('fps') or 0))
        speed = max(0.0, float(snapshot.get('speed') or 0))
        workers = max(0, int(snapshot.get('active_workers') or 0))
        elapsed = snapshot.get('elapsed', 0)
        eta = snapshot.get('eta')
        if stage == 'muxing':
            rate = f'FPS — · 封装 {speed:.2f}×'
        elif stage == 'done':
            duration = float(snapshot.get('total_seconds') or 0)
            elapsed_value = float(elapsed or 0)
            average = duration/elapsed_value if elapsed_value > 0 else 0
            rate = f'完成 · 全程平均 {average:.2f}×'
            eta = 0
        elif stage in {'cancelled', 'failed'}:
            rate = '导出已取消' if stage == 'cancelled' else '导出失败'
            eta = None
        else:
            rate = f'近3秒：{fps:.1f} FPS · {speed:.2f}×'
            if workers > 1: rate += f' · {workers} 路合计'
            average=snapshot.get('average_fps')
            if isinstance(average,(int,float)) and math.isfinite(average):rate+=f' · 全程 {max(0.,average):.1f} FPS'
        self.export_phase_var.set(phase)
        self.export_speed_var.set(rate)
        self.export_progress_var.set(percent)
        self.export_elapsed_var.set(self._duration_text(elapsed))
        self.export_eta_var.set(self._duration_text(eta))
        self.export_percent_label.configure(text=f'{percent:.1f}%')
        summary = f'{phase} · {percent:.1f}% · {rate} · 已用 {self.export_elapsed_var.get()} · 剩余 {self.export_eta_var.get()}'
        self.export_metrics_var.set(summary)
        self.app.progress_var.set(percent)
        self.app.progress_text_var.set(summary)
        self.status.set(phase)

    def _available(self):
        if self.busy or self.app.working:
            self.status.set('当前任务仍在运行；可以取消或等待完成。')
            return False
        return True

    def _remember(self):
        self.undo_stack.append(copy.deepcopy(self.project))
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()
        self._mark_dirty()

    def _mark_dirty(self):
        self.dirty = True
        self.project_label.configure(text=(Path(self.project_path).name if self.project_path else '未保存工程') + ' *')

    def _selected_clip(self):
        selection = self.media_tree.selection()
        return next((clip for clip in self.project.clips if selection and clip.id == selection[0]), None)

    def _refresh(self, reconfigure=True):
        self._refresh_video_stack_ui()
        self._cancel_grade_drafts(publishing=reconfigure)
        if self.project.color_reference_camera not in self.project.cameras:self.project.color_reference_camera=None
        if self.project.alignment_reference_camera not in self.project.cameras:
            self.project.alignment_reference_camera=None
        self.compare_button.configure(state='normal' if self.project.stabilizations else 'disabled')
        if not self.project.stabilizations and self.stabilization_compare_var.get():
            self.stabilization_compare_var.set(False);self.toggle_stabilization_compare()
        identifiers = {clip.id for clip in self.project.clips}
        self.project.stabilizations = [item for item in self.project.stabilizations
            if item.source_id in identifiers and item.start < self.project.duration]
        for item in self.project.stabilizations: item.end = min(item.end,self.project.duration)
        if isinstance(self.grade_target, int) and self.grade_target not in self.project.cameras:
            self.grade_target = min(self.project.cameras) if self.project.cameras else None
        if self.grade_target == 'selection' and not self.project.clips:
            self.grade_target = None
        label = '所选片段' if self.grade_target == 'selection' else '全片' if self.grade_target is None else f'机位 {self.grade_target}'
        self.creative_panel.grade_scope_var.set(label)
        fps = self.project.fps
        frame = lambda value: math.floor(value * fps + .5)
        last = frame(self.project.duration)
        self.project.edit_points = sorted({frame(value)/fps for value in self.project.edit_points
                                           if 0 < frame(value) < last})
        trimmed_grades = []
        for item in self.project.clip_grades:
            first, stop = max(0, frame(item.start)), min(last, frame(item.end))
            if stop > first:
                item.start, item.end = first/fps, stop/fps
                trimmed_grades.append(item)
        self.project.clip_grades = trimmed_grades
        boundary_times = [cut.time for cut in self.project.cuts] + self.project.edit_points
        cut_frames = {frame(value) for value in boundary_times if frame(value) > 0}
        self.project.transitions = [item for item in self.project.transitions
            if frame(item.time) in cut_frames and item.time - item.duration/2 >= 0
            and item.time + item.duration/2 <= self.project.duration
            and not any(item.time - item.duration/2 < value < item.time + item.duration/2
                        and frame(value) != frame(item.time) for value in boundary_times)]
        if last <= 0:
            self.selected_program_range = None
        elif self.selected_program_range is not None:
            first = max(0, min(last-1, frame(self.selected_program_range[0])))
            stop = max(first+1, min(last, frame(self.selected_program_range[1])))
            self.selected_program_range = first/fps, stop/fps
        if self._color_mode and self.selected_program_range is None:
            span = self._program_span_at(self.current)
            if span: self.selected_program_range = (span.start, span.end)
        selection = self.media_tree.selection()
        self.media_tree.delete(*self.media_tree.get_children())
        for camera in self.project.cameras:
            parent = f'camera_{camera}'
            self.media_tree.insert('', 'end', iid=parent, text=self._label_camera(camera), open=True)
            for clip in sorted((c for c in self.project.clips if c.camera == camera), key=lambda c: c.start):
                result = self.sync_results.get(clip.id)
                tag = 'synced' if result is not None and result.confidence >= 0.2 else 'review' if result is not None else ''
                suffix = ' · 已对齐' if tag == 'synced' else ' · 待检查' if tag == 'review' else ''
                self.media_tree.insert(parent, 'end', iid=clip.id, text=Path(clip.path).name+' · '+self._clip_camera_label(clip) + suffix,
                                       tags=(tag,) if tag else (),
                                       values=(format_signed_time(clip.start), format_time(clip.duration)))
        if selection and self.media_tree.exists(selection[0]):
            self.media_tree.selection_set(selection)
        self.cut_tree.delete(*self.cut_tree.get_children())
        self.cut_camera_combo.configure(values=tuple(str(camera) for camera in self.project.cameras))
        for i, cut in enumerate(self.project.cuts):
            self.cut_tree.insert('', 'end', iid=str(i), values=(format_time(cut.time), f'{cut.camera}'))
        self.master_var.set('主音频：' + Path(self.project.master_audio).name if self.project.master_audio else '未选择主音频（将导出静音）')
        self._build_camera_frames()
        self.draw_timeline()
        self._show_time()
        refresh_creative = vars(self).get('creative_panel_refresh')
        if refresh_creative:
            refresh_creative()
        if vars(self).get('color_workspace'):
            self.color_workspace.refresh()
        if reconfigure:
            self._configure_player()
        self._schedule_transition_previews()
        self._refresh_camera_labels()
        self._schedule_camera_metadata()
        if vars(self).get('clip_inspector'): self.clip_inspector.refresh(force=True)

    @staticmethod
    def _checked_camera_grid(rows, columns):
        values = []
        for value in (rows, columns):
            if isinstance(value, str) and value.isdecimal(): value = int(value)
            if type(value) is not int or not 1 <= value <= 6:
                raise ValueError('监看行数和列数必须为 1–6 的整数。')
            values.append(value)
        return tuple(values)

    def _apply_camera_grid(self, _event=None):
        try:
            self.set_camera_grid(self.camera_rows_var.get(), self.camera_columns_var.get())
        except (ValueError, tk.TclError) as error:
            self.camera_rows_var.set(str(self.camera_rows))
            self.camera_columns_var.set(str(self.camera_columns))
            self.status.set(str(error))

    def set_camera_grid(self, rows, columns, *, persist=True):
        rows, columns = self._checked_camera_grid(rows, columns)
        if (rows, columns) == (self.camera_rows, self.camera_columns): return
        cameras = tuple(self.project.cameras)
        visible = self._visible_camera_ids
        current = self._displayed_camera if self._displayed_camera in visible else (visible[0] if visible else None)
        self.camera_rows, self.camera_columns = rows, columns
        self.camera_rows_var.set(str(rows)); self.camera_columns_var.set(str(columns))
        self.camera_page = cameras.index(current) // (rows*columns) if current in cameras else 0
        self._build_camera_frames()
        self._refresh_camera_labels()
        self.status.set(f'监看 {rows}×{columns} · 每页最多 {rows*columns} 个机位 · 数字 1–9 选择前九格，其余鼠标点选')
        if persist and vars(self).get('dock_manager'):
            self._workspace_layout_changed()

    def _build_camera_frames(self):
        all_cameras=tuple(self.project.cameras)
        rows, cols = self.camera_rows, self.camera_columns
        capacity = rows*cols
        pages=max(1,math.ceil(len(all_cameras)/capacity))
        self.camera_page=max(0,min(self.camera_page,pages-1))
        self.camera_page_var.set(f'{self.camera_page+1}/{pages}')
        self.camera_prev.configure(state='normal' if self.camera_page>0 else 'disabled')
        self.camera_next.configure(state='normal' if self.camera_page+1<pages else 'disabled')
        if pages>1:
            self.camera_shortcut_hint.pack_forget()
            self.camera_pager.pack(side='right',padx=self._px(2))
        else:
            self.camera_pager.pack_forget()
            self.camera_shortcut_hint.pack(side='right',padx=self._px(4))
        cameras=all_cameras[self.camera_page*capacity:(self.camera_page+1)*capacity]
        self._visible_camera_ids=cameras
        layout = rows, cols, cameras
        if self._camera_grid_layout == layout and tuple(self.camera_frames) == cameras:
            return
        old_windows = {camera: surface.winfo_id() for camera, surface in self.camera_surfaces.items()}
        empty=vars(self).get('_empty_camera_widget')
        if empty is not None and empty.winfo_exists():empty.destroy()
        can_release = not self.player and self._can_start_transport_monitors(report=False)
        for camera,frame in list(self.camera_frames.items()):
            if camera in cameras: continue
            frame.grid_forget()
            if not can_release:
                self._retired_camera_frames[self.camera_surfaces[camera].winfo_id()]=frame
            else:frame.destroy()
            del self.camera_frames[camera]; del self.camera_surfaces[camera]; del self.camera_buttons[camera]
        if can_release:
            self._release_camera_frames(list(self._retired_camera_frames))
        if not cameras:
            self._show_empty_media()
            self._camera_grid_layout = layout
            if self.player and old_windows:self.player.submit('camera_windows',windows={})
            return
        for i in range(6):
            self.camera_grid.columnconfigure(i, weight=1 if i < cols else 0,
                                             uniform='angle' if i < cols else '', minsize=0)
            self.camera_grid.rowconfigure(i, weight=1 if i < rows else 0,
                                          uniform='angle' if i < rows else '', minsize=0)
        for i, camera in enumerate(cameras):
            if camera in self.camera_frames:
                self.camera_frames[camera].grid(row=i // cols, column=i % cols, sticky='nsew', padx=1, pady=1)
                continue
            frame = tk.Frame(self.camera_grid, bg='#414449', padx=1, pady=1)
            frame.pack_propagate(False)
            frame.grid(row=i // cols, column=i % cols, sticky='nsew', padx=1, pady=1)
            surface = tk.Frame(frame, bg='#0e0f10', width=1, height=1)
            surface.pack(fill='both', expand=True)
            surface.bind('<Button-1>', lambda event, c=camera: self.switch(c) if self._color_mode or self.project.video_stack_enabled else None)
            surface.bind('<Button-1>', self._focus_preview, add='+')
            button = tk.Button(frame, text=self._camera_button_label(camera), bg=PANEL, fg=INK,
                               activebackground='#3a4047', activeforeground=INK,
                               relief='flat', borderwidth=0, highlightthickness=0,
                               font=('Microsoft YaHei UI', 9), pady=3,
                               command=lambda c=camera: self.switch(c))
            button.pack(fill='x')
            self.camera_frames[camera] = frame
            self.camera_surfaces[camera] = surface
            self.camera_buttons[camera] = button
            self._tag_transport_widgets(frame)
        self.camera_frames = {camera: self.camera_frames[camera] for camera in cameras}
        self.camera_surfaces = {camera: self.camera_surfaces[camera] for camera in cameras}
        self.camera_buttons = {camera: self.camera_buttons[camera] for camera in cameras}
        dense = rows > 3 or cols > 3
        for camera, button in self.camera_buttons.items():
            button.configure(text=self._camera_button_label(camera), font=('Microsoft YaHei UI', 8 if dense else 9),
                             pady=1 if dense else 3)
        self._camera_grid_layout = layout
        self._highlight(self._displayed_camera if self._displayed_camera in all_cameras else self.project.camera_at(self.current))
        windows = {camera: surface.winfo_id() for camera, surface in self.camera_surfaces.items()}
        if self.player and windows != old_windows:
            self.update_idletasks()
            self.player.submit('camera_windows',windows=windows)

    def _release_camera_frames(self,windows):
        for window in windows:
            frame=self._retired_camera_frames.pop(window,None)
            if frame is not None and frame.winfo_exists():frame.destroy()

    def _change_camera_page(self,delta):
        if not self._available():return
        pages=max(1,math.ceil(len(self.project.cameras)/(self.camera_rows*self.camera_columns)))
        page=max(0,min(self.camera_page+delta,pages-1))
        if page==self.camera_page:return
        self.camera_page=page;self._build_camera_frames();self._refresh_camera_labels()
        self.status.set(f'监看第 {page+1}/{pages} 页 · 数字1–9选择本页前九格，其余鼠标点选')

    def add_camera(self):
        if not self._available(): return
        paths=filedialog.askopenfilenames(parent=self,title='批量导入机位（每个视频一个机位）',filetypes=VIDEO_TYPES)
        if not paths:return
        ffprobe=self.app.caps.ffprobe_path or find_binary('ffprobe',Path(__file__).parent)
        if not ffprobe:
            messagebox.showerror('导入机位','未找到 ffprobe.exe。',parent=self);return
        self.busy=True;self.status.set(f'正在读取 {len(paths)} 个视频机位…')
        self.app.cancel_event.clear();self.app._set_working(True)
        self.app._begin_shared_progress('批量导入机位 0%')
        existing=tuple(self.project.cameras)
        cancel=self.app.cancel_event
        def worker():
            try:
                assignments=batch_import(ffprobe,paths,existing,cancel_event=cancel,
                    progress=lambda percent,text:self.jobs.put(('batch_import_progress',(percent,text))))
                self.jobs.put(('batch_import_ready',assignments))
            except Exception as error:self.jobs.put(('batch_import_error',(str(error),cancel.is_set())))
        threading.Thread(target=worker,name='batch-camera-import',daemon=False).start()

    def append_clips(self):
        if not self._available(): return
        clip = self._selected_clip()
        selection = self.media_tree.selection()
        camera = clip.camera if clip else (int(selection[0].split('_')[1]) if selection and selection[0].startswith('camera_') else None)
        if camera is None:
            messagebox.showinfo('多机位', '先在素材列表中选择一个机位。', parent=self)
            return
        paths = filedialog.askopenfilenames(parent=self, title=f'追加机位 {camera} 的片段', filetypes=VIDEO_TYPES)
        if paths: self._import(paths, camera)

    def _import(self, paths, camera):
        self._camera_metadata_attempted={item for item in self._camera_metadata_attempted if item[0] not in paths}
        ffprobe = self.app.caps.ffprobe_path or find_binary('ffprobe', Path(__file__).parent)
        if not ffprobe:
            messagebox.showerror('多机位', '未找到 ffprobe.exe。', parent=self); return
        self.busy = True
        self.status.set('正在读取相机素材…')
        def worker():
            try:
                infos = [probe_media(ffprobe, path) for path in paths]
                if any(not info.width or not info.height or not info.duration for info in infos):
                    raise ValueError('所选文件需要包含可读取的视频画面与时长。')
                self.jobs.put(('import', (camera, infos)))
            except Exception as exc:
                self.jobs.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def remove_clip(self):
        if not self._available(): return
        clip = self._selected_clip()
        if not clip: return
        self._remember()
        self.project.clips.remove(clip)
        if hasattr(self.project, 'library_overlays'):
            self.project.library_overlays = [item for item in self.project.library_overlays if item.source_id != clip.id]
        self.project.cuts = [c for c in self.project.cuts if c.camera in self.project.cameras]
        self._refresh()

    def _select_clip(self, event=None):
        clip = self._selected_clip()
        if clip: self.offset_var.set(format_signed_time(clip.start))

    def _jump_clip(self, event=None):
        clip = self._selected_clip()
        if clip: self.seek(max(0, clip.start))

    def apply_offset(self):
        if not self._available(): return
        clip = self._selected_clip()
        if not clip: return
        try: value = parse_signed_timecode(self.offset_var.get())
        except ValueError as exc:
            messagebox.showerror('时间码', str(exc), parent=self); return
        value = round(value*self.project.fps)/self.project.fps
        if not self._can_move_timeline_clip(clip, value): return
        self._remember();self._move_clip_start(clip, value)
        self._refresh()

    def _move_clip_start(self,clip,value):
        if not self._can_move_timeline_clip(clip, value): return False
        delta=value-clip.start
        for item in self.project.stabilizations:
            if item.source_id==clip.id:
                item.start=max(0.,item.start+delta);item.end+=delta
        self.project.stabilizations=[item for item in self.project.stabilizations if item.end>item.start]
        clip.start=value
        return True

    def nudge(self, frames):
        clip = self._selected_clip()
        if clip:
            self.offset_var.set(format_signed_time(clip.start + frames / self.project.fps))
            self.apply_offset()

    def choose_proxy(self):
        return self.link_existing_proxy()

    def choose_master(self):
        if not self._available(): return
        path = filedialog.askopenfilename(parent=self, title='选择从主时间 00:00 开始的连续主录音',
                                         filetypes=[('音视频', '*.wav *.flac *.mp3 *.m4a *.aac *.aif *.aiff *.mov *.mp4 *.mkv'), ('所有文件', '*.*')])
        if path: self._load_master(path)

    def master_from_clip(self):
        if not self._available(): return
        clip = self._selected_clip()
        if clip: self._load_master(clip.path)

    def _load_master(self, path):
        ffprobe = self.app.caps.ffprobe_path or find_binary('ffprobe', Path(__file__).parent)
        self.busy = True
        self.status.set('正在读取主音频…')
        def worker():
            try:
                result = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'a:0',
                    '-show_entries', 'stream=index', '-of', 'json', path],
                    capture_output=True, text=True, encoding='utf-8', timeout=30,
                    creationflags=CREATE_NO_WINDOW)
                if result.returncode or not json.loads(result.stdout).get('streams'):
                    raise ValueError('这个文件不包含可读取的音轨。')
                duration = probe_media_duration(ffprobe, path)
                self.jobs.put(('master', (path, duration)))
            except Exception as exc: self.jobs.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def align(self, selected=False):
        if not self._available(): return
        ffmpeg = self.app.caps.ffmpeg_path
        clip = self._selected_clip()
        clips = [clip] if selected and clip else list(self.project.clips)
        reference=self.project.alignment_reference_camera
        reference_clips=[copy.deepcopy(item) for item in self.project.clips if item.camera==reference] if reference is not None else []
        if reference is not None:clips=[item for item in clips if item.camera!=reference]
        if not clips or not ffmpeg or (reference is None and not self.project.master_audio):
            messagebox.showinfo('同步', '请选择待同步素材，以及主音频或一个参考机位；参考机位自身保持固定。', parent=self); return
        if any(not self._can_move_timeline_clip(item) for item in clips): return
        self.stop_monitors()
        self.busy = True
        self.sync_running = True
        self.app.cancel_event.clear()
        self.app._set_working(True)
        self.app._begin_shared_progress('同步并对齐 0.0%')
        self._alignment_reference_label='主音频' if reference is None else self._label_camera(reference)
        self.status.set('正在截取中段波形并匹配'+self._alignment_reference_label+'；完成后直接对齐到时间线。底部可取消。')
        paths = [c.path for c in clips]
        ids = [c.id for c in clips]
        master = self.project.master_audio
        mode = 'full' if self.sync_mode_var.get().startswith('完整') else 'fast'
        began = time.monotonic()
        def worker():
            try:
                progress=lambda percent,phase:self.jobs.put(('sync_progress',(percent,phase)))
                if reference is None:
                    results = align_audio_segments(ffmpeg, paths, master, max_offset_seconds=0,
                        mode=mode, cancel_event=self.app.cancel_event,continue_on_error=True,progress=progress)
                else:
                    results=align_to_camera(ffmpeg,copy.deepcopy(clips),reference_clips,
                        ffprobe_path=self.app.caps.ffprobe_path,mode=mode,cancel_event=self.app.cancel_event,progress=progress)
                if self.app.cancel_event.is_set(): raise ExportCancelled('同步已取消')
                self.jobs.put(('aligned', (list(zip(ids, results)), time.monotonic() - began)))
            except ExportCancelled:
                self.jobs.put(('align_cancelled', None))
            except Exception as exc: self.jobs.put(('align_error', str(exc)))
        threading.Thread(target=worker, daemon=False).start()

    def _finish_alignment(self, pairs, elapsed=0.0):
        self.busy = False
        self.sync_running = False
        self.app._set_working(False)
        if self.app.cancel_event.is_set():
            message = '已取消同步，保留原时间线'
            self.status.set(message)
            self.app.progress_text_var.set(message)
            return
        for clip_id, result in pairs:
            clip = next((c for c in self.project.clips if c.id == clip_id), None)
            if clip is not None and math.isfinite(result.offset_seconds) and result.confidence >= 0.2:
                target = round(-result.offset_seconds*self.project.fps)/self.project.fps
                if not self._can_move_timeline_clip(clip, target):
                    self.app.progress_text_var.set(self.status.get())
                    return
        self._remember()
        details = []
        placed = []
        review = []
        for clip_id, result in pairs:
            clip = next((c for c in self.project.clips if c.id == clip_id), None)
            if clip is None: continue
            self.sync_results[clip_id] = result
            if math.isfinite(result.offset_seconds) and result.confidence >= 0.2:
                self._move_clip_start(clip,round(-result.offset_seconds*self.project.fps)/self.project.fps)
                placed.append(clip)
            else:
                review.append(clip)
            details.append(f'{Path(clip.path).name}: {result.confidence:.3f}, '
                           f'{format_signed_time(-result.offset_seconds)} · {result.engine}')
        self.app._log('多机位同步并对齐：\n'+'\n'.join(details))
        self._refresh()
        self.fit_timeline()
        if not self._media_visible: self.toggle_media_pool()
        self._select_workspace_page(self.tools, self.media_tab)
        target = placed[0] if placed else review[0] if review else None
        if target:
            self.media_tree.selection_set(target.id)
            self.media_tree.see(target.id)
            self.seek(max(0.0, target.start))
        self.app.progress_var.set(100)
        reference_label=getattr(self,'_alignment_reference_label','主音频')
        message = f'已将 {len(placed)} 段机位画面直接对齐到{reference_label}时间线 · {elapsed:.1f} 秒'
        if review: message += f' · {len(review)} 段待检查（保留原位置）'
        self.status.set(message)
        self.app.progress_text_var.set(message)

    def _select_cut(self, event=None):
        selection = self.cut_tree.selection()
        if selection:
            self.selected_cut = int(selection[0])
            cut = self.project.cuts[self.selected_cut]
            self.cut_time_var.set(format_time(cut.time)); self.cut_camera_var.set(str(cut.camera))

    def _jump_cut(self, event=None):
        self._select_cut()
        if self.selected_cut is not None and self.selected_cut < len(self.project.cuts):
            self.seek(self.project.cuts[self.selected_cut].time)

    def edit_cut(self):
        if not self._available(): return
        try:
            time = parse_timecode(self.cut_time_var.get())
            camera = int(self.cut_camera_var.get())
            if camera not in self.project.cameras: raise ValueError('这个机位还没有素材。')
            if not 0 <= time < self.project.duration: raise ValueError('切点须位于工程范围内。')
        except ValueError as exc:
            messagebox.showerror('切点', str(exc), parent=self); return
        self._remember()
        if self.selected_cut is not None and self.selected_cut < len(self.project.cuts):
            del self.project.cuts[self.selected_cut]
        self.project.set_cut(time, camera)
        self.selected_cut = None
        self._refresh()

    def delete_cut(self):
        if not self._available(): return
        selection = self.cut_tree.selection()
        if selection:
            self._remember(); del self.project.cuts[int(selection[0])]
            self.selected_cut = None; self._refresh()

    def switch(self, camera):
        if not self._available() or camera not in self.project.cameras: return
        if self.project.video_stack_enabled:
            self._displayed_camera = camera
            self.set_grade_target(camera)
            self._highlight(camera)
            self._set_program_label(camera)
            self.status.set(f'已选图层 V{camera}；可调整整机位调色，合成顺序及原节目切点保留。')
            return
        if self.selected_program_range is not None:
            self._switch_selected_camera(camera)
            return
        if self.record_var.get():
            self._remember(); self.project.set_cut(self.current, camera)
            self._refresh(False)
            if self.player:
                self.player.submit('configure', project=copy.deepcopy(self.project), use_proxies=self.proxy_var.get())
                self.player.submit('audition', camera=None)
        elif self.player:
            self.player.submit('audition', camera=camera)
        self._displayed_camera = camera
        self.program_var.set('成片监看 · '+self._label_camera(camera) + ('' if self.record_var.get() else '（试听）'))
        self._highlight(camera)

    def start_monitors(self):
        if not self._available() or not self.project.clips: return
        if not self._can_start_transport_monitors(): return
        if not self.app.mpv_path:
            messagebox.showerror('监看', '请将 mpv.exe 放入 bin 文件夹。', parent=self); return
        if not self.player:
            self.update_idletasks()
            self._show_program_view(self.program_views[0].winfo_id())
            self.player = MulticamPlayer(self.app.mpv_path,
                {c: surface.winfo_id() for c, surface in self.camera_surfaces.items()},
                self.program_views[0].winfo_id(), self.audio_surface.winfo_id(), self.player_queue,
                program_standby_window=self.program_views[1].winfo_id(),
                gpu_live=self.gpu_live_var.get(),gpu_output_window=self.program_surface.winfo_id(),
                comparison_window=self.comparison_views[0].winfo_id(),comparison_standby_window=self.comparison_views[1].winfo_id())
        self._configure_player()
        self.player.submit('seek', time=self.current)
        self.status.set('正在打开同步监看；多路原片较大时请使用代理。')

    def _show_program_view(self, window):
        if self._stack_preview_active:
            self._program_front_window = window
            return True
        if self._gpu_live_active:return False
        view = next((view for view in self.program_views if view.winfo_id() == window), None)
        if view is None: return False
        view.lift()
        self._program_front_window = window
        return True

    def _show_comparison_view(self,window):
        if self._stack_preview_active:
            self._comparison_front_window = window
            return True
        view=next((view for view in self.comparison_views if view.winfo_id()==window),None)
        if view is None:return False
        view.lift();self._comparison_front_window=window
        return True

    def _set_comparison_layout(self,enabled):
        self._comparison_layout_enabled=bool(enabled)
        self.stabilization_compare_var.set(bool(enabled))
        header=self._px(23)
        if enabled:
            for view in self.program_views:view.place(relx=.5,x=0,y=header,relwidth=.5,relheight=1,height=-header)
            for view in self.comparison_views:view.place(relx=0,x=0,y=header,relwidth=.5,relheight=1,height=-header)
            self.comparison_original_label.place(relx=0,y=0,relwidth=.5,height=header)
            self.comparison_stabilized_label.place(relx=.5,y=0,relwidth=.5,height=header)
            self._show_comparison_view(self._comparison_front_window)
        else:
            for view in self.comparison_views:view.place_forget()
            self.comparison_original_label.place_forget();self.comparison_stabilized_label.place_forget()
            for view in self.program_views:
                if self._gpu_live_active:view.place_forget()
                else:view.place(relx=0,x=0,y=0,relwidth=1,relheight=1,height=0)
        self._show_program_view(self._program_front_window)
        self._raise_stack_preview()
        self.update_idletasks()

    def toggle_stabilization_compare(self):
        enabled=self.stabilization_compare_var.get()
        if not self._available():
            self.stabilization_compare_var.set(self._comparison_layout_enabled);return
        if enabled and not self.project.stabilizations:
            self.stabilization_compare_var.set(False);return
        self._set_comparison_layout(enabled)
        self._comparison_requested=enabled
        if enabled and not self.player:self.start_monitors()
        if self.player:self.player.submit('stabilization_compare',enabled=enabled)

    def _sync_comparison_state(self,payload):
        if 'comparison_enabled' not in payload:return
        enabled=bool(payload['comparison_enabled'])
        if self._comparison_requested is not None:
            if enabled!=self._comparison_requested:return
            self._comparison_requested=None
        if enabled!=self._comparison_layout_enabled:self._set_comparison_layout(enabled)
        self.comparison_stabilized_label.configure(text='防抖后 · 含裁切' if payload.get('stabilization_active') else '当前段未防抖')

    def _configure_player(self):
        if self.player:
            self.player.submit('configure', project=copy.deepcopy(self.project), use_proxies=self.proxy_var.get(),
                               gpu_live=self.gpu_live_var.get())
            self._publish_transition_previews()
            if self.stabilization_compare_var.get():self.player.submit('stabilization_compare',enabled=True)
            if self._color_mode:
                self._audition_grade_target()
            else:
                self.player.submit('audition',camera=None)

    def _toggle_gpu_live(self):
        self._gpu_live_failed=False
        self._configure_player()
        self._schedule_transition_previews()

    def _set_gpu_live_state(self,payload):
        enabled=bool(payload.get('enabled'))
        self._gpu_live_active=enabled
        if enabled:
            self._gpu_live_failed=False
            for view in self.program_views:view.place_forget()
            self.status.set('GPU 实时监看 · 双路纹理合成 · 转场后机位连续播放')
        else:
            self._set_comparison_layout(self._comparison_layout_enabled)
            if payload.get('reason'):
                self._gpu_live_failed=True
                self.status.set(str(payload['reason']))
                self._schedule_transition_previews()

    def stop_monitors(self):
        self._set_stack_preview_mode(False)
        self._reset_transport_controls()
        self.clear_grade_preview()
        if self.player:
            self._track_stopped_player(self.player)
            self.player.close()
            self.player = None
            self.player_queue = queue.Queue()
        self._gpu_live_active=False
        self._set_comparison_layout(self._comparison_layout_enabled)
        self.playing = False

    def pause(self):
        if self.player: self.player.submit('pause')
        self.playing = False

    def toggle(self):
        if not self._available(): return
        self._stop_transport_hold()
        if not self.player: self.start_monitors()
        if self.player: self.player.submit('pause' if self.playing else 'play')

    def replay(self):
        if not self._available(): return
        self.record_var.set(False)
        if not self.player: self.start_monitors()
        if self.player:
            self.player.submit('audition', camera=None)
            try: start = self._read_range()[0]
            except ValueError as exc:
                messagebox.showerror('成片范围', str(exc), parent=self); return
            self.player.submit('seek', time=start)
            self.player.submit('play')

    def seek(self, time):
        return self._request_transport_seek(time, pause=True)

    def seek_text(self):
        try: self.seek(parse_timecode(self.seek_var.get()))
        except ValueError as exc: messagebox.showerror('时间码', str(exc), parent=self)

    def _show_time(self):
        self.time_var.set(f'{format_time(self.current)} / {format_time(self.project.duration)}')
        for camera,button in self.camera_buttons.items():
            label=self._camera_button_label(camera)
            if button.cget('text')!=label: button.configure(text=label)

    def _highlight(self, camera):
        for c, button in self.camera_buttons.items():
            selected = c == camera
            button.configure(bg='#42302f' if selected else PANEL,
                             fg=PINK if selected else INK)
            self.camera_frames[c].configure(bg=PINK if selected else '#414449')

    def _key(self, event):
        if not self.winfo_ismapped(): return
        if self._transport_input_widget(event.widget): return
        if event.state & 0x4:
            if event.keysym.lower() == 'z': self.undo(); return 'break'
            if event.keysym.lower() == 'y': self.redo(); return 'break'
            if event.keysym.lower() == 's': self.save(); return 'break'
            return
        if event.char in '123456789' and event.char:
            index=int(event.char)-1
            camera=self._visible_camera_ids[index] if index<len(self._visible_camera_ids) else None
            if camera is not None:self.switch(camera)
            return 'break'
        if event.keysym.lower() == 'c': self.split_program_at_playhead(); return 'break'
        if event.keysym == 'Escape':
            self._stop_transport_hold()
            if self._timeline_scrubbing:
                self._timeline_scrubbing = False
                self._timeline_drag_geometry = None
            if self._end_video_stack_drag(cancel=True): return 'break'
            if self._end_program_boundary_drag(cancel=True): return 'break'
            self.selected_program_range = None
            self.creative_panel_refresh()
            self.draw_timeline()
            if vars(self).get('color_workspace'): self.color_workspace.refresh()
            self.status.set('已取消片段选中；数字键恢复播放头机位操作。')
            return 'break'
        if event.keysym == 'space': self.toggle(); return 'break'
        if event.keysym == 'Left': return self._transport_key_press(-1)
        if event.keysym == 'Right': return self._transport_key_press(1)

    def undo(self):
        if getattr(self, '_end_video_stack_drag', lambda **_kwargs: False)(cancel=True): return
        if getattr(self, '_end_program_boundary_drag', lambda **_kwargs: False)(cancel=True): return
        if not self._available() or not self.undo_stack: return
        self._cancel_grade_drafts()
        self.redo_stack.append(copy.deepcopy(self.project)); self.project = self.undo_stack.pop()
        self.selected_program_range = None
        self.sync_results.clear()
        self._mark_dirty(); self._refresh()

    def redo(self):
        if getattr(self, '_end_video_stack_drag', lambda **_kwargs: False)(cancel=True): return
        if getattr(self, '_end_program_boundary_drag', lambda **_kwargs: False)(cancel=True): return
        if not self._available() or not self.redo_stack: return
        self._cancel_grade_drafts()
        self.undo_stack.append(copy.deepcopy(self.project)); self.project = self.redo_stack.pop()
        self.selected_program_range = None
        self.sync_results.clear()
        self._mark_dirty(); self._refresh()

    def _confirm_discard(self):
        if not self.dirty: return True
        answer = messagebox.askyesnocancel('多机位工程', '保存当前多机位工程后再继续？', parent=self)
        if answer is None: return False
        return self.save() if answer else True

    def new(self):
        if not self._available() or not self._confirm_discard(): return
        self._reset_quick_proxy()
        self.stop_monitors(); self.project = MulticamProject(); self.project_path = ''
        self.selected_program_range = None
        self.camera_page=0
        self._camera_metadata_attempted.clear()
        self.grade_target = None; self._grade_target_chosen = False
        self.sync_results.clear()
        self.dirty = False; self.undo_stack.clear(); self.redo_stack.clear(); self.current = 0
        self._loading_fields = True
        self.start_var.set(format_time(0)); self.end_var.set(''); self.fps_var.set('30')
        self._loading_fields = False
        self.project_label.configure(text='未保存工程'); self._refresh()

    def open(self):
        if not self._available() or not self._confirm_discard(): return
        path = filedialog.askopenfilename(parent=self, title='打开多机位工程', filetypes=[('多机位工程', '*.multicam.json'), ('JSON', '*.json')])
        if path: self.load_project(path)

    def load_project(self, path):
        if path:
            try: project = MulticamProject.load(path)
            except Exception as exc: messagebox.showerror('打开工程', str(exc), parent=self); return
            self._reset_quick_proxy()
            self.stop_monitors(); self.project = project; self.project_path = path; self.dirty = False
            self.selected_program_range = None
            self.camera_page=0
            self._camera_metadata_attempted.clear()
            self.grade_target = None; self._grade_target_chosen = False
            self.sync_results.clear()
            self.undo_stack.clear(); self.redo_stack.clear(); self.current = 0
            self._loading_fields = True
            self.start_var.set(format_time(project.range_start))
            self.end_var.set(format_time(project.range_end) if project.range_end is not None else '')
            self.fps_var.set(f'{project.fps:g}'); self.project_label.configure(text=Path(path).name)
            self._loading_fields = False
            missing = [c.path for c in project.clips if not Path(c.path).is_file()]
            self._refresh()
            if missing: messagebox.showwarning('素材缺失', '\n'.join(missing[:8]), parent=self)

    def _read_range(self):
        start = parse_timecode(self.start_var.get() or '0')
        end = parse_timecode(self.end_var.get()) if self.end_var.get().strip() else None
        fps = float(self.fps_var.get())
        if not math.isfinite(fps) or not 1 <= fps <= 240: raise ValueError('FPS 请输入 1–240 之间的数字。')
        if start < 0 or (end is not None and end <= start): raise ValueError('成片终点必须晚于起点。')
        return start, end, fps

    def save(self):
        if not self._available(): return False
        path = self.project_path or filedialog.asksaveasfilename(parent=self, title='保存多机位工程', defaultextension='.multicam.json', filetypes=[('多机位工程', '*.multicam.json')])
        if not path: return False
        try:
            self.project.range_start, self.project.range_end, self.project.fps = self._read_range()
            self.project.save(path)
        except Exception as exc: messagebox.showerror('保存工程', str(exc), parent=self); return False
        self.project_path = path; self.dirty = False; self.project_label.configure(text=Path(path).name)
        self.status.set('工程已保存（保留素材位置、同步时间与全部切点）。')
        return True

    def choose_output(self):
        suffix = '.' + self.delivery_container_var.get().strip().lower().lstrip('.')
        if suffix not in {'.mp4', '.mov', '.mkv'}: suffix = '.mp4'
        types = [(suffix[1:].upper(), '*'+suffix)] + [(name, '*.'+name.lower())
                 for name in ('MP4','MOV','MKV') if '.'+name.lower() != suffix]
        path = filedialog.asksaveasfilename(parent=self, title='多机位成片', defaultextension=suffix, filetypes=types)
        if path:
            self.output_var.set(path)
            chosen = Path(path).suffix.lower()
            if chosen in {'.mp4','.mov','.mkv'}: self.delivery_container_var.set(chosen[1:].upper())

    def preview_effects(self):
        if not self._available(): return
        if not self.project.clips:
            messagebox.showinfo('效果预览', '请先导入视频机位。字幕可以先导入、编辑并保存。', parent=self)
            return
        try:
            if not self.app.caps.ffmpeg_path or not self.app.caps.ffprobe_path:
                raise ValueError('请等待 FFmpeg 检测完成。')
            if not self.app.mpv_path:
                raise ValueError('未找到 mpv.exe，无法打开内嵌效果预览。')
            project = copy.deepcopy(self.project)
            project.fps = float(canonical_frame_rate(float(self.fps_var.get())))
            start = max(0.0, min(self.current - 2.0, project.duration - 5.0))
            end = min(project.duration, start + 5.0)
            start = math.floor(start * project.fps + .5) / project.fps
            end = math.floor(end * project.fps + 1e-9) / project.fps
            project.range_start, project.range_end = start, end
            project.subtitles.burn_in = True
            project.validate()
            if self._creative_preview_folder is None:
                self._creative_preview_folder = Path(tempfile.mkdtemp(prefix='okk_effect_preview_',dir=cache_dir('previews/effects')))
            self._creative_preview_sequence += 1
            output = self._creative_preview_folder / f'preview_{self._creative_preview_sequence:04d}.mp4'
            from hardware_encoders import preferred_codec
            codec = preferred_codec(self.app.caps, 'h264')
            settings = ExportSettings(codec=codec,
                preset='p2', quality=20, resolution='1080p',
                bit_depth='8-bit', audio='none', decode_mode='cuda' if codec.endswith('_nvenc') and self.app.caps.cuda_decode else 'cpu', parallel_workers=1)
        except Exception as exc:
            messagebox.showerror('效果预览', str(exc), parent=self); return
        self._comparison_requested=None
        self._set_comparison_layout(False)
        self.stop_monitors()
        self.app.cancel_event.clear(); self.app._set_working(True); self.busy = True
        self.export_monitor.grid_remove()
        self.app._begin_shared_progress('生成效果预览 0.0%')
        self.status.set('正在生成当前播放头附近的 5 秒效果预览…')

        def worker():
            try:
                render_multicam(project, output, self.app.caps.ffmpeg_path, self.app.caps.ffprobe_path,
                    settings, self.app.caps, self.app._run_editor_process,
                    progress=lambda done, total, phase: self.jobs.put(('preview_progress',
                        (min(99.9, 100 * done / max(total, .001)), phase))),
                    cancel_event=self.app.cancel_event)
                self.jobs.put(('preview_ready', (str(output), start, end)))
            except Exception as exc:
                self.jobs.put(('preview_error', (str(exc), self.app.cancel_event.is_set())))
        self._creative_preview_worker = threading.Thread(target=worker, daemon=False)
        self._creative_preview_worker.start()

    def export(self):
        if not self._available() or not self.project.clips: return
        if not self.output_var.get(): self.choose_output()
        output = self.output_var.get().strip()
        if not output: return
        try:
            project = copy.deepcopy(self.project)
            project.range_start, project.range_end, project.fps = self._read_range()
            project.validate()
            settings, suffix = make_delivery_settings(self)
            output = self.output_var.get().strip()
            if Path(output).suffix.lower() != suffix:
                output = str(Path(output).with_suffix(suffix))
                self.output_var.set(output)
            from hardware_encoders import available_codecs
            if settings.codec not in available_codecs(self.app.caps):
                raise ValueError('当前编码器不可用；请等待 GPU 检测完成或选择 CPU 兼容模式。')
            if not self.app.caps.ffmpeg_path or not self.app.caps.ffprobe_path:
                raise ValueError('未找到 FFmpeg / ffprobe。')
            if Path(output).exists() and not messagebox.askyesno('覆盖成片', f'替换已有成片？\n{output}', parent=self): return
        except Exception as exc: messagebox.showerror('多机位导出', str(exc), parent=self); return
        self.stop_monitors()
        if self.app.mpv:
            try: self.app.mpv.command(['set_property', 'pause', True])
            except Exception: pass
        self.app.cancel_event.clear(); self.app._set_working(True)
        self.busy = True
        self.delivery_export_button.configure(state='disabled')
        self._export_started_at = time.monotonic()
        self._export_last_snapshot = {}
        self.app._begin_shared_progress('多机位导出 0.0%')
        self._update_export_metrics(dict(stage='encoding', phase='准备多机位导出', progress_percent=0,
            fps=0, speed=0, elapsed=0, eta=None, active_workers=0))
        self.status.set('正在按切点导出原始画面，主音频连续写入…')
        def progress(done, total, phase):
            self.jobs.put(('progress', (min(99.9, 100*done/max(total, 0.001)), phase)))
        def worker():
            try:
                render_multicam(project, output, self.app.caps.ffmpeg_path, self.app.caps.ffprobe_path,
                                settings, self.app.caps, self.app._run_editor_process, progress=progress,
                                cancel_event=self.app.cancel_event,
                                metrics=lambda snapshot: self.jobs.put(('export_metrics', snapshot)))
                self.jobs.put(('exported', output))
            except Exception as exc:
                self.jobs.put(('export_error', (str(exc), self.app.cancel_event.is_set() or isinstance(exc, ExportCancelled))))
        threading.Thread(target=worker, daemon=False).start()

    def _scroll_timeline(self, *args):
        self.timeline.xview(*args)
        self.draw_timeline()

    def change_zoom(self, factor):
        self.zoom = min(10000, max(1, self.zoom * factor)); self.draw_timeline()
        origin = self._timeline_origin()
        self.timeline.xview_moveto(max(0, (self.current-origin)/max(1, self.project.duration-origin) - 0.3/self.zoom))
        self._queue_timeline_redraw()

    def fit_timeline(self):
        self.zoom = 1; self.timeline.xview_moveto(0); self.draw_timeline()

    def pan_timeline(self, event):
        self.timeline.xview_scroll(-3 if event.delta > 0 else 3, 'units')
        self.draw_timeline(); return 'break'

    def _timeline_scale(self):
        if self._timeline_drag_geometry is not None: return self._timeline_drag_geometry[1]
        return max(1, self.timeline.winfo_width() - self._timeline_left - self._px(16)) / max(1, self._timeline_view_duration()-self._timeline_origin()) * self.zoom

    def _timeline_view_duration(self):
        if self._timeline_drag_geometry is not None:
            return max(self.project.duration, self._timeline_drag_geometry[2])
        if self.project.duration > 0: return self.project.duration
        track = self.project.subtitles
        return max((cue.end + track.offset for cue in track.cues), default=0.0)

    def _timeline_origin(self):
        if self._timeline_drag_geometry is not None: return self._timeline_drag_geometry[0]
        return min(0.0, min((clip.start for clip in self.project.clips), default=0.0))

    def _timeline_x(self, time):
        return self._timeline_left + (time-self._timeline_origin())*self._timeline_scale()

    def draw_timeline(self):
        canvas = self.timeline
        canvas.delete('all')
        cameras = self._timeline_camera_order()
        height = max(self._px(100), canvas.winfo_height())
        left_edge = self._timeline_left
        origin = self._timeline_origin()
        pps = self._timeline_scale()
        timeline_x = lambda value: left_edge + (value-origin)*pps
        width = max(canvas.winfo_width(), left_edge + (self._timeline_view_duration()-origin) * pps + self._px(16))
        self.timeline_clip_items = {}
        self.timeline_program_items = {}
        self.timeline_subtitle_items = {}
        self.timeline_transition_items = {}
        self._timeline_track_bounds = {}
        label_font = self._timeline_label_font
        title_font = self._timeline_title_font
        ruler_font = self._timeline_ruler_font
        ruler_h = max(self._px(27), ruler_font.metrics('linespace') + self._px(9))
        program_h = max(self._px(31), title_font.metrics('linespace') + self._px(9))
        self._timeline_program_bounds = (ruler_h, ruler_h + program_h)
        audio_h = max(self._px(28), label_font.metrics('linespace') + self._px(8))
        subtitle_h = max(self._px(30), label_font.metrics('linespace') + self._px(8))
        subtitle_y = ruler_h + program_h + self._px(3)
        track_top = subtitle_y + subtitle_h + self._px(6)
        minimum_row = label_font.metrics('linespace') + self._px(6)
        available_row = (height - track_top - audio_h - self._px(8)) / max(1, len(cameras))
        row_h = max(minimum_row, min(self._px(29), available_row))
        audio_y = max(track_top + len(cameras) * row_h + self._px(3), height - audio_h - self._px(4))
        content_height = max(height, audio_y + audio_h + self._px(4))
        canvas.configure(scrollregion=(0, 0, width, content_height))
        if content_height > canvas.winfo_height() + self._px(2):
            self.timeline_vscroll.grid()
        else:
            self.timeline_vscroll.grid_remove()
            canvas.yview_moveto(0)
        duration = self._timeline_view_duration()
        canvas.create_rectangle(0, 0, width, ruler_h, fill='#292b2e', outline='')
        canvas.create_rectangle(0, ruler_h, width, ruler_h + program_h, fill='#27292c', outline='')
        canvas.create_text(self._px(8), ruler_h + program_h / 2, text='合成' if self.project.video_stack_enabled else '节目', fill=INK, anchor='w',
                           font=title_font)
        displayed_project = self.project
        if self._timeline_drag_geometry is not None:
            # Keep existing cut/grade boundaries valid until the gesture is
            # committed and _refresh trims them to a shorter programme.
            displayed_project = copy.copy(self.project)
            displayed_project.master_duration = max(self.project.master_duration, self._timeline_drag_geometry[2])
        displayed_spans = program_spans(displayed_project)
        self._timeline_boundary_times = [span.start for span in displayed_spans if span.start > 0]
        for span in displayed_spans:
            x = timeline_x(span.start)
            x2 = timeline_x(span.end)
            chosen = (self.selected_program_range is not None
                and span.start >= self.selected_program_range[0]-1e-6
                and span.end <= self.selected_program_range[1]+1e-6)
            item = canvas.create_rectangle(x, ruler_h + self._px(4), x2, ruler_h + program_h - self._px(3),
                fill=COLORS[(span.camera-1) % 9], outline=ACCENT if chosen else '#91a0ac',
                width=self._px(2) if chosen else 1, tags=('program',))
            self.timeline_program_items[item] = (span.start, span.end)
            if x2-x > self._px(24):
                stabilized = self.project.stabilization_at(span.camera,span.start)
                stabilization_label = (' · 防抖' if Path(stabilized.cache_path).is_file() else ' · 防抖待重建') if stabilized else ''
                title=f'{span.camera} · '+self._label_camera(span.camera,span.start).split(' · ',1)[-1] + (' · 调色' if grade_at(self.project, span.start) else '') + stabilization_label
                while title and title_font.measure(title)>x2-x-self._px(12): title=title[:-1]
                item = canvas.create_text(x + self._px(7), ruler_h + program_h / 2,
                    text=title,
                    fill=INK, anchor='w', font=title_font, tags=('program',))
                self.timeline_program_items[item] = (span.start, span.end)
        if self.selected_program_range:
            x, x2 = (timeline_x(value) for value in self.selected_program_range)
            canvas.create_rectangle(x, ruler_h+self._px(2), x2, ruler_h+program_h-self._px(1),
                outline=ACCENT, width=max(1,self._px(2)), tags=('selection',))
        for transition in self.project.transitions:
            x = timeline_x(transition.time - transition.duration / 2)
            x2 = timeline_x(transition.time + transition.duration / 2)
            midpoint = timeline_x(transition.time)
            if x2 - x < self._px(10): x, x2 = midpoint - self._px(5), midpoint + self._px(5)
            item = canvas.create_rectangle(x, ruler_h + self._px(2), x2,
                ruler_h + program_h - self._px(2), fill='#a18a52', outline='#e3c980', tags=('transition',))
            self.timeline_transition_items[item] = transition.time
        for boundary in self._timeline_boundary_times:
            x = timeline_x(boundary)
            self.timeline.create_line(x, ruler_h+self._px(2), x, ruler_h+program_h-self._px(2),
                fill='#c0d0dc', width=max(1, self._px(2)), tags=('program-boundary',))
        canvas.create_rectangle(0, subtitle_y, width, subtitle_y + subtitle_h, fill='#2b2831', outline='')
        canvas.create_text(self._px(8), subtitle_y + subtitle_h / 2, text='S1 字幕', fill='#c4a4e4',
                           anchor='w', font=label_font)
        track = self.project.subtitles
        for cue in track.cues:
            start, end = cue.start + track.offset, cue.end + track.offset
            if end <= 0 or start >= duration: continue
            x, x2 = timeline_x(max(0, start)), timeline_x(min(duration, end))
            item = canvas.create_rectangle(x, subtitle_y + self._px(2), max(x+self._px(2), x2),
                subtitle_y + subtitle_h - self._px(2), fill='#71578a' if track.enabled else '#414048',
                outline='#b696d1', tags=('subtitle',))
            self.timeline_subtitle_items[item] = cue.id
            if x2-x > self._px(60):
                item = canvas.create_text(x+self._px(5), subtitle_y + subtitle_h/2,
                    text=cue.text.replace('\n', ' · ')[:60], fill=INK, anchor='w', font=label_font,
                    tags=('subtitle',))
                self.timeline_subtitle_items[item] = cue.id
        camera_clips = {camera: [] for camera in cameras}
        for clip in self.project.clips: camera_clips[clip.camera].append(clip)
        for i, camera in enumerate(cameras):
            y = track_top + i * row_h
            self._timeline_track_bounds[camera] = (y, y + row_h)
            canvas.create_rectangle(0, y, width, y + row_h, fill='#25272a' if i % 2 == 0 else '#222427',
                                    outline='')
            canvas.create_text(self._px(8), y + row_h / 2, text=f'V{camera}', fill=MUTED, anchor='w',
                               font=label_font)
            for clip in camera_clips[camera]:
                x = timeline_x(clip.start)
                x2 = timeline_x(clip.start + clip.duration)
                if x2 <= x:
                    continue
                item = canvas.create_rectangle(x, y + self._px(1), x2, y + row_h - self._px(1),
                                                fill='#383a3e' if self.project.video_stack_enabled and camera in self.project.video_stack_hidden else COLORS[(camera-1) % 9], outline='#6b7f89', tags=('clip',))
                self.timeline_clip_items[item] = clip.id
                if x2-x > self._px(95):
                    title=('α · ' if clip.has_alpha else '')+self._clip_camera_label(clip)+' · '+Path(clip.path).name[:24]
                    while title and label_font.measure(title)>x2-x-self._px(12): title=title[:-1]
                    label = canvas.create_text(x + self._px(6), y + row_h / 2, text=title,
                                                fill=INK, anchor='w', font=label_font, tags=('clip',))
                    self.timeline_clip_items[label] = clip.id
            self._draw_stack_track_header(camera, y, row_h, label_font)
        for point in self.project.edit_points:
            x = timeline_x(point)
            canvas.create_line(x, track_top, x, audio_y-self._px(2), fill='#7193aa',
                               dash=(self._px(3), self._px(2)), tags=('edit-boundary',))
        canvas.create_rectangle(0, audio_y, width, audio_y + audio_h, fill='#272c2c', outline='')
        canvas.create_text(self._px(8), audio_y + audio_h / 2, text='A1 主音轨', fill=GREEN, anchor='w',
                           font=label_font)
        if self.project.master_audio and duration > 0:
            audio_end = min(duration, self.project.master_duration or duration)
            audio_start = timeline_x(0)
            x2 = timeline_x(audio_end)
            canvas.create_rectangle(audio_start, audio_y + self._px(3), x2, audio_y + audio_h - self._px(3),
                                    fill='#3d625c', outline='#5a8178')
            if x2-left_edge > self._px(125):
                canvas.create_text(audio_start + self._px(7), audio_y + audio_h / 2,
                                   text=Path(self.project.master_audio).name[:58], fill=INK, anchor='w', font=label_font)
        elif not cameras:
            canvas.create_text(left_edge + self._px(15), ruler_h + program_h / 2, text='导入机位后开始编辑',
                               fill=MUTED, anchor='w', font=title_font)
        label_width = ruler_font.measure('00:00:00.000') + self._px(35)
        step = max(1 / self.project.fps, label_width / max(pps, 0.00001))
        power = 10 ** math.floor(math.log10(step))
        step = next((m * power for m in (1, 2, 5, 10) if m * power >= step), 10 * power)
        left = canvas.canvasx(0)
        right = canvas.canvasx(canvas.winfo_width())
        tick = max(math.floor(origin/step)*step, math.floor((origin+(left-left_edge)/pps)/step)*step)
        count = 0
        while timeline_x(tick) <= right + label_width and tick <= duration and count < 500:
            x = timeline_x(tick)
            canvas.create_line(x, ruler_h - self._px(5), x, ruler_h, fill='#63676d')
            canvas.create_text(x + self._px(3), ruler_h / 2, text=format_signed_time(tick) if tick < 0 else format_time(tick), anchor='w', fill=MUTED,
                               font=ruler_font)
            tick += step
            count += 1
        self.draw_playhead()

    def draw_playhead(self):
        self.timeline.delete('head')
        x = self._timeline_x(self.current)
        top = self.timeline.canvasy(0)
        bottom = self.timeline.canvasy(self.timeline.winfo_height())
        self.timeline.create_polygon(x-self._px(5), top, x+self._px(5), top,
                                      x+self._px(5), top+self._px(7), x, top+self._px(12),
                                      x-self._px(5), top+self._px(7), fill=PINK, outline='', tags='head')
        self.timeline.create_line(x, top+self._px(10), x, bottom, fill=PINK,
                                  width=max(1, self._px(1)), tags='head')
        if self._color_mode and vars(self).get('color_workspace'):
            self.color_workspace.update_playhead()

    def timeline_press(self, event):
        if not self._available(): return
        if self._begin_video_stack_drag(event): return
        self._stop_transport_hold()
        self._timeline_scrubbing = False
        self._timeline_drag_geometry = None
        self._cancel_timeline_redraw()
        self.timeline.focus_set()
        x = self.timeline.canvasx(event.x)
        self.timeline_drag = None
        self.subtitle_drag = None
        ruler_end = getattr(self, '_timeline_program_bounds', (self._px(27),))[0]
        if self.timeline.canvasy(event.y) < ruler_end:
            self._timeline_scrubbing = True
            self._begin_preview_epoch()
            self._capture_timeline_geometry()
            self.seek(self._timeline_origin()+(x-self._timeline_left)/self._timeline_scale())
            return
        self.pause()
        ids = self.timeline.find_overlapping(x-self._px(2), self.timeline.canvasy(event.y)-self._px(2), x+self._px(2), self.timeline.canvasy(event.y)+self._px(2))
        boundary_dragging = self._begin_program_boundary_drag(event)
        if boundary_dragging == 'blocked': return
        cue_id = next((self.timeline_subtitle_items[i] for i in reversed(ids) if i in self.timeline_subtitle_items), None)
        if cue_id:
            cue = next(cue for cue in self.project.subtitles.cues if cue.id == cue_id)
            self.subtitle_drag = (cue.id, x, cue.start, cue.end, False)
            self.show_creative('subtitle')
            self.creative_panel.subtitle_tree.selection_set(cue.id)
            self.creative_panel.subtitle_tree.see(cue.id)
            self.creative_panel._load_cue()
            self.seek(cue.start + self.project.subtitles.offset)
            self._capture_timeline_geometry()
            return
        transition_time = next((self.timeline_transition_items[i] for i in reversed(ids)
                                if i in self.timeline_transition_items), None)
        if boundary_dragging and transition_time is None: return
        if transition_time is not None:
            selected = next((i for i, cut in enumerate(self.project.cuts)
                             if math.floor(cut.time*self.project.fps+.5) == math.floor(transition_time*self.project.fps+.5)), None)
            if selected is not None:
                self.cut_tree.selection_set(str(selected))
                self._select_cut()
                self.creative_panel_refresh()
            self.show_creative('transition')
            label = next((label for label, value in self.creative_panel._cut_choices.items()
                          if math.floor(value*self.project.fps+.5) == math.floor(transition_time*self.project.fps+.5)), None)
            if label:
                self.creative_panel.transition_cut_var.set(label)
                self.creative_panel._load_transition()
            self.seek(transition_time)
            return
        clip_id = next((self.timeline_clip_items[i] for i in reversed(ids) if i in self.timeline_clip_items), None)
        selected_range = next((self.timeline_program_items[i] for i in reversed(ids)
                               if i in self.timeline_program_items), None)
        clicked = self._timeline_origin()+(x-self._timeline_left)/self._timeline_scale()
        if selected_range:
            self.select_program_range(*selected_range, seek=False)
        elif clip_id:
            span = self._program_span_at(clicked)
            if span: self.select_program_range(span.start, span.end, seek=False)
        else:
            self.selected_program_range = None
            self.creative_panel_refresh()
            self.draw_timeline()
        if clip_id:
            clip = next(c for c in self.project.clips if c.id == clip_id)
            self.media_tree.selection_set(clip.id)
            self.timeline_drag = (clip.id, x, clip.start, False)
            self._capture_timeline_geometry()
        self.seek(clicked)

    def timeline_context(self, event):
        if not self._available(): return
        if self._video_stack_context(event): return
        self.timeline.focus_set(); self.pause()
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        ids = self.timeline.find_overlapping(x-1, y-1, x+1, y+1)
        selected = next((self.timeline_program_items[item] for item in reversed(ids)
                         if item in self.timeline_program_items), None)
        position = self._timeline_origin() + (x-self._timeline_left)/self._timeline_scale()
        if selected is None:
            span = self._program_span_at(position)
            selected = (span.start, span.end) if span else None
        if selected is None: return
        self.select_program_range(*selected, seek=False)
        self.seek(max(selected[0], min(position, selected[1]-1/self.project.fps)))
        menu = tk.Menu(self, tearoff=False, bg=PANEL, fg=INK, activebackground='#3c5263')
        cameras = tk.Menu(menu, tearoff=False, bg=PANEL, fg=INK)
        for camera in self.project.cameras:
            cameras.add_command(label=f'机位 {camera}', command=lambda value=camera: self.switch(value))
        menu.add_cascade(label='切换机位', menu=cameras)
        menu.add_command(label='进入调色界面', command=lambda: self.show_creative('color'))
        image_stabilization = tk.Menu(menu, tearoff=False, bg=PANEL, fg=INK)
        image_stabilization.add_command(label='自动配置…',command=lambda:open_image_stabilization_dialog(self,mode='auto'))
        image_stabilization.add_command(label='手动参数…',command=lambda:open_image_stabilization_dialog(self,mode='manual'))
        image_stabilization.add_command(label='移除所选片段防抖',command=self.remove_selected_stabilization,
                                       state='normal' if self.get_selected_stabilization() else 'disabled')
        menu.add_cascade(label='画面防抖（无需陀螺仪）',menu=image_stabilization)
        stabilization = tk.Menu(menu, tearoff=False, bg=PANEL, fg=INK)
        stabilization.add_command(label='自动配置…', command=lambda: open_stabilization_dialog(self,mode='auto'))
        stabilization.add_command(label='手动参数…', command=lambda: open_stabilization_dialog(self,mode='manual'))
        stabilization.add_command(label='移除所选片段防抖', command=self.remove_selected_stabilization,
                                  state='normal' if self.get_selected_stabilization() else 'disabled')
        menu.add_cascade(label='Gyroflow 防抖', menu=stabilization)
        transitions = tk.Menu(menu, tearoff=False, bg=PANEL, fg=INK)
        for title, kind in [('叠化','fade'), ('溶解','dissolve'), ('向左擦除','wipeleft'),
                ('向右擦除','wiperight'), ('向左滑动','slideleft'), ('向右滑动','slideright'),
                ('圆形展开','circleopen'), ('黑场','fadeblack'), ('白场','fadewhite')]:
            transitions.add_command(label=title, command=lambda value=kind: self.add_transition_at_selected_end(value))
        enabled = selected[1] < self.project.duration - 1/self.project.fps/2
        menu.add_cascade(label='在片段末尾添加转场', menu=transitions,
                         state='normal' if enabled else 'disabled')
        self.timeline_context_menu = menu
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def timeline_motion(self, event):
        if self._move_video_stack_drag(event): return
        if self._timeline_scrubbing:
            self.seek(self._timeline_origin()+(self.timeline.canvasx(event.x)-self._timeline_left)/self._timeline_scale())
            return
        if self._move_program_boundary_drag(event): return
        if self.subtitle_drag:
            cue_id, x, start, end, changed = self.subtitle_drag
            delta = self.timeline.canvasx(event.x) - x
            if abs(delta) < self._px(3) and not changed: return
            if not changed: self._remember()
            cue = next(cue for cue in self.project.subtitles.cues if cue.id == cue_id)
            new_start = max(0, round((start + delta/self._timeline_scale()) * self.project.fps) / self.project.fps)
            cue.start, cue.end = new_start, new_start + end - start
            self.subtitle_drag = (cue_id, x, start, end, True)
            self._queue_timeline_redraw()
            return
        if not self.timeline_drag: return
        clip_id, x, start, changed = self.timeline_drag
        delta = self.timeline.canvasx(event.x)-x
        if abs(delta) < 4 and not changed: return
        clip = next(c for c in self.project.clips if c.id == clip_id)
        value = round((start+delta/self._timeline_scale())*self.project.fps)/self.project.fps
        if not self._can_move_timeline_clip(clip, value): return
        if not changed: self._remember()
        self._move_clip_start(clip, value)
        self.offset_var.set(format_signed_time(clip.start))
        self.timeline_drag = (clip_id, x, start, True)
        self._queue_timeline_redraw()

    def timeline_release(self, event):
        if self._end_video_stack_drag(): return
        if self._timeline_scrubbing:
            self._finish_preview_epoch(seek=False)
            self.seek(self._timeline_origin()+(self.timeline.canvasx(event.x)-self._timeline_left)/self._timeline_scale())
            self._timeline_scrubbing = False
            self._timeline_drag_geometry = None
            return
        if self._end_program_boundary_drag(): return
        self._cancel_timeline_redraw()
        self._timeline_drag_geometry = None
        if self.subtitle_drag:
            if self.subtitle_drag[-1]: self._refresh()
            self.subtitle_drag = None
            return
        if self.timeline_drag and self.timeline_drag[3]:
            clip_id = self.timeline_drag[0]
            self._refresh()
            clip = next((clip for clip in self.project.clips if clip.id == clip_id), None)
            if clip is not None: self._reveal_timeline_clip(clip)
        self.timeline_drag = None

    def _poll(self):
        if not self.alive: return
        try:
            while True:
                kind, payload = self.player_queue.get_nowait()
                if self._handle_stack_preview_job(kind, payload): continue
                if kind == 'state':
                    if self.player is None: continue
                    if not self._accept_transport_state(payload): continue
                    self.current = float(payload['time']); self.playing = bool(payload['playing'])
                    camera = payload.get('camera')
                    if self.project.video_stack_enabled and self._displayed_camera in self.project.cameras:
                        camera = self._displayed_camera
                    self._sync_comparison_state(payload)
                    self._set_program_label(camera,payload.get('preview',False))
                    self._highlight(camera); self._show_time(); self.draw_playhead()
                elif kind == 'program_swap':
                    if self.player is not None and self._show_program_view(payload):
                        self.update_idletasks()
                        self.player.submit('program_swap_ack', window=payload)
                elif kind=='gpu_live_state':
                    if self.player is not None:self._set_gpu_live_state(payload)
                elif kind=='comparison_swap':
                    if self.player is not None and self._show_comparison_view(payload):
                        self.update_idletasks();self.player.submit('comparison_swap_ack',window=payload)
                elif kind=='camera_windows_released':self._release_camera_frames(payload)
                elif kind == 'status': self.status.set(str(payload))
                elif kind == 'error':
                    self.status.set(str(payload)); self.app._log('多机位监看：' + str(payload))
        except queue.Empty: pass
        try:
            while True:
                kind, payload = self.jobs.get_nowait()
                if self._handle_transport_job(kind, payload): continue
                if kind.startswith('quick_proxy_') and self._handle_quick_proxy_job(kind, payload): continue
                if self._handle_library_job(kind, payload): continue
                if kind == 'timeline_import_ready':
                    self._finish_timeline_import(payload); continue
                if kind == 'timeline_import_error':
                    self.busy = False; self.app._set_working(False)
                    text, cancelled = payload
                    self.status.set('已取消拖入，保留原时间线。' if cancelled else text)
                    if not cancelled: messagebox.showerror('拖入时间线', text, parent=self)
                    continue
                if kind == 'proxy_validated':
                    self.busy = False
                    project, identifier, source, path = payload
                    clip = next((clip for clip in self.project.clips if clip.id == identifier and clip.path == source), None)
                    if project is self.project and clip is not None:
                        self._remember(); clip.proxy_path = path
                        register_proxy(source, path)
                        self._configure_player()
                        self.status.set('代理检查通过，已用于预览；最终导出仍读取原片。')
                    continue
                if kind == 'proxy_validation_error':
                    self.busy = False; self.status.set(str(payload))
                    messagebox.showerror('预览代理不匹配', str(payload), parent=self)
                    continue
                if kind=='batch_import_progress':
                    percent,text=payload
                    self.status.set(text);self.app.progress_var.set(percent);self.app.progress_text_var.set(text);continue
                if kind=='batch_import_error':
                    self.busy=False;self.app._set_working(False)
                    text,cancelled=payload;self.status.set('已取消批量导入，保留原工程' if cancelled else text)
                    if not cancelled:messagebox.showerror('导入机位',text,parent=self)
                    continue
                if kind=='batch_import_ready':
                    self.busy=False;self.app._set_working(False)
                    if self.app.cancel_event.is_set():
                        self.status.set('已取消批量导入，保留原工程');continue
                    if not payload:continue
                    self._remember();first=not self.project.clips
                    for camera,info in payload:
                        self.project.clips.append(CameraClip(uuid.uuid4().hex,camera,info.path,0.,info.duration,has_alpha=bool(getattr(info,'has_alpha',False))))
                        if not self.project.master_audio and info.has_audio:
                            self.project.master_audio=info.path;self.project.master_duration=info.duration
                    if first:
                        self.project.fps=payload[0][1].fps or 30;self.fps_var.set(f'{self.project.fps:.8g}')
                    if not self.project.cuts:self.project.set_cut(0,payload[0][0])
                    self._refresh();self.fit_timeline()
                    self.status.set(f'已导入 {len(payload)} 个视频，新增 {len(payload)} 个机位；同机位分段请使用“追加”。')
                    continue
                if kind.startswith('auto_color_'):
                    self._handle_auto_color_job(kind,payload);continue
                if kind == 'camera_metadata':
                    identifier,path,metadata = payload
                    changed=False
                    for clip in (clip for clip in self.project.clips if clip.path==path):
                        changed=changed or (clip.camera_make,clip.camera_model)!=(metadata.make,metadata.model)
                        clip.camera_make,clip.camera_model,clip.camera_metadata_source=metadata.make,metadata.model,metadata.source
                    if changed: self._mark_dirty()
                    self._refresh_camera_labels();self.draw_timeline()
                    continue
                if kind.startswith('stabilization_') or kind in ('transition_ready','transition_error'):
                    self._handle_effect_job(kind,payload)
                    continue
                if kind == 'import':
                    self.busy = False; self._remember()
                    camera, infos = payload
                    start = max((c.start+c.duration for c in self.project.clips if c.camera == camera), default=0)
                    first = not self.project.clips
                    for info in infos:
                        self.project.clips.append(CameraClip(uuid.uuid4().hex, camera, info.path, start, info.duration,has_alpha=bool(getattr(info,'has_alpha',False))))
                        start += info.duration
                        if not self.project.master_audio and info.has_audio:
                            self.project.master_audio = info.path; self.project.master_duration = info.duration
                    if first:
                        self.project.fps = infos[0].fps or 30; self.fps_var.set(f'{self.project.fps:.8g}')
                    if not self.project.cuts: self.project.set_cut(0, camera)
                    self._refresh(); self.status.set(f'机位 {camera} 已导入 {len(infos)} 段；不同机位初始都从 00:00 开始，请同步。')
                elif kind == 'master':
                    self.busy = False; self._remember()
                    self.project.master_audio, self.project.master_duration = payload
                    self._refresh(); self.status.set('已选择连续主音频。可自动同步全部或填写片段在主录音中的起点。')
                elif kind == 'aligned':
                    pairs, elapsed = payload
                    self._finish_alignment(pairs, elapsed)
                elif kind == 'sync_progress':
                    percent, phase = payload
                    self.app.progress_var.set(percent)
                    self.app.progress_text_var.set(f'同步并对齐 {percent:.1f}% · {phase}')
                    self.status.set(str(phase))
                elif kind in {'align_cancelled', 'align_error'}:
                    self.busy = False; self.sync_running = False
                    self.app._set_working(False)
                    message = '已取消同步，保留原时间线' if kind == 'align_cancelled' else str(payload)
                    self.status.set(message); self.app.progress_text_var.set(message)
                    if kind == 'align_error': messagebox.showerror('同步并对齐', message, parent=self)
                elif kind == 'error':
                    self.busy = False; self.status.set(str(payload)); messagebox.showerror('多机位', str(payload), parent=self)
                elif kind == 'preview_progress':
                    percent, phase = payload
                    self.app.progress_var.set(percent)
                    self.app.progress_text_var.set(f'效果预览 {percent:.1f}% · {phase}')
                elif kind == 'preview_ready':
                    self.busy = False; self.app._set_working(False)
                    path, start, end = payload
                    self.app.progress_var.set(100)
                    self.app.progress_text_var.set('效果预览已生成')
                    self.start_monitors()
                    if self.player:
                        self.player.submit('render_preview', path=path, start=start, end=end, play=True)
                    self.status.set('正在播放当前播放头附近的效果预览；修改参数后可再次生成。')
                elif kind == 'preview_error':
                    self.busy = False; self.app._set_working(False)
                    text, cancelled = payload
                    self.app.progress_text_var.set('已取消效果预览' if cancelled else '效果预览失败')
                    self.status.set(self.app.progress_text_var.get())
                    if not cancelled: messagebox.showerror('效果预览', text, parent=self)
                elif kind == 'export_metrics':
                    self._update_export_metrics(payload)
                elif kind == 'progress' and not self._export_last_snapshot:
                    percent, phase = payload
                    self.app.progress_var.set(percent); self.app.progress_text_var.set(f'多机位 {percent:.1f}% · {phase}')
                    self.status.set(str(phase))
                elif kind == 'exported':
                    self.busy = False; self.app._set_working(False)
                    self.delivery_export_button.configure(state='normal')
                    snapshot = dict(self._export_last_snapshot)
                    if snapshot.get('stage') != 'done':
                        if self._export_started_at is not None:
                            snapshot['elapsed'] = time.monotonic() - self._export_started_at
                        snapshot.update(stage='done', phase='导出完成', progress_percent=100,
                                        eta=0, fps=0, speed=0, active_workers=0)
                    self._update_export_metrics(snapshot)
                    self.status.set('已导出：' + str(payload))
                    self.app._log('多机位导出完成：'+str(payload))
                    messagebox.showinfo('多机位导出完成', str(payload), parent=self)
                elif kind == 'export_error':
                    self.busy = False; self.app._set_working(False)
                    self.delivery_export_button.configure(state='normal')
                    text, cancelled = payload
                    snapshot = dict(self._export_last_snapshot)
                    if self._export_started_at is not None:
                        snapshot['elapsed'] = time.monotonic() - self._export_started_at
                    snapshot.update(stage='cancelled' if cancelled else 'failed',
                                    phase='已取消导出' if cancelled else '导出失败',
                                    eta=None, active_workers=0)
                    self._update_export_metrics(snapshot)
                    self.status.set('已取消导出' if cancelled else text)
                    self.app._log('多机位导出：' + text)
                    if not cancelled: messagebox.showerror('多机位导出', text, parent=self)
        except queue.Empty: pass
        self.after(80, self._poll)

    def help(self):
        messagebox.showinfo('多机位剪辑操作',
            '1. “添加机位”可一次多选视频，每个文件创建一个机位；同机位分段请用“追加”。\n'
            '   监看上方可分别选择1–6行、1–6列，最多6×6同屏36机位；超过当前网格容量时左右箭头翻页。\n'
            '   数字键1–9选择本页前九格，第10–36格请点击机位按钮；网格设置随工作区布局保存。\n'
            '2. “对齐参考”可选主音频或单个机位；参考机位固定，其他机位按其波形对齐。\n'
            '   也可手填片段起点、拖动时间线或逐帧微调；同步不会替换主音频。\n'
            '3. 点“监看”，勾选“记录”，播放时按 1–9 或点击机位按钮记录切点。\n'
            '4. 空格播放/暂停；←/→按下立即暂停并移动一帧，长按连续倒放/正放，松开保持暂停。\n'
            '   拖动时间线标尺可连续定位；定位播放头后按 C 切开，切点会保留。\n'
            '   鼠标移到节目轨两段之间，出现左右箭头后按住拖动可移动切点；Esc 取消，Ctrl+Z 撤销。\n'
            '   切点按帧对齐，两侧至少保留一帧；已有转场会随切点移动并限制可移动范围。\n'
            '   相邻片段已应用库叠加特效时，请先移除特效，调整切点后重新应用。\n'
            '5. 左键选中“节目”轨道片段后，按 1–9 仅切换所选片段的机位；Esc 取消选中。\n'
            '6. 右键片段可切换机位、进入调色，或在片段末尾与后续片段之间添加转场。\n'
            '   “Gyroflow 防抖”提供自动配置/手动参数，检测并处理所选片段，完成后预览与导出共用。\n'
            '   无陀螺仪或识别失败时，可用“画面防抖”：通过图像特征分析运动，提供平滑、锁定机位和自动裁边。\n'
            '   转场会在后台自动准备，看到“转场预览已就绪”后普通回放即可查看。\n'
            '7. “调色”页默认只调Cut后选中的片段，并自动识别该片段机位；拖动色轮实时显示。\n'
            '   “曲线”内页支持主亮度/R/G/B，点击加点、拖动调节、右键删除内部点。\n'
            '   “自动 / 人像”提供自然微亮和人物美白预设，默认曝光偏好+0.25EV，分析后一次应用可撤销。\n'
            '   人物预设会纠正偏黄；“色彩参考”可选择其他机位，让白衣与肤色向它靠近。\n'
            '   防抖完成后，右上“防抖对比”可同屏查看原片完整视野与防抖裁切后的画面。\n'
            '   只有显式切换到“整机位”或“全片”目标时才会调其他片段。\n'
            '   拖动选区两端可修改范围；该页同样支持 C 和逐帧。点“返回剪辑”恢复时间线。\n'
            '8. Ctrl+Z 撤销、Ctrl+Y 重做。保存工程后，点“导出…”设置并导出。\n\n'
            '媒体库素材可直接拖入时间线；中键拖动或 Shift+滚轮横移，Ctrl+滚轮缩放。\n'
            '透明视频拖入时间线会自动作为顶层叠加；“图层叠加”开启时，上方轨道位于画面上层。\n'
            '拖左侧V轨道标签可上下调序，右键轨道可置顶、下移或隐藏；关闭叠加仍保留原节目切点。\n'
            '叠加模式中，点击监看机位或按数字键只选择该图层的调色目标，不改变合成顺序或写入切点。\n'
            '主音频固定连续播放；所选机位停机处输出黑场。代理仅用于监看，导出始终使用原片。\n'
            '8K 多路监看可点“一键1080p代理”，生成期间仍可剪辑；完成后自动挂载，导出始终读取原片。\n'
            '“释放缓存”回收播放器解码缓存与显存，再加载当前帧并保持暂停，不删除素材或代理。\n'
            '工作面板可拖动标签/标题浮出窗口，拖到其他面板中心合并标签，四边分割停靠。\n'
            '“布局 → 面板 → 浮动整个标签组”可一起移出整组；拖动组标题整体停靠，拖动单个标签只移动该页。\n'
            '浮动窗口关闭会停靠回工作区；“布局”菜单可显示、浮动、隐藏面板或恢复默认布局。\n'
            '布局独立保存，不改变工程剪辑；精细调色面板也可独立浮动。\n'
            '自动同步调整恒定偏移；长录制时钟漂移需分段检查并单独微调。', parent=self)

    def request_close(self):
        return self._confirm_discard()

    def close(self):
        self.alive = False
        self._end_video_stack_drag(cancel=True)
        self._close_workspace_docking()
        self._close_transport_controls()
        self._close_quick_proxy()
        self._library_apply_token = getattr(self, '_library_apply_token', 0) + 1
        self._close_timeline_interaction()
        self._close_auto_color()
        self._camera_metadata_cancel.set()
        self._close_effects()
        if self.sync_running or self.busy: self.app.cancel_event.set()
        player, worker, folder = self.player, self._creative_preview_worker, self._creative_preview_folder
        self.stop_monitors()
        if folder is not None:
            def cleanup():
                if worker is not None and worker.is_alive(): worker.join(timeout=20)
                if worker is not None and worker.is_alive(): return
                if player is not None: player.wait_closed(timeout=10)
                resolved = folder.resolve()
                if (resolved.parent == cache_dir('previews/effects').resolve()
                        and resolved.name.startswith('okk_effect_preview_')):
                    shutil.rmtree(resolved, ignore_errors=True)
            threading.Thread(target=cleanup, daemon=False).start()
