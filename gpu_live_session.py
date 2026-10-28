"""Two hardware-decoded libmpv textures presented by one D3D11 output.

The MPV event pumps, command caller and render thread are independent. Native
render calls never wait for ordinary MPV commands. No helper EXE is executed.
"""
from __future__ import annotations
from concurrent.futures import Future
import ctypes as C
import hashlib
import json
import math
import sys
from pathlib import Path
import threading
import time

from gpu_d3d11 import D3D11Compositor
from gpu_mpv_api import Library,Core

KINDS=('fade','dissolve','wipeleft','wiperight','slideleft','slideright','circleopen','fadeblack','fadewhite')


def runtime_directory(preferred=None):
    candidates=[Path(preferred)] if preferred else []
    root=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
    candidates.extend((root/'bin/gpu_live',root/'native_runtime/gpu_live'))
    for path in candidates:
        if all((path/name).is_file() for name in ('libmpv-2.dll','vertex.cso','composite.cso','shader_layout.json')):
            return path.resolve()
    raise RuntimeError('未找到完整 GPU 实时渲染运行库。')


class GpuLiveSession:
    def __init__(self,parent_hwnd,*,runtime=None,width=640,height=360,diagnostics=False,adapter_preference=None):
        self.parent=int(parent_hwnd or 0);self.runtime=runtime_directory(runtime)
        self.adapter_preference=adapter_preference
        self.width,self.height=int(width),int(height);self.diagnostics=bool(diagnostics)
        self._lock=threading.Lock();self._ready=threading.Event();self._stop=threading.Event();self._wake=threading.Event()
        self._thread=None;self._error=None;self._closed=False;self._started=False;self._library=None
        self._cores={};self._contexts=[];self._gfx=None;self._capture=[]
        self._grades={'a':[0.]*612,'b':[0.]*612}
        self._mix={'front_slot':'a','other_slot':'b','kind':'fade','progress':0.,'active':False,
                   'rate':0.,'received':time.perf_counter()}
        self._present_count=0;self._readbacks=0;self._adapter='';self._output_hwnd=0;self._render_debug=[{},{}]
        self._presented_render_generations={'a':0,'b':0}
        self._visible=True
        layout=json.loads((self.runtime/'shader_layout.json').read_text(encoding='utf-8'))
        names=tuple(layout['side_uniforms'])
        if len(names)!=612:raise RuntimeError('GPU 调色布局数量不匹配。')
        digest=hashlib.sha256('\n'.join(names).encode('utf-8')).hexdigest()
        self.capabilities={'protocol':1,'slots':['a','b'],'backend':'d3d11-texture',
            'transitions':list(KINDS),'grade_uniforms':True,'grade_count':612,
            'grade_layout_hash':digest,'grade_fields':list(names),'lut':False,'subtitles':False,
            'gpu_only_video_path':True,'external_executable':False}

    def start(self,timeout=12.):
        if self._closed:raise RuntimeError('GPU render session is closed')
        if self._started:
            self._healthy();return
        self._started=True
        try:
            self._library=Library(self.runtime/'libmpv-2.dll')
            options={'target-prim':'bt.709','target-trc':'bt.1886','osd-level':'0',
                     'input-default-bindings':'no','load-scripts':'no'}
            for slot in ('a','b'):self._cores[slot]=Core(self._library,options,name='programme-'+slot)
            self._thread=threading.Thread(target=self._run,name='programme-d3d11-render',daemon=False)
            self._thread.start()
            if not self._ready.wait(timeout):raise TimeoutError('GPU renderer initialization deadline expired')
            self._healthy()
        except Exception:
            self.close();raise

    def _healthy(self):
        if self._error:raise RuntimeError('GPU render failure: '+self._error)
        if self._closed:raise RuntimeError('GPU render session is closed')

    def request(self,payload,*,timeout=3.):
        op=payload.get('op')
        if op=='capabilities':return dict(self.capabilities)
        if op=='command':return self.command(payload['slot'],payload['command'],timeout=timeout)
        if op=='frame_state':return self.frame_state(payload['slot'],timeout=timeout)
        if op=='grade':return self.grade(payload['slot'],payload['values'],payload.get('layout_hash'),payload.get('lut_paths'))
        if op=='compose':
            return self.compose(**{key:value for key,value in payload.items() if key not in ('op','request_id')})
        if op=='resize':return self.resize(payload['width'],payload['height'])
        if op=='set_visible':return self.set_visible(payload['visible'])
        if op=='screenshot':return self.screenshot(payload['path'],timeout=timeout)
        if op=='quit':return self.close()
        raise ValueError('Unsupported GPU operation: '+str(op))

    def command(self,slot,command,timeout=3.):
        self._healthy()
        return self._core(slot).command(command,timeout)

    def _core(self,slot):
        if slot not in ('a','b'):raise ValueError('Invalid GPU source slot')
        if slot not in self._cores:raise RuntimeError('GPU source is not initialized')
        return self._cores[slot]

    def frame_state(self,slot,timeout=.5):
        self._healthy();value=self._core(slot).frame_state()
        video=value.get('video-params') or value.get('video-dec-params') or {}
        value.update(time_pos=value.get('time-pos'),hwdec=value.get('hwdec-current'),
                     width=video.get('w',0),height=video.get('h',0),
                     output_hwnd=self._output_hwnd,present_generation=self._present_count,
                     presented_render_generation=self._presented_render_generations[slot],
                     adapter=self._adapter,render_debug=dict(self._render_debug[0 if slot=='a' else 1]))
        return value

    def grade(self,slot,values,layout_hash=None,lut_paths=None):
        self._healthy();self._core(slot)
        if layout_hash!=self.capabilities['grade_layout_hash']:raise ValueError('GPU grade layout does not match')
        if len(values)!=612 or not all(math.isfinite(float(value)) for value in values):
            raise ValueError('GPU grade controls must contain 612 finite values')
        if any(lut_paths or []):raise RuntimeError('此 GPU 预览暂未加载用户 LUT，请使用精确效果预览。')
        with self._lock:self._grades[slot]=[float(value) for value in values]
        self._wake.set()

    def compose(self,front_slot='a',other_slot=None,kind='fade',progress=0.,active=False,subtitle=None,**extra):
        self._healthy()
        if front_slot not in ('a','b') or other_slot not in (None,'a','b'):raise ValueError('Invalid GPU composition slots')
        if active and (front_slot==other_slot or kind not in KINDS):raise ValueError('Invalid GPU transition')
        if subtitle:raise RuntimeError('此 GPU 预览暂未加载字幕，请使用精确效果预览。')
        progress=float(progress)
        if not math.isfinite(progress) or not 0<=progress<=1:raise ValueError('Invalid GPU transition progress')
        rate=float(extra.get('progress_rate',0.))
        if not math.isfinite(rate):raise ValueError('Invalid GPU transition rate')
        delay=max(0.,float(extra.get('start_delay',0.)))
        with self._lock:
            self._mix={'front_slot':front_slot,'other_slot':other_slot or ('b' if front_slot=='a' else 'a'),
                'kind':kind,'progress':progress,'active':bool(active),'rate':rate,'received':time.perf_counter()+delay,
                'blank':bool(extra.get('blank',False))}
        self._wake.set()

    def resize(self,width,height):
        if type(width) is not int or type(height) is not int or not 16<=width<=8192 or not 16<=height<=8192:
            raise ValueError('GPU output size is invalid')
        with self._lock:self.width,self.height=width,height
        self._wake.set()

    def set_visible(self,visible):
        """Suspend presentation without replacing either decoder or output HWND."""
        if type(visible) is not bool:raise ValueError('GPU visibility must be a boolean')
        self._healthy()
        with self._lock:self._visible=visible
        self._wake.set()

    def screenshot(self,path,timeout=3.):
        if not self.diagnostics:raise RuntimeError('GPU pixel readback is disabled during normal playback')
        future=Future()
        with self._lock:self._capture.append((str(Path(path).resolve()),future))
        self._wake.set()
        return future.result(timeout)

    def _run(self):
        gfx=None;contexts=[]
        try:
            options={'adapter_preference':self.adapter_preference} if self.adapter_preference is not None else {}
            gfx=D3D11Compositor(self.runtime,self.parent,self.width,self.height,**options);self._gfx=gfx
            self._adapter=gfx.adapter_name;self._output_hwnd=int(gfx.window or 0)
            for core in self._cores.values():contexts.append(core.create_context(gfx.device.value.value,self._wake))
            self._contexts=contexts
            self._ready.set();pending=[False,False];visible_applied=True
            while not self._stop.is_set():
                began=time.perf_counter();gfx.pump_messages()
                with self._lock:
                    requested=(self.width,self.height);mix=dict(self._mix)
                    grades={key:list(value) for key,value in self._grades.items()}
                    captures=self._capture;self._capture=[];visible=self._visible
                if visible_applied!=visible:
                    gfx.set_visible(visible);visible_applied=visible
                if not visible and not captures:
                    # Stack preview owns the displayed picture. Keep the
                    # decoder/context/HWND alive, but do no resize, texture
                    # draw or 60-Hz Present work until it is shown again.
                    self._wake.wait(.1);self._wake.clear()
                    continue
                size=gfx.client_size() if self.parent else requested
                resized=gfx.resize(*size)
                for index,context in enumerate(contexts):
                    updates=context.update()
                    if updates&1:pending[index]=True
                    info=context.get_frame_info() if pending[index] else None
                    state=self._cores[('a','b')[index]].frame_state()
                    first_frame=not state.get('render_generation',0) and bool(state.get('video-out-params'))
                    self._render_debug[index]={'updates':updates,'pending':pending[index],'info':info,'clock':context.time_ns()}
                    due=bool(info and info['present'] and (info['redraw'] or not info['target_time']
                               or info['target_time']<=context.time_ns()+1_000_000))
                    if due or first_frame or (resized and state.get('render_generation',0)):
                        context.render_to_texture(gfx.videos[index].texture.value.value)
                        pending[index]=False
                front=0 if mix['front_slot']=='a' else 1
                other=0 if mix['other_slot']=='a' else 1
                phase=max(0.,min(1.,mix['progress']+mix['rate']*(time.perf_counter()-mix['received'])))
                if mix.get('blank'):
                    gfx.clear_output()
                else:
                    gfx.draw(front,other,KINDS.index(mix['kind']) if mix['active'] else -1,phase,
                             grades[mix['front_slot']],grades[mix['other_slot']])
                for path,future in captures:
                    try:
                        result=gfx.diagnostic_bmp(path);self._readbacks+=1
                        if not future.done():future.set_result(result)
                    except Exception as error:
                        if not future.done():future.set_exception(error)
                gfx.present();self._present_count+=1
                for index,context in enumerate(contexts):
                    slot=('a','b')[index]
                    generation=self._cores[slot].frame_state().get('render_generation',0)
                    self._presented_render_generations[slot]=generation
                    if generation:context.report_swap()
                self._wake.wait(max(0.,1/60-(time.perf_counter()-began)));self._wake.clear()
        except Exception as error:
            self._error=str(error);self._ready.set()
            with self._lock:captures=self._capture;self._capture=[]
            for _,future in captures:
                if not future.done():future.set_exception(error)
        finally:
            for context in reversed(contexts):
                try:context.free_context()
                except Exception as error:self._error=self._error or str(error)
            self._contexts=[]
            if gfx:gfx.close()
            self._gfx=None

    def close(self):
        if self._closed and not self._cores:return
        self._closed=True
        if self._thread is not None and self._thread.is_alive():
            requests=[]
            for core in self._cores.values():
                try:requests.append(core.command_async(['stop'],timeout=.8))
                except Exception:pass
            end=time.monotonic()+1.
            for request in requests:
                try:request.result(max(0.,end-time.monotonic()))
                except Exception:pass
            self._stop.set();self._wake.set();self._thread.join(8)
            if self._thread.is_alive():raise TimeoutError('GPU render thread did not close')
        errors=[]
        for slot,core in list(self._cores.items()):
            try:core.close()
            except Exception as error:errors.append(error)
            else:self._cores.pop(slot,None)
        with self._lock:captures=self._capture;self._capture=[]
        for _,future in captures:
            if not future.done():future.set_exception(RuntimeError('GPU render session is closed'))
        if not self._cores:self._library=None
        if errors:raise errors[0]
