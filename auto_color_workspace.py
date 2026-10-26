"""Cancelable, snapshot-checked automatic grading with a single undo step."""
from __future__ import annotations

import copy
from pathlib import Path
import threading
import tkinter as tk
from tkinter import messagebox

from auto_color import sample_analyze,build_auto_grade,PRESET_LABELS,reference_match_summary
from multicam_core import Cut
from creative_core import ColorGrade
from exporter_core import ExportCancelled
from transition_preview import transition_signature


class AutoColorWorkspaceMixin:
    def _init_auto_color(self):
        self.auto_color_status=tk.StringVar(self,value='默认仅所选片段 · 曝光偏好 +0.25 EV · 分析后可继续手动微调')
        self._auto_color_worker=None
        self._auto_color_cancel=threading.Event()
        self._auto_color_running=False

    def _auto_color_context(self):
        return (self.grade_target,tuple(self.selected_program_range) if self.selected_program_range else None,
                transition_signature(self.project),self.project.range_start,self.project.range_end,
                self.start_var.get(),self.end_var.get(),self.fps_var.get(),self.project.color_reference_camera)

    def set_color_reference_camera(self,camera):
        if not self._available():return False
        if camera is not None and (type(camera) is not int or camera not in self.project.cameras):return False
        if camera!=self.project.color_reference_camera:
            self.project.color_reference_camera=camera;self._mark_dirty()
        workspace=vars(self).get('color_workspace')
        panel=vars(workspace).get('auto_panel') if workspace is not None else None
        if panel is not None:panel.refresh_reference_choices()
        return True

    def _auto_color_set_running(self,value):
        self._auto_color_running=value
        workspace=vars(self).get('color_workspace')
        panel=vars(workspace).get('auto_panel') if workspace is not None else None
        if panel is not None:panel.set_running(value)

    def start_auto_color(self,preset='natural',bias_ev=.25,strength=1.,reference_camera=None):
        if not self._available() or self._auto_color_running:return False
        try:
            if not self.project.clips:raise ValueError('请先导入视频并选择片段。')
            ffmpeg=self.app.caps.ffmpeg_path
            if not ffmpeg or not Path(ffmpeg).is_file():raise ValueError('请等待 FFmpeg 检测完成。')
            if preset not in PRESET_LABELS:raise ValueError('自动调色风格无效。')
            snapshot=copy.deepcopy(self.project)
            if self.grade_target=='selection':
                if self.selected_program_range is None:
                    span=self._program_span_at(self.current)
                    if span is None:raise ValueError('请先选中需要调色的片段。')
                    self.select_program_range(span.start,span.end)
                first,last=self.selected_program_range
            else:
                first,range_end,_=self._read_range()
                last=range_end if range_end is not None else snapshot.duration
                snapshot.range_start,snapshot.range_end=first,range_end
                if isinstance(self.grade_target,int):
                    snapshot.cuts=[Cut(0,self.grade_target)];snapshot.transitions=[]
            if reference_camera is not None:
                if type(reference_camera) is not int or reference_camera not in snapshot.cameras:
                    raise ValueError('请选择有效的色彩参考机位。')
                if self.grade_target is None:raise ValueError('全片自动调色请使用自动判断；参考机位适用于所选片段或单个机位。')
                targets=({self.grade_target} if isinstance(self.grade_target,int) else
                         {snapshot.camera_at(first),*(cut.camera for cut in snapshot.cuts if first<cut.time<last)})
                if reference_camera in targets:raise ValueError('请选择与待调片段不同的机位作为色彩参考。')
                self.set_color_reference_camera(reference_camera)
            base=self.get_active_grade()
            context=self._auto_color_context()
        except Exception as error:
            messagebox.showerror('自动调色',str(error),parent=self);return False
        self.pause();self._cancel_grade_drafts()
        event=threading.Event();self._auto_color_cancel=event
        self._auto_color_set_running(True)
        self.auto_color_status.set('正在分析偏黄、肤色、白衣和高光…')
        def worker():
            try:
                analysis=sample_analyze(snapshot,first,last,ffmpeg,self.app.caps.ffprobe_path,
                    cancel_event=event,progress=lambda percent,text:self.jobs.put(('auto_color_progress',
                        (percent*(.55 if reference_camera is not None else 1.),text))))
                reference_analysis=None;reference_note=''
                if reference_camera is not None:
                    reference=copy.deepcopy(snapshot);reference.cuts=[Cut(0,reference_camera)];reference.transitions=[]
                    try:
                        reference_analysis=sample_analyze(reference,first,last,ffmpeg,self.app.caps.ffprobe_path,
                            sample_grade=reference.camera_grades.get(reference_camera,ColorGrade()),cancel_event=event,
                            progress=lambda percent,text:self.jobs.put(('auto_color_progress',
                                (55+percent*.45,f'参考机位 {reference_camera} · {text}'))))
                    except ExportCancelled:raise
                    except Exception as error:reference_note='参考机位当前不可用，已退回自动去黄：'+str(error).split('\n')[0][:100]
                grade=build_auto_grade(analysis,preset=preset,base_grade=base,bias_ev=bias_ev,strength=strength,
                                       reference_analysis=reference_analysis)
                self.jobs.put(('auto_color_ready',(context,analysis,grade,preset,bias_ev,event,
                                                  reference_analysis,reference_camera,reference_note)))
            except Exception as error:self.jobs.put(('auto_color_error',(str(error),event.is_set())))
        self._auto_color_worker=threading.Thread(target=worker,name='auto-color-analysis',daemon=False)
        self._auto_color_worker.start()
        return True

    def cancel_auto_color(self):
        self._auto_color_cancel.set()
        self.auto_color_status.set('正在取消分析，保留原调色…')

    def _handle_auto_color_job(self,kind,payload):
        if kind=='auto_color_progress':
            percent,text=payload;self.auto_color_status.set(f'{text} · {float(percent):.0f}%')
        elif kind=='auto_color_error':
            text,cancelled=payload;self._auto_color_set_running(False)
            self.auto_color_status.set('已取消，保留原调色。' if cancelled else '分析失败，保留原调色：'+text)
            self.status.set(self.auto_color_status.get())
        elif kind=='auto_color_ready':
            context,analysis,grade,preset,bias,event,*reference_data=payload
            reference_analysis=reference_data[0] if reference_data else None
            reference_camera=reference_data[1] if len(reference_data)>1 else None
            reference_note=reference_data[2] if len(reference_data)>2 else ''
            self._auto_color_set_running(False)
            if event.is_set():
                self.auto_color_status.set('已取消，保留原调色。');return
            if (context!=self._auto_color_context() or self.busy
                    or self._grade_preview_pending is not None or self._grade_preview_active):
                self.auto_color_status.set('片段、参数或当前任务已变化，分析结果未应用；请重新分析。');return
            if not analysis.usable:
                self.auto_color_status.set(analysis.summary);self.status.set(analysis.summary);return
            if self.apply_active_grade(grade):
                text=(f'{PRESET_LABELS[preset]} · {analysis.samples_used} 帧 · 曝光 {grade.exposure:+.2f} EV'
                      f' · 偏好 {bias:+.2f} EV · 可 Ctrl+Z 撤销')
                text+='\n'+reference_match_summary(analysis,reference_analysis)
                if reference_camera is not None:text+=f' · 参考机位 {reference_camera}'
                warnings=[warning for warning in analysis.warnings
                          if grade.gamma>1.001 or not warning.startswith('高光保护限制了正曝光增益')]
                if reference_analysis is not None:
                    for warning in reference_analysis.warnings:
                        warning='参考机位：'+warning
                        if warning not in warnings:warnings.append(warning)
                if grade.gamma>1.001 and not any('高光' in warning for warning in warnings):
                    warnings.append('已限制高光溢出，并温和提亮中间调')
                if warnings:text+='\n'+'；'.join(warnings)
                if reference_note:text+='\n'+reference_note
                self.auto_color_status.set(text);self.status.set(text.split('\n')[0])

    def _close_auto_color(self):
        self._auto_color_cancel.set()
