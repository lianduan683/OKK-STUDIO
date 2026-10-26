"""Simple selected-cut automatic grading controls for the colour workspace."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from auto_color import PRESET_LABELS

BG,PANEL,INK,MUTED,ACCENT='#202124','#292b2e','#dedfe3','#969ba3','#58a6d6'


class AutoColorPanel(tk.Frame):
    def __init__(self,parent,owner):
        super().__init__(parent,bg=BG)
        self.owner,self.ws=owner,owner.ws
        self.preset_var=tk.StringVar(self,value=PRESET_LABELS['natural'])
        self.bias_var=tk.StringVar(self,value='微亮 +0.25 EV（推荐）')
        self.strength_var=tk.DoubleVar(self,value=100)
        self.reference_var=tk.StringVar(self,value='自动去黄（不跟随机位）')
        self._reference_choices={'自动去黄（不跟随机位）':None}
        self._running=False
        self.option_widgets=[]
        self.status_var=getattr(self.ws,'auto_color_status',None) or tk.StringVar(self,value='选择片段后，一键分析并应用。')
        self.columnconfigure(0,weight=1)
        footer=tk.Frame(self,bg=PANEL)
        footer.pack(side='bottom',fill='x')
        self.apply_button=ttk.Button(footer,text='分析并应用',command=self.apply,style='Compact.TButton')
        self.apply_button.pack(side='left',padx=owner.px(8),pady=owner.px(6))
        self.cancel_button=ttk.Button(footer,text='取消分析',command=self.cancel,style='Compact.TButton',state='disabled')
        self.cancel_button.pack(side='left',padx=owner.px(2),pady=owner.px(6))
        ttk.Button(footer,text='撤销一次',command=lambda:getattr(self.ws,'undo',lambda:None)(),
                   style='Compact.TButton').pack(side='right',padx=owner.px(8))
        canvas=tk.Canvas(self,bg=BG,highlightthickness=0,height=owner.px(220))
        scroll=ttk.Scrollbar(self,orient='vertical',command=canvas.yview)
        scroll.pack(side='right',fill='y');canvas.pack(fill='both',expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body=tk.Frame(canvas,bg=BG);window=canvas.create_window(0,0,window=body,anchor='nw')
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=max(1,event.width)))
        body.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        tk.Label(body,text='自动调色 · 先分析，再微调',bg=BG,fg=INK,
                 font=('Microsoft YaHei UI',11,'bold')).pack(anchor='w',padx=owner.px(12),pady=(owner.px(8),owner.px(3)))
        target=tk.Label(body,textvariable=owner.scope_var,bg=BG,fg=ACCENT,font=('Microsoft YaHei UI',9))
        target.pack(anchor='w',padx=owner.px(12))
        for label,var,values in (
            ('风格',self.preset_var,list(PRESET_LABELS.values())),
            ('曝光偏好',self.bias_var,['正常 +0.00 EV','微亮 +0.25 EV（推荐）','明亮 +0.50 EV']),
        ):
            row=tk.Frame(body,bg=BG);row.pack(fill='x',padx=owner.px(12),pady=owner.px(4))
            tk.Label(row,text=label,bg=BG,fg=MUTED,font=('Microsoft YaHei UI',9),width=9,anchor='w').pack(side='left')
            combo=ttk.Combobox(row,textvariable=var,values=values,state='readonly',width=25)
            combo.pack(side='left');self.option_widgets.append(combo)
        row=tk.Frame(body,bg=BG);row.pack(fill='x',padx=owner.px(12),pady=owner.px(4))
        tk.Label(row,text='色彩参考',bg=BG,fg=MUTED,font=('Microsoft YaHei UI',9),width=9,anchor='w').pack(side='left')
        self.reference_combo=ttk.Combobox(row,textvariable=self.reference_var,state='readonly',width=25)
        self.reference_combo.pack(side='left');self.option_widgets.append(self.reference_combo)
        self.reference_combo.bind('<<ComboboxSelected>>',self._reference_changed)
        row=tk.Frame(body,bg=BG);row.pack(fill='x',padx=owner.px(12))
        tk.Label(row,text='预设强度',bg=BG,fg=MUTED,font=('Microsoft YaHei UI',9),width=9,anchor='w').pack(side='left')
        scale=ttk.Scale(row,from_=0,to=100,variable=self.strength_var,orient='horizontal')
        scale.pack(side='left',fill='x',expand=True);self.option_widgets.append(scale)
        tk.Label(body,text='偏黄机位可选较满意的机位作色彩参考，再用人物预设去黄、提亮。默认仅所选 Cut，手动轮/曲线/LUT继续叠加。',
                 bg=BG,fg=MUTED,font=('Microsoft YaHei UI',9),anchor='w',justify='left',wraplength=800).pack(
                     fill='x',padx=owner.px(12),pady=owner.px(5))
        status=tk.Label(body,textvariable=self.status_var,bg=BG,fg=INK,font=('Microsoft YaHei UI',9),anchor='w',justify='left')
        status.pack(fill='x',padx=owner.px(12),pady=owner.px(4))
        body.bind('<Configure>',lambda event:status.configure(wraplength=max(120,event.width-owner.px(24))),add='+')
        tag=f'AutoColorScroll{canvas.winfo_id()}'
        def scroll_wheel(event):
            canvas.yview_scroll(-1 if event.delta>0 else 1,'units');return 'break'
        self.bind_class(tag,'<MouseWheel>',scroll_wheel)
        def install(widget):
            if not isinstance(widget,(tk.Entry,ttk.Scale,ttk.Combobox)):
                widget.bindtags((tag,)+widget.bindtags())
            for child in widget.winfo_children():install(child)
        install(body)
        self.refresh_reference_choices()

    def refresh_reference_choices(self):
        default='自动去黄（不跟随机位）'
        self._reference_choices={default:None}
        target=getattr(self.ws,'grade_target','selection')
        selected=getattr(self.ws,'selected_program_range',None)
        excluded=set()
        project=self.ws.project
        if isinstance(target,int):excluded.add(target)
        elif target=='selection' and selected:
            excluded.add(project.camera_at(selected[0]))
            excluded.update(cut.camera for cut in project.cuts if selected[0]<cut.time<selected[1])
        if target is not None:
            for camera in project.cameras:
                if camera not in excluded:
                    label=getattr(self.ws,'_label_camera',lambda value:f'机位 {value}')(camera)
                    self._reference_choices[label]=camera
        reference=getattr(project,'color_reference_camera',None)
        self.reference_combo.configure(values=list(self._reference_choices))
        self.reference_var.set(next((label for label,value in self._reference_choices.items() if value==reference),default))
        self.reference_combo.configure(state='disabled' if self._running or len(self._reference_choices)<2 else 'readonly')

    def _reference_changed(self,event=None):
        callback=getattr(self.ws,'set_color_reference_camera',None)
        if callable(callback):callback(self._reference_choices.get(self.reference_var.get()))

    def apply(self):
        callback=getattr(self.ws,'start_auto_color',None)
        if not callable(callback): return
        preset=next((key for key,value in PRESET_LABELS.items() if value==self.preset_var.get()),'natural')
        bias=.5 if '+0.50' in self.bias_var.get() else .25 if '+0.25' in self.bias_var.get() else 0.
        callback(preset=preset,bias_ev=bias,strength=self.strength_var.get()/100.,
                 reference_camera=self._reference_choices.get(self.reference_var.get()))

    def cancel(self):
        callback=getattr(self.ws,'cancel_auto_color',None)
        if callable(callback):callback()

    def set_running(self,value):
        self._running=bool(value)
        self.apply_button.configure(state='disabled' if value else 'normal')
        self.cancel_button.configure(state='normal' if value else 'disabled')
        for widget in self.option_widgets:widget.state(['disabled'] if value else ['!disabled'])
        self.refresh_reference_choices()
