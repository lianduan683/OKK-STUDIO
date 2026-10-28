"""Native effect library for the existing multi-camera programme pipeline.

Presets are ordinary range grades/transitions, so saving, undo, GPU monitoring
and original-media export all use the existing creative model.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import math
import hashlib
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

from creative_core import ColorGrade, validate_grade
from editor_core import format_time
from exporter_core import CREATE_NO_WINDOW, find_binary
from effects_library import (load_overlay_assets, load_transition_masks, asset_path,
                             transition_mask_path, default_app_dir)
from effects_catalog import (GROUPS, catalog_categories, category_sort_key, overlay_metadata,
                             mask_metadata, query_words, searchable_text)
from cache_paths import cache_dir

PANEL, INPUT, INK, MUTED, ACCENT = '#292b2e', '#18191b', '#dedfe3', '#969ba3', '#58a6d6'
FONT = ('Microsoft YaHei UI', 9)


@dataclass(frozen=True, slots=True)
class EffectPreset:
    key: str
    name: str
    category: str
    description: str
    kind: str = 'grade'
    changes: tuple[tuple[str, float], ...] = ()
    action: str = ''
    swatch: str = '#58a6d6'
    asset: object = None


PRESETS = (
    EffectPreset('clean', '清透自然', '调色风格', '轻微增加对比与饱和度，压低高光，保留自然层次。', changes=(('contrast',1.08),('saturation',1.04),('highlights',-.06))),
    EffectPreset('warm', '暖阳柔光', '调色风格', '温暖色温和柔和对比，适合日光人像与生活片段。', changes=(('temperature',.16),('contrast',1.04),('saturation',1.08),('highlights',-.08)), swatch='#cba479'),
    EffectPreset('cool', '冷调夜色', '调色风格', '降低色温和饱和度，增加轻微对比。', changes=(('temperature',-.16),('contrast',1.10),('saturation',.92)), swatch='#7199b8'),
    EffectPreset('cinema', '柔和电影', '调色风格', '轻微冷调、柔和暗部和收敛的颜色。', changes=(('temperature',-.05),('contrast',1.12),('saturation',.9),('shadows',.04),('highlights',-.1)), swatch='#759c99'),
    EffectPreset('concert', '舞台鲜明', '调色风格', '增强舞台色彩与层次，同时压低高光。', changes=(('contrast',1.13),('saturation',1.2),('highlights',-.12)), swatch='#a27ca7'),
    EffectPreset('soft', '低反差柔和', '调色风格', '降低反差并轻提中间调，适合柔和画面。', changes=(('contrast',.92),('gamma',1.04),('saturation',.94),('highlights',-.1)), swatch='#a3aaa5'),
    EffectPreset('mono', '经典黑白', '调色风格', '去除色彩并增加适度对比；强度可以保留部分原色。', changes=(('saturation',0.),('contrast',1.12)), swatch='#a3a3a3'),
    EffectPreset('portrait', '人像清润', '调色风格', '使用现有肤色处理减轻偏黄并适度提亮肤色。', changes=(('skin_lighten',.22),('skin_deyellow',.2),('contrast',1.03)), swatch='#c0a095'),
    EffectPreset('reset', '还原片段调色', '调色风格', '清除所选节目片段的调色与 LUT；机位调色和全片调色仍保留。', kind='reset', swatch='#77828a'),
    *(EffectPreset('transition_'+kind, name, '转场', description, kind='transition', action=kind, swatch='#738f91')
      for name, kind, description in (
          ('柔和叠化','fade','在所选片段末尾与下一片段平滑叠化。'),
          ('颗粒溶解','dissolve','在所选片段末尾以颗粒状溶解过渡。'),
          ('向左擦除','wipeleft','从右向左擦入下一片段。'),
          ('向右擦除','wiperight','从左向右擦入下一片段。'),
          ('向左滑动','slideleft','让画面向左滑入下一片段。'),
          ('向右滑动','slideright','让画面向右滑入下一片段。'),
          ('圆形展开','circleopen','从画面中心展开圆形转场。'),
          ('淡入黑场','fadeblack','通过黑场连接两个片段。'),
          ('淡入白场','fadewhite','通过白场连接两个片段。'))),
    EffectPreset('subtitle', '字幕与标题', '字幕工具', '打开字幕编辑器，可导入 SRT、添加文字并调整样式。', kind='tool', action='subtitle', swatch='#abb39b'),
    EffectPreset('image_stabilize', '画面防抖', '防抖工具', '为所选片段打开画面防抖设置，分析后应用并保留撤销。', kind='tool', action='image_stabilize', swatch='#8ba6ac'),
    EffectPreset('gyro_stabilize', '陀螺仪防抖', '防抖工具', '为所选片段打开 Gyroflow 设置，检测运动数据与镜头配置。', kind='tool', action='gyro_stabilize', swatch='#8ba6ac'),
)
PRESET_BY_KEY = {item.key:item for item in PRESETS}
CATEGORIES = GROUPS


def library_presets():
    """Read metadata only; video files are opened lazily for one thumbnail."""
    items=list(PRESETS)
    for asset in load_overlay_assets():
        try: asset_path(asset)
        except ValueError: continue
        composition='透明通道叠加' if asset.kind=='alpha' else '滤色叠加'
        name, category = overlay_metadata(asset)
        items.append(EffectPreset('overlay:'+asset.id,name,category,
            f'{composition} · {asset.width}×{asset.height} · {asset.duration:.1f} 秒。应用到所选节目片段，强度控制不透明度。',
            kind='overlay',swatch='#b08d70',asset=asset))
    for mask in load_transition_masks():
        try: transition_mask_path(mask)
        except ValueError: continue
        name, category = mask_metadata(mask)
        items.append(EffectPreset('mask:'+mask.id,name,category,
            '使用灰度遮罩在所选片段末尾连接下一片段；可调整过渡时长，导出和效果预览使用同一遮罩。',
            kind='mask',swatch='#819a9a',asset=mask))
    # Keep built-in preset order and stable IDs. Sort asset families together.
    return tuple(items[:len(PRESETS)] + sorted(items[len(PRESETS):],
        key=lambda item: (category_sort_key(item.category), item.key)))


def filtered_presets(category='全部', query='', presets=PRESETS):
    words = query_words(query)
    return tuple(item for item in presets
                 if (category in ('全部',item.category) or item.category.startswith(category+' / ')) and all(word in
                    searchable_text(item) for word in words))


def preset_grade(preset: EffectPreset, original: ColorGrade, strength: float = 1.) -> ColorGrade:
    """Blend only the preset's controls; preserve independent LUTs/curves."""
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('预设强度必须在 0% 至 100% 之间。')
    if preset.kind == 'reset':
        return ColorGrade()
    if preset.kind != 'grade':
        raise ValueError('此预设不是调色风格。')
    result = copy.deepcopy(original)
    for name, target in preset.changes:
        start = getattr(original,name)
        setattr(result,name,start+(target-start)*strength)
    if strength > 0: result.enabled = True
    validate_grade(result,check_lut=False)
    return result


