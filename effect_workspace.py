"""Asynchronous selected-cut stabilization and automatic transition previews."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import math
from pathlib import Path
import shutil
import tempfile
import threading
import uuid
from tkinter import messagebox

from exporter_core import ExportCancelled
from cache_paths import cache_dir
from gyroflow_core import (StabilizationSettings, StabilizedRange, validate_settings,
                           probe_source, run_stabilization)
from transition_preview import transition_signature, render_transition_previews


class EffectsWorkspaceMixin:
    def _init_effects(self):
        self._transition_preview_after = None
        self._transition_preview_worker = None
        self._transition_preview_cancel = threading.Event()
        self._transition_preview_signature = ''
        self._transition_preview_items = []
        self._transition_cache_folder = None
        self._stabilization_worker = None
        self._stabilization_probes = []
        self._stabilization_probe_cancel = threading.Event()

    def _selected_stabilization_inputs(self):
        if self.selected_program_range is None:
            raise ValueError('请先左键选中时间线中要防抖的片段。')
        start, end = self.selected_program_range
        fps = self.project.fps
        edges = {start, end}
        edges.update(cut.time for cut in self.project.cuts if start < cut.time < end)
        for clip in self.project.clips:
            edges.update(value for value in (clip.start, clip.end) if start < value < end)
        result = []
        for first, last in zip(sorted(edges), sorted(edges)[1:]):
            camera = self.project.camera_at(first + min(.25/fps, (last-first)/2))
            clip = self.project.clip_at(camera, first + min(.25/fps, (last-first)/2))
            if clip is None: continue
            if result and result[-1]['clip'].id == clip.id and abs(result[-1]['end']-first) < .5/fps:
                result[-1]['end'] = last
            else:
                result.append({'start': first, 'end': last, 'camera': camera, 'clip': copy.deepcopy(clip)})
        if not result: raise ValueError('所选范围没有可防抖的视频画面。')
        return result

    def get_selected_stabilization(self):
        if not self.selected_program_range: return None
        time = self.selected_program_range[0]
        camera = self.project.camera_at(time)
        clip = self.project.clip_at(camera, time)
        return self.project.stabilization_at(camera, time, clip.id if clip else '')

    def _gyroflow_executable(self, settings):
        candidates = [settings.gyroflow_path,
                      str(Path(__file__).resolve().parent/'bin'/'gyroflow'/'Gyroflow.exe'),
                      str(Path(__file__).resolve().parent/'bin'/'Gyroflow.exe'),
                      shutil.which('Gyroflow') or '']
        # Development checkout uses the same verified portable distribution.
        if Path(__file__).resolve().parent.parent.name == 'work':
            candidates.append(str(Path(__file__).resolve().parent.parent/'gyroflow_portable'/'Gyroflow.exe'))
        executable = next((value for value in candidates if value and Path(value).is_file()), '')
        if not executable:
            raise ValueError('未找到 Gyroflow.exe，请在手动设置中指定，或安装完整更新包。')
        return executable

    def probe_selected_stabilization(self, callback, settings=None):
        if not self._available(): return False
        try:
            inputs = self._selected_stabilization_inputs()
            settings = copy.deepcopy(settings or StabilizationSettings())
            executable = self._gyroflow_executable(settings)
            source = inputs[0]['clip'].path
        except Exception as error:
            callback({'error': str(error), 'message': str(error), 'has_gyro': False, 'has_lens': False})
            return False
        def worker():
            try:
                probe = probe_source(source, settings, gyroflow_path=executable,
                    ffprobe_path=self.app.caps.ffprobe_path, cancel_event=probe_cancel)
                info = asdict(probe)
                info['message'] = ('已检测到运动数据和镜头配置。' if probe.has_gyro and probe.has_lens
                                   else '；'.join(probe.problems) or '缺少陀螺仪数据或镜头配置。')
                if len(inputs) > 1: info['message'] += f' 此检测对应首个源文件，开始时会检查全部 {len(inputs)} 段。'
            except Exception as error:
                info = {'error': str(error), 'message': str(error), 'has_gyro': False, 'has_lens': False}
            self.jobs.put(('stabilization_probe', (callback, info)))
        probe_cancel = self._stabilization_probe_cancel
        thread = threading.Thread(target=worker, name='gyroflow-probe', daemon=False)
        self._stabilization_probes = [job for job in self._stabilization_probes if job.is_alive()]
        self._stabilization_probes.append(thread); thread.start()
        return True

    def start_stabilization(self, settings):
        if not self._available(): return False
        engine_name='画面防抖' if getattr(settings,'engine','gyroflow')=='image' else 'Gyroflow 防抖'
        try:
            validate_settings(settings, check_files=True)
            inputs = self._selected_stabilization_inputs()
            executable = '' if settings.engine=='image' else self._gyroflow_executable(settings)
            settings = copy.deepcopy(settings)
            if executable:settings = replace(settings, gyroflow_path=executable)
            ffprobe = self.app.caps.ffprobe_path
            if not ffprobe or not Path(ffprobe).is_file(): raise ValueError('请等待 ffprobe 检测完成。')
            if settings.engine=='image':
                ffmpeg=self.app.caps.ffmpeg_path
                if not ffmpeg or not Path(ffmpeg).is_file():raise ValueError('请等待 FFmpeg 检测完成。')
            folder = cache_dir('stabilization/image' if settings.engine=='image' else 'stabilization/gyroflow')
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            messagebox.showerror(engine_name, str(error), parent=self); return False
        self._stabilization_probe_cancel.set()
        self._stabilization_probe_cancel = threading.Event()
        self.pause(); self.app.cancel_event.clear()
        self.busy = True; self.app._set_working(True)
        self._stabilization_engine_name=engine_name
        self.app._begin_shared_progress(engine_name+'准备中')
        self.export_monitor.grid()
        self.export_phase_var.set(engine_name+(' · 分析画面运动 / 生成防抖画面' if settings.engine=='image' else ' · 检测运动数据 / 镜头'))
        self.export_speed_var.set('防抖处理中，完成后替换所选片段画面')
        self.export_progress_var.set(0)
        def worker():
            records = []
            try:
                for index, item in enumerate(inputs):
                    clip = item['clip']
                    first = max(0., item['start']-clip.start-2.5)
                    last = min(clip.duration, item['end']-clip.start+2.5)
                    output = folder/(uuid.uuid4().hex+'.mp4')
                    def progress(value, *extra):
                        percent = float(value)
                        self.jobs.put(('stabilization_progress',
                            ((index+min(100.,max(0.,percent))/100)/len(inputs)*100,
                             f'{engine_name} {index+1}/{len(inputs)} · {percent:.1f}%')))
                    result = run_stabilization(clip.path, first, last, settings, output,
                        gyroflow_path=executable, ffprobe_path=ffprobe,ffmpeg_path=self.app.caps.ffmpeg_path,
                        cancel_event=self.app.cancel_event, progress=progress,
                        log=lambda value: self.jobs.put(('stabilization_log', str(value))))
                    records.append(StabilizedRange(item['start'],item['end'],item['camera'],clip.id,
                        copy.deepcopy(settings),result.cache_path,result.signature,
                        result.source_start,result.source_end))
                self.jobs.put(('stabilization_ready', records))
            except Exception as error:
                for record in records:
                    cache=Path(record.cache_path)
                    if cache.parent.resolve()==folder.resolve():cache.unlink(missing_ok=True)
                self.jobs.put(('stabilization_error', (str(error), self.app.cancel_event.is_set()
                                                      or isinstance(error, ExportCancelled))))
        self._stabilization_worker = threading.Thread(target=worker, name='selected-range-stabilize', daemon=False)
        self._stabilization_worker.start()
        return True

    @staticmethod
    def _replace_stabilization_range(project, replacement):
        result = []
        for item in project.stabilizations:
            if (item.camera != replacement.camera or item.source_id != replacement.source_id
                    or item.end <= replacement.start or item.start >= replacement.end):
                result.append(item); continue
            if item.start < replacement.start: result.append(replace(item, end=replacement.start))
            if item.end > replacement.end: result.append(replace(item, start=replacement.end))
        project.stabilizations = sorted([*result,replacement], key=lambda item:(item.start,item.camera))

    def remove_selected_stabilization(self):
        if not self._available() or not self.selected_program_range: return False
        candidate = copy.deepcopy(self.project)
        for source in self._selected_stabilization_inputs():
            result = []
            for item in candidate.stabilizations:
                if (item.camera != source['camera'] or item.source_id != source['clip'].id
                        or item.end <= source['start'] or item.start >= source['end']):
                    result.append(item); continue
                if item.start < source['start']: result.append(replace(item,end=source['start']))
                if item.end > source['end']: result.append(replace(item,start=source['end']))
            candidate.stabilizations = result
        if candidate.stabilizations == self.project.stabilizations: return False
        candidate.validate(); self._remember(); self.project = candidate; self._refresh()
        self.status.set('已移除所选范围防抖，恢复原片画面；可撤销。')
        return True

    def _schedule_transition_previews(self):
        if self._transition_preview_after is not None:
            self.after_cancel(self._transition_preview_after); self._transition_preview_after = None
        if self._can_use_gpu_transitions():
            self._transition_preview_cancel.set()
            return
        if not self.project.transitions:
            self._transition_preview_items = []; self._transition_preview_signature = ''
            self._transition_preview_cancel.set()
            return
        signature = transition_signature(self.project)
        if self._transition_preview_signature == signature:
            self._publish_transition_previews(); return
        self._transition_preview_cancel.set()
        self._transition_preview_after = self.after(250,self._prepare_transition_previews)

    def _can_use_gpu_transitions(self):
        preference=getattr(self,'gpu_live_var',None)
        if preference is None or not preference.get() or getattr(self,'_gpu_live_failed',False):return False
        try:
            from gpu_live_session import KINDS,runtime_directory
            from gpu_live_player import missing_gpu_features
            runtime_directory()
            return not missing_gpu_features(self.project,{'transitions':KINDS,'grade_uniforms':True,
                                                          'lut':False,'subtitles':False})
        except (ImportError,OSError,RuntimeError):return False

    def _prepare_transition_previews(self):
        self._transition_preview_after = None
        if not self.alive or not self.project.transitions: return
        if self._can_use_gpu_transitions():return
        if self.busy or (self._transition_preview_worker and self._transition_preview_worker.is_alive()):
            self._transition_preview_after = self.after(150,self._prepare_transition_previews); return
        ffmpeg = self.app.caps.ffmpeg_path
        if not ffmpeg or not Path(ffmpeg).is_file(): return
        snapshot = copy.deepcopy(self.project)
        signature = transition_signature(snapshot)
        if self._transition_cache_folder is None:
            self._transition_cache_folder = Path(tempfile.mkdtemp(prefix='okk_transition_preview_',dir=cache_dir('previews/transitions')))
        event = threading.Event(); self._transition_preview_cancel = event
        self.status.set('正在后台准备转场，完成后普通回放即可看到效果…')
        def worker():
            try:
                items = render_transition_previews(snapshot,self._transition_cache_folder,ffmpeg,
                    self.app.caps.ffprobe_path,cancel_event=event,
                    use_nvenc='h264_nvenc' in getattr(self.app.caps,'nvenc_codecs',()))
                self.jobs.put(('transition_ready',(signature,items)))
            except Exception as error:
                self.jobs.put(('transition_error',(signature,str(error),event.is_set())))
        self._transition_preview_worker = threading.Thread(target=worker,name='transition-cache',daemon=False)
        self._transition_preview_worker.start()

    def _publish_transition_previews(self):
        if (self.player and self._transition_preview_signature
                and self._transition_preview_signature == transition_signature(self.project)):
            self.player.submit('transition_previews',signature=self._transition_preview_signature,
                               items=copy.deepcopy(self._transition_preview_items))

    def _handle_effect_job(self, kind, payload):
        if kind == 'transition_ready':
            signature, items = payload
            if signature == transition_signature(self.project):
                self._transition_preview_signature,self._transition_preview_items = signature,items
                self._publish_transition_previews()
                self.status.set('转场预览已就绪，普通回放与逐帧定位均可查看。')
        elif kind == 'transition_error':
            signature,text,cancelled = payload
            if not cancelled and signature == transition_signature(self.project):
                self.status.set('转场预览未生成：'+text)
                self.app._log('转场预览：'+text)
        elif kind == 'stabilization_probe':
            callback,info = payload
            if self.alive: callback(info)
        elif kind == 'stabilization_log': self.app._log(getattr(self,'_stabilization_engine_name','防抖')+'：'+payload)
        elif kind == 'stabilization_progress':
            percent,text = payload
            self.export_phase_var.set(text);self.export_progress_var.set(percent)
            self.app.progress_var.set(percent);self.app.progress_text_var.set(text);self.status.set(text)
        elif kind in ('stabilization_ready','stabilization_error'):
            self.busy = False;self.app._set_working(False)
            if kind == 'stabilization_ready':
                if self.app.cancel_event.is_set():
                    for record in payload:
                        cache=Path(record.cache_path)
                        expected=cache_dir('stabilization/image' if record.settings.engine=='image' else 'stabilization/gyroflow')
                        if cache.parent.resolve()==expected.resolve():cache.unlink(missing_ok=True)
                    self.status.set('已取消防抖，保留原片段画面。');self.export_phase_var.set(self.status.get())
                    return
                candidate = copy.deepcopy(self.project)
                for record in payload: self._replace_stabilization_range(candidate,record)
                candidate.validate();self._remember();self.project = candidate;self._refresh()
                self.app.progress_var.set(100);self.export_progress_var.set(100)
                self.export_phase_var.set(getattr(self,'_stabilization_engine_name','防抖')+'完成')
                self.status.set('所选片段防抖已应用，预览和导出均使用防抖画面；原片保留。')
            else:
                text,cancelled = payload
                self.status.set('已取消防抖' if cancelled else '防抖失败：'+text)
                self.export_phase_var.set(self.status.get())
                if not cancelled: messagebox.showerror(getattr(self,'_stabilization_engine_name','防抖'),text,parent=self)

    def _close_effects(self):
        self._stabilization_probe_cancel.set();self._transition_preview_cancel.set()
        if self._transition_preview_after is not None:
            self.after_cancel(self._transition_preview_after);self._transition_preview_after = None
        folder,worker,player = self._transition_cache_folder,self._transition_preview_worker,self.player
        if folder is not None:
            def cleanup():
                if worker: worker.join(timeout=20)
                if worker and worker.is_alive(): return
                if player: player.wait_closed(timeout=10)
                resolved = folder.resolve()
                if resolved.parent == cache_dir('previews/transitions').resolve() and resolved.name.startswith('okk_transition_preview_'):
                    shutil.rmtree(resolved,ignore_errors=True)
            threading.Thread(target=cleanup,name='effect-cache-cleanup',daemon=False).start()