def _selected_range(workspace):
    if not workspace.selected_program_range:
        raise ValueError('请先在下方节目时间线选择要应用效果的片段。')
    return workspace.selected_program_range


def apply_preset(workspace, preset: EffectPreset, strength=1., duration=.5) -> bool:
    """Publish through the workspace's existing undoable editing APIs."""
    if not workspace._available(): return False
    if preset.kind in ('grade','reset'):
        _selected_range(workspace)
        grade = preset_grade(preset,workspace.get_selected_grade(),strength)
        if grade == workspace.get_selected_grade():
            workspace.clear_grade_preview()
            return True
        workspace._cancel_grade_drafts(publishing=True)
        return bool(workspace.apply_selected_grade(grade))
    if preset.kind == 'transition':
        _selected_range(workspace)
        before = copy.deepcopy(workspace.project.transitions)
        workspace.clear_grade_preview()
        workspace.add_transition_at_selected_end(preset.action)
        return before != workspace.project.transitions
    if preset.kind in ('overlay','mask'):
        _selected_range(workspace)
        callback=getattr(workspace,'apply_library_'+preset.kind,None)
        if not callable(callback):raise ValueError('此工作区尚未接入该素材的渲染模块。')
        workspace.clear_grade_preview()
        if preset.kind=='overlay':return bool(callback(preset.asset,opacity=strength))
        if not math.isfinite(duration) or duration<=0:raise ValueError('转场时长必须大于 0 秒。')
        return bool(callback(preset.asset,duration=duration))
    if preset.action == 'subtitle':
        workspace.show_creative('subtitle')
        return True
    _selected_range(workspace)
    if preset.action == 'image_stabilize':
        from image_stabilization_panel import open_image_stabilization_dialog
        open_image_stabilization_dialog(workspace,mode='auto')
        return True
    if preset.action == 'gyro_stabilize':
        from stabilization_panel import open_stabilization_dialog
        open_stabilization_dialog(workspace,mode='auto')
        return True
    raise ValueError('无法识别此效果。')


class EffectsBrowser(tk.Frame):
    def __init__(self,parent,workspace):
        super().__init__(parent,bg=PANEL)
        self.ws = workspace
        self.query = tk.StringVar(self)
        self.category = tk.StringVar(self,value='全部')
        self.subcategory = tk.StringVar(self,value='全部类型')
        self.strength = tk.DoubleVar(self,value=100.)
        self.strength_text = tk.StringVar(self,value='100%')
        self.duration = tk.StringVar(self,value='0.5')
        self.target_text = tk.StringVar(self)
        self.detail_text = tk.StringVar(self)
        self.category_text = tk.StringVar(self)
        self.feedback = tk.StringVar(self,value='先选择节目片段，再选择效果并应用。')
        self._previewing = False
        self._preview_selection = None
        self._poll_id = None
        self._items = ()
        self._catalog = library_presets()
        self._catalog_by_key = {item.key:item for item in self._catalog}
        self._thumb_requests = queue.Queue()
        self._thumb_results = queue.Queue()
        self._thumb_closed = threading.Event()
        self._thumb_worker = None
        self._thumb_serial = 0
        self._thumb_photo = None
        self._thumb_original = None
        self._thumb_message = ''
        self._selected_detail_key = None
        self._responsive_key = None
        self._filtering = False
        px = workspace._px
        self.columnconfigure(0,weight=1)
        self.rowconfigure(3,weight=2,minsize=px(128))
        self.rowconfigure(4,weight=3,minsize=px(57))
        header=tk.Frame(self,bg=PANEL)
        header.grid(row=0,column=0,sticky='ew',padx=px(10),pady=(px(10),px(5)))
        tk.Label(header,text='特效库',bg=PANEL,fg=INK,font=('Microsoft YaHei UI',12,'bold')).pack(side='left')
        ttk.Button(header,text='刷新',width=4,command=self.reload_library,style='Compact.TButton').pack(side='right')
        self.count_label=tk.Label(header,bg=PANEL,fg=MUTED,font=FONT)
        self.count_label.pack(side='right')
        filters=tk.Frame(self,bg=PANEL)
        filters.grid(row=1,column=0,sticky='ew',padx=px(10),pady=px(4))
        tk.Label(filters,text='搜索',bg=PANEL,fg=MUTED,font=FONT).pack(side='left',padx=(0,px(5)))
        self.search_entry=tk.Entry(filters,textvariable=self.query,bg=INPUT,fg=INK,insertbackground=INK,
                                  relief='flat',font=FONT)
        self.search_entry.pack(side='left',fill='x',expand=True,ipady=px(4))
        self.category_filters=tk.Frame(self,bg=PANEL)
        self.category_filters.grid(row=2,column=0,sticky='ew',padx=px(10),pady=(0,px(7)))
        self.category_filters.columnconfigure(0,weight=1,uniform='effect-filter')
        self.category_filters.columnconfigure(1,weight=1,uniform='effect-filter')
        self.categories=ttk.Combobox(self.category_filters,textvariable=self.category,
            values=self._categories(),state='readonly',width=8)
        self.categories.grid(row=0,column=0,sticky='ew',padx=(0,px(4)))
        self.subcategories=ttk.Combobox(self.category_filters,textvariable=self.subcategory,
            values=('全部类型',),state='readonly',width=10)
        self.subcategories.grid(row=0,column=1,sticky='ew')
        results=tk.Frame(self,bg=INPUT)
        results.grid(row=3,column=0,sticky='nsew',padx=px(10))
        self.list = ttk.Treeview(results,columns=('category',),show='tree headings',selectmode='browse',
                                height=5,style='Multicam.Treeview')
        self.list.heading('#0',text='预设名称');self.list.heading('category',text='分类')
        self.list.column('#0',width=px(150),minwidth=px(90));self.list.column('category',width=px(70),stretch=False)
        self.list.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(results,orient='vertical',command=self.list.yview)
        scroll.pack(side='right',fill='y');self.list.configure(yscrollcommand=scroll.set)
        self.list.bind('<<TreeviewSelect>>',self._selection_changed)
        self.list.bind('<Double-1>',lambda event:self.apply())
        detail_outer=tk.Frame(self,bg=PANEL)
        detail_outer.grid(row=4,column=0,sticky='nsew',padx=px(10),pady=(px(5),0))
        self.detail_canvas=tk.Canvas(detail_outer,bg=PANEL,highlightthickness=0,width=1,height=1,
                                     yscrollincrement=px(16))
        detail_scroll=ttk.Scrollbar(detail_outer,orient='vertical',command=self.detail_canvas.yview)
        detail_scroll.pack(side='right',fill='y');self.detail_canvas.pack(side='left',fill='both',expand=True)
        self.detail_canvas.configure(yscrollcommand=detail_scroll.set)
        self.detail_body=tk.Frame(self.detail_canvas,bg=PANEL)
        self.detail_body.columnconfigure(0,weight=1)
        detail_window=self.detail_canvas.create_window(0,0,window=self.detail_body,anchor='nw')
        self.detail_canvas.bind('<Configure>',lambda event:self.detail_canvas.itemconfigure(detail_window,width=max(1,event.width)))
        self.detail_body.bind('<Configure>',lambda event:self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox('all')))
        details=tk.Frame(self.detail_body,bg=PANEL)
        details.grid(row=0,column=0,sticky='ew')
        self.swatch=tk.Frame(details,bg=ACCENT,height=px(4));self.swatch.pack(fill='x',pady=(0,px(6)))
        self.thumbnail=tk.Canvas(details,bg=INPUT,height=px(80),highlightthickness=0)
        self.thumbnail.bind('<Configure>',lambda event:self._draw_thumbnail())
        self._wrapped(details,self.category_text,ACCENT).pack(fill='x',pady=(0,px(4)))
        self._wrapped(details,self.detail_text,INK).pack(fill='x')
        self._wrapped(details,self.target_text,MUTED).pack(fill='x',pady=(px(6),0))
        self.strength_row=tk.Frame(self.detail_body,bg=PANEL)
        self.strength_row.grid(row=1,column=0,sticky='ew',pady=px(6))
        self.strength_label=tk.Label(self.strength_row,text='强度',bg=PANEL,fg=MUTED,font=FONT)
        self.strength_label.pack(side='left')
        self.scale=ttk.Scale(self.strength_row,from_=0,to=100,variable=self.strength,command=self._strength_changed)
        self.scale.pack(side='left',fill='x',expand=True,padx=px(5))
        tk.Label(self.strength_row,textvariable=self.strength_text,bg=PANEL,fg=INK,font=FONT,width=5).pack(side='right')
        self.duration_row=tk.Frame(self.detail_body,bg=PANEL)
        tk.Label(self.duration_row,text='遮罩转场时长（秒）',bg=PANEL,fg=MUTED,font=FONT).pack(side='left')
        tk.Entry(self.duration_row,textvariable=self.duration,width=7,bg=INPUT,fg=INK,insertbackground=INK,relief='flat').pack(side='right')
        self.actions=tk.Frame(self,bg=PANEL);self.actions.grid(row=5,column=0,sticky='ew',padx=px(8),pady=(px(4),px(5)))
        self.actions.columnconfigure(0,weight=1)
        self.actions.columnconfigure(1,weight=1)
        self.preview_button=ttk.Button(self.actions,text='试看颜色',command=self.preview,style='Compact.TButton')
        self.preview_button.grid(row=0,column=0,sticky='ew',padx=px(2))
        self.apply_button=ttk.Button(self.actions,text='应用到所选片段',command=self.apply,style='Compact.TButton')
        self.apply_button.grid(row=0,column=1,sticky='ew',padx=px(2))
        self.secondary_actions=tk.Frame(self.detail_body,bg=PANEL)
        self.secondary_actions.grid(row=2,column=0,sticky='ew',pady=px(3))
        self.secondary_actions.columnconfigure(0,weight=1)
        self.secondary_actions.columnconfigure(1,weight=1)
        self.secondary_button=ttk.Button(self.secondary_actions,text='停止试看',command=self.stop_preview,style='Compact.TButton')
        self.secondary_button.grid(row=0,column=0,sticky='ew',padx=px(2))
        self.applied_preview_button=ttk.Button(self.secondary_actions,text='预览已应用效果',command=self.preview_applied,style='Compact.TButton')
        self.applied_preview_button.grid(row=0,column=1,sticky='ew',padx=px(2))
        self._wrapped(self.detail_body,self.feedback,MUTED).grid(row=3,column=0,sticky='ew',pady=(px(4),px(8)))
        self._bind_detail_wheel(self.detail_body)
        self.detail_canvas.bind('<MouseWheel>',self._detail_wheel)
        self.query.trace_add('write',self._filter)
        self.category.trace_add('write',self._category_changed)
        self.subcategory.trace_add('write',self._filter)
        self.bind('<Configure>',self._responsive_layout,add='+')
        self.bind('<Unmap>',self._unmap,add='+')
        self.bind('<Map>',lambda event:self.refresh(),add='+')
        self._category_changed();self._poll_selection()

    def _wrapped(self,parent,variable,color):
        label=tk.Label(parent,textvariable=variable,bg=PANEL,fg=color,font=FONT,anchor='w',justify='left',wraplength=self.ws._px(260))
        label.bind('<Configure>',lambda event:label.configure(wraplength=max(70,event.width)),add='+')
        return label

    def _detail_wheel(self,event):
        self.detail_canvas.yview_scroll(-1 if event.delta>0 else 1,'units')
        return 'break'

    def _bind_detail_wheel(self,widget):
        widget.bind('<MouseWheel>',self._detail_wheel,add='+')
        for child in widget.winfo_children():self._bind_detail_wheel(child)

    def selected_preset(self):
        selection=self.list.selection()
        return self._catalog_by_key.get(selection[0]) if selection else None

    def _categories(self):
        present={item.category.split(' / ',1)[0] for item in self._catalog}
        return tuple(group for group in CATEGORIES if group=='全部' or group in present)

    def _category_changed(self,*_):
        categories=catalog_categories(self._catalog,self.category.get())
        # A short leaf label fits alongside the main group in narrow inspectors.
        self._subcategory_values={'全部类型': self.category.get()}
        for category in categories:
            label=category if self.category.get()=='全部' else category.split(' / ',1)[1]
            self._subcategory_values[label]=category
        self.subcategories.configure(values=tuple(self._subcategory_values),
            state='readonly' if categories else 'disabled')
        if self.subcategory.get() not in self._subcategory_values:
            self.subcategory.set('全部类型')
        self._filter()

    def _responsive_layout(self,event):
        if event.widget is not self:return
        px=self.ws._px
        compact=event.width<px(430)
        stacked=event.width<px(295)
        key=compact,stacked
        if key==self._responsive_key:return
        self._responsive_key=key
        self.list.configure(displaycolumns=() if compact else ('category',))
        self.list.column('category',width=px(125),stretch=False)
        for first,second in ((self.preview_button,self.apply_button),
                             (self.secondary_button,self.applied_preview_button)):
            first.grid_configure(row=0,column=0,columnspan=2 if stacked else 1)
            second.grid_configure(row=1 if stacked else 0,column=0 if stacked else 1,
                columnspan=2 if stacked else 1,pady=(px(3),0) if stacked else 0)

    def reload_library(self):
        self._catalog=library_presets()
        self._catalog_by_key={item.key:item for item in self._catalog}
        self.categories.configure(values=self._categories())
        self._selected_detail_key=None
        if self.category.get() not in self._categories():self.category.set('全部')
        self._category_changed()

    def _filter(self,*_):
        if self._filtering:return
        self._filtering=True
        selected=self.list.selection()
        category=getattr(self,'_subcategory_values',{}).get(self.subcategory.get(),self.category.get())
        self._items=filtered_presets(category,self.query.get(),self._catalog)
        self.list.delete(*self.list.get_children())
        for item in self._items:self.list.insert('','end',iid=item.key,text=item.name,values=(item.category.split(' / ',1)[-1],))
        self.count_label.configure(text=f'{len(self._items)}/{len(self._catalog)} 项')
        if self._items:
            key=selected[0] if selected and any(item.key==selected[0] for item in self._items) else self._items[0].key
            self.list.selection_set(key)
            self.list.see(key)
        self._filtering=False
        self._selection_changed()

    def _selection_changed(self,event=None):
        item=self.selected_preset()
        key=item.key if item else ''
        if key==self._selected_detail_key:
            self.refresh();return
        self._selected_detail_key=key
        self.stop_preview(quiet=True)
        self.category_text.set(f'{item.category}\n{item.name}' if item else '')
        self.detail_text.set(item.description if item else '没有匹配的效果，请更换搜索词或分类。')
        self.swatch.configure(bg=item.swatch if item else PANEL)
        self.preview_button.configure(state='normal' if item and item.kind=='grade' else 'disabled')
        self.scale.configure(state='normal' if item and item.kind in ('grade','overlay') else 'disabled')
        self.scale.configure(from_=1 if item and item.kind=='overlay' else 0)
        if item and item.kind=='overlay' and self.strength.get()<1:self.strength.set(1)
        self.strength_label.configure(text='不透明度' if item and item.kind=='overlay' else '强度')
        self.duration_row.grid_remove();self.strength_row.grid()
        if item and item.kind=='mask':
            self.strength_row.grid_remove()
            self.duration_row.grid(row=1,column=0,sticky='ew',pady=self.ws._px(6))
        available=bool(item)
        if item and item.kind in ('overlay','mask'):
            available=callable(getattr(self.ws,'apply_library_'+item.kind,None))
        self.apply_button.configure(state='normal' if available else 'disabled',text=(
            '打开设置…' if item and item.kind=='tool' else '添加到片段末尾' if item and item.kind in ('transition','mask') else '应用到所选片段'))
        self.secondary_button.configure(text='移除所选叠加' if item and item.kind=='overlay' else '停止试看',
            command=self.remove_overlay if item and item.kind=='overlay' else self.stop_preview)
        self._request_thumbnail(item)
        self.detail_canvas.yview_moveto(0)
        self.refresh()

    def refresh(self):
        selected=self.ws.selected_program_range
        self.target_text.set(f'所选片段  {format_time(selected[0])} — {format_time(selected[1])}'
                             if selected else '尚未选择节目片段')
        if self._previewing and selected != self._preview_selection:self.stop_preview(quiet=True)

    def _poll_selection(self):
        self._poll_id=None
        if self.winfo_ismapped():self.refresh()
        self._collect_thumbnails()
        self._poll_id=self.after(350,self._poll_selection)

    def _request_thumbnail(self,item):
        self._thumb_serial+=1
        self.thumbnail.delete('all');self._thumb_photo=None;self._thumb_original=None
        self._thumb_message=''
        if item is None or item.kind not in ('overlay','mask'):
            self.thumbnail.pack_forget();return
        self.thumbnail.pack(fill='x',before=self.swatch,pady=(0,self.ws._px(6)))
        self._thumb_message='读取缩略图…';self._draw_thumbnail()
        while True:
            try:self._thumb_requests.get_nowait()
            except queue.Empty:break
        self._thumb_requests.put((self._thumb_serial,item))
        if self._thumb_worker is None:
            self._thumb_worker=threading.Thread(target=self._thumbnail_worker,name='effect-library-thumbnail',daemon=True)
            self._thumb_worker.start()

    def _thumbnail_worker(self):
        while not self._thumb_closed.is_set():
            request=self._thumb_requests.get()
            if request is None:return
            serial,item=request
            try:
                source=Path(asset_path(item.asset) if item.kind=='overlay' else transition_mask_path(item.asset))
                if item.kind=='mask':
                    self._thumb_results.put((serial,str(source),''));continue
                stamp=source.stat()
                key=hashlib.sha256(f'{source}|{stamp.st_size}|{stamp.st_mtime_ns}'.encode()).hexdigest()[:28]
                folder=cache_dir('effects_thumbnails');folder.mkdir(parents=True,exist_ok=True)
                output=folder/(key+'.png')
                if not output.is_file():
                    caps=getattr(getattr(self.ws,'app',None),'caps',None)
                    ffmpeg=getattr(caps,'ffmpeg_path',None) or find_binary('ffmpeg',default_app_dir())
                    if not ffmpeg:raise ValueError('未找到 FFmpeg，无法生成缩略图。')
                    pending=folder/(key+f'.{id(self):x}.pending.png')
                    command=[ffmpeg,'-hide_banner','-v','error','-y','-ss',str(min(.5,item.asset.duration/2)),
                             '-i',str(source),'-an','-frames:v','1','-vf',
                             'scale=256:144:force_original_aspect_ratio=decrease','-threads','1',str(pending)]
                    process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=CREATE_NO_WINDOW)
                    try:
                        for _ in range(75):
                            if self._thumb_closed.is_set():raise RuntimeError('缩略图任务已取消。')
                            try:
                                _,error=process.communicate(timeout=.2)
                                break
                            except subprocess.TimeoutExpired:continue
                        else:raise RuntimeError('缩略图读取超时。')
                        if process.returncode or not pending.is_file():raise RuntimeError('无法读取此素材的缩略图。')
                        pending.replace(output)
                    finally:
                        if process.poll() is None:
                            process.kill();process.communicate()
                        pending.unlink(missing_ok=True)
                self._thumb_results.put((serial,str(output),''))
            except Exception as error:self._thumb_results.put((serial,'',str(error)))

    def _collect_thumbnails(self):
        while True:
            try:serial,path,error=self._thumb_results.get_nowait()
            except queue.Empty:return
            if serial!=self._thumb_serial:continue
            try:
                if error:raise ValueError(error)
                self._thumb_original=tk.PhotoImage(master=self,file=path)
                self._thumb_message=''
            except (ValueError,tk.TclError):
                self._thumb_original=None;self._thumb_message='缩略图暂不可用'
            self._draw_thumbnail()

    def _draw_thumbnail(self):
        self.thumbnail.delete('all')
        width=max(1,self.thumbnail.winfo_width())
        height=max(1,self.ws._px(80))
        picture=self._thumb_original
        if picture is not None:
            shrink=max(1,math.ceil(picture.width()/width),math.ceil(picture.height()/height))
            self._thumb_photo=picture.subsample(shrink,shrink) if shrink>1 else picture
            self.thumbnail.create_image(width/2,height/2,image=self._thumb_photo)
        elif self._thumb_message:
            self.thumbnail.create_text(width/2,height/2,text=self._thumb_message,fill=MUTED,font=FONT,
                width=max(1,width-self.ws._px(10)))

    def _strength_changed(self,value):
        self.strength_text.set(f'{float(value):.0f}%')
        if self._previewing:self.preview()

    def preview(self):
        item=self.selected_preset()
        if not item or item.kind!='grade' or not self.ws._available():return False
        try:
            selected=_selected_range(self.ws)
            if not (self._previewing and selected==self._preview_selection and self.ws.grade_target=='selection'):
                if not self.ws.set_grade_target('selection'):return False
            grade=preset_grade(item,self.ws.get_selected_grade(),self.strength.get()/100.)
            if not self.ws.player:
                self.feedback.set('请先启动节目监看，再试看颜色；也可以应用后生成准确效果预览。')
                return False
            if not self.ws.preview_active_grade(grade):return False
            self._previewing=True;self._preview_selection=selected
            self.feedback.set(f'正在试看「{item.name}」；点击应用才会保存，停止试看可恢复。')
            return True
        except ValueError as error:self.feedback.set(str(error));return False

    def stop_preview(self,quiet=False):
        if self._previewing:self.ws.clear_grade_preview()
        self._previewing=False;self._preview_selection=None
        if not quiet:self.feedback.set('已停止试看，恢复已保存的画面。')

    def _unmap(self,event):
        if event.widget is self:self.stop_preview(quiet=True)

    def apply(self):
        item=self.selected_preset()
        if not item:return False
        try:
            duration=float(self.duration.get()) if item.kind=='mask' else .5
            changed=apply_preset(self.ws,item,self.strength.get()/100.,duration)
            self._previewing=False;self._preview_selection=None
            if changed:self.feedback.set(f'「{item.name}」'+('设置已打开。' if item.kind=='tool' else
                '已提交，处理状态见时间线下方。' if item.kind=='overlay' else '已应用；支持撤销、保存和原片导出。'))
            else:self.feedback.set(self.ws.status.get())
            self.refresh()
            return changed
        except ValueError as error:self.feedback.set(str(error));return False

    def preview_applied(self):
        self.stop_preview(quiet=True)
        if not self.ws._available():return
        item=self.selected_preset()
        selected=self.ws.selected_program_range
        if selected:
            position=selected[1] if item and item.kind in ('transition','mask') else (selected[0]+selected[1])/2
            self.ws.seek(position)
        self.ws.preview_effects()

    def remove_overlay(self):
        if not self.ws._available():return False
        try:
            _selected_range(self.ws)
            callback=getattr(self.ws,'remove_library_overlay',None)
            if not callable(callback):raise ValueError('此工作区尚未接入叠加素材的移除操作。')
            result=callback()
            self.feedback.set(self.ws.status.get())
            return bool(result)
        except ValueError as error:self.feedback.set(str(error));return False

    def destroy(self):
        self.stop_preview(quiet=True)
        self._thumb_closed.set();self._thumb_requests.put(None)
        if self._poll_id is not None:
            self.after_cancel(self._poll_id);self._poll_id=None
        super().destroy()


def ensure_effects_browser(workspace):
    """Build the library when its panel is first revealed in any dock host."""
    browser=vars(workspace).get('effects_browser')
    if browser is None or not browser.winfo_exists():
        browser=EffectsBrowser(workspace.library_tab,workspace)
        browser.pack(fill='both',expand=True)
        workspace.effects_browser=browser
    return browser


def show_effects_browser(workspace):
    """Open the library at its current dock location or in its floating window."""
    workspace.exit_color_workspace()
    browser=ensure_effects_browser(workspace)
    workspace.show_inspector('library')
    browser.refresh()
    return browser
