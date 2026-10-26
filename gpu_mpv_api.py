"""ctypes libmpv client/render API with independent asynchronous event pumps.

Normal commands never wait on the render thread. Event data is copied before
the next mpv_wait_event call. Render callbacks only signal Python Events.
ABI definitions follow the bundled client.h, render.h and render_d3d11.h.
"""
from __future__ import annotations
from concurrent.futures import Future
import copy
import ctypes as C
from dataclasses import dataclass
import itertools
import math
import os
from pathlib import Path
import threading
import time

NONE,STRING,OSD_STRING,FLAG,INT64,DOUBLE,NODE,ARRAY,MAP,BYTE_ARRAY=range(10)
EVENT_NONE,EVENT_SHUTDOWN,EVENT_GET,EVENT_SET,EVENT_COMMAND=0,1,3,4,5
EVENT_START,EVENT_END,EVENT_LOADED,EVENT_SEEK,EVENT_RESTART,EVENT_PROPERTY,EVENT_OVERFLOW=6,7,8,20,21,22,24
RENDER_UPDATE_FRAME=1


class Node(C.Structure):pass
class NodeList(C.Structure):pass
class ByteArray(C.Structure):_fields_=[('data',C.c_void_p),('size',C.c_size_t)]
class NodeUnion(C.Union):
    _fields_=[('string',C.c_char_p),('flag',C.c_int),('int64',C.c_int64),('double_',C.c_double),
              ('list',C.POINTER(NodeList)),('ba',C.POINTER(ByteArray))]
Node._fields_=[('u',NodeUnion),('format',C.c_int)]
NodeList._fields_=[('num',C.c_int),('values',C.POINTER(Node)),('keys',C.POINTER(C.c_char_p))]
class EventProperty(C.Structure):_fields_=[('name',C.c_char_p),('format',C.c_int),('data',C.c_void_p)]
class EventCommand(C.Structure):_fields_=[('result',Node)]
class Event(C.Structure):
    _fields_=[('event_id',C.c_int),('error',C.c_int),('reply_userdata',C.c_uint64),('data',C.c_void_p)]
class RenderParam(C.Structure):_fields_=[('type',C.c_int),('data',C.c_void_p)]
class D3D11InitParams(C.Structure):_fields_=[('device',C.c_void_p)]
class D3D11FBO(C.Structure):_fields_=[('tex',C.c_void_p)]
class RenderFrameInfo(C.Structure):_fields_=[('flags',C.c_uint64),('target_time',C.c_int64)]
RenderUpdateCallback=C.CFUNCTYPE(None,C.c_void_p)


def _text(value):
    if not isinstance(value,str) or '\0' in value:raise ValueError('libmpv strings must be NUL-free text')
    return value.encode('utf-8')


def _void_pointer(value):
    return value if isinstance(value,C.c_void_p) else C.c_void_p(value)


class OwnedNode:
    """Python-owned node tree. Never pass it to mpv_free_node_contents."""
    def __init__(self,value):self.references=[];self.node=self._encode(value,0)
    def keep(self,value):self.references.append(value);return value
    def _encode(self,value,depth):
        if depth>32:raise ValueError('libmpv node nesting exceeds 32 levels')
        node=Node()
        if value is None:node.format=NONE
        elif isinstance(value,bool):node.format=FLAG;node.u.flag=int(value)
        elif isinstance(value,int):
            if not -(1<<63)<=value<(1<<63):raise ValueError('libmpv integer exceeds int64')
            node.format=INT64;node.u.int64=value
        elif isinstance(value,float):
            if not math.isfinite(value):raise ValueError('libmpv number must be finite')
            node.format=DOUBLE;node.u.double_=value
        elif isinstance(value,str):node.format=STRING;node.u.string=self.keep(_text(value))
        elif isinstance(value,(bytes,bytearray,memoryview)):
            data=self.keep(C.create_string_buffer(bytes(value)));array=self.keep(ByteArray(C.cast(data,C.c_void_p),len(value)))
            node.format=BYTE_ARRAY;node.u.ba=C.pointer(array)
        elif isinstance(value,(list,tuple,dict)):
            if len(value)>100000:raise ValueError('libmpv node collection is too large')
            mapping=isinstance(value,dict);items=list(value.values()) if mapping else list(value)
            values=self.keep((Node*len(items))(*(self._encode(item,depth+1) for item in items)))
            keys=self.keep((C.c_char_p*len(value))(*(self.keep(_text(key)) for key in value))) if mapping else None
            array=self.keep(NodeList(len(items),values,keys))
            node.format=MAP if mapping else ARRAY;node.u.list=C.pointer(array)
        else:raise TypeError(f'Unsupported libmpv node value: {type(value).__name__}')
        return node


def node_value(node,depth=0):
    """Copy an mpv-owned node before the next event is requested."""
    if depth>32:raise ValueError('libmpv node nesting exceeds 32 levels')
    kind=node.format
    if kind==NONE:return None
    if kind in (STRING,OSD_STRING):return (node.u.string or b'').decode('utf-8','replace')
    if kind==FLAG:return bool(node.u.flag)
    if kind==INT64:return int(node.u.int64)
    if kind==DOUBLE:return float(node.u.double_)
    if kind in (ARRAY,MAP):
        if not node.u.list:return [] if kind==ARRAY else {}
        values=node.u.list.contents
        if not 0<=values.num<=100000:raise ValueError('Invalid libmpv node collection size')
        if values.num and not values.values:raise ValueError('Missing libmpv node values')
        result=[node_value(values.values[index],depth+1) for index in range(values.num)]
        if kind==ARRAY:return result
        if values.num and not values.keys:raise ValueError('Missing libmpv node map keys')
        return {(values.keys[index] or b'').decode('utf-8','replace'):item for index,item in enumerate(result)}
    if kind==BYTE_ARRAY:
        if not node.u.ba:return b''
        array=node.u.ba.contents
        if array.size>64*1024*1024:raise ValueError('libmpv byte array is too large')
        return C.string_at(array.data,array.size) if array.size else b''
    raise ValueError(f'Unknown libmpv node format {kind}')


def property_value(prop):
    if prop.format==NONE or not prop.data:return None
    if prop.format==NODE:return node_value(C.cast(prop.data,C.POINTER(Node)).contents)
    scalar={FLAG:C.c_int,INT64:C.c_int64,DOUBLE:C.c_double,STRING:C.c_char_p,OSD_STRING:C.c_char_p}.get(prop.format)
    if scalar is None:raise ValueError(f'Unknown property format {prop.format}')
    value=C.cast(prop.data,C.POINTER(scalar)).contents.value
    if prop.format==FLAG:return bool(value)
    if prop.format in (STRING,OSD_STRING):return (value or b'').decode('utf-8','replace')
    return value


class MpvError(RuntimeError):
    def __init__(self,code,message):super().__init__(f'libmpv {code}: {message}');self.code=code


class Library:
    def __init__(self,path=None,*,dll=None):
        self.path=Path(path or Path(__file__).parent/'native_runtime/gpu_live/libmpv-2.dll').resolve()
        self._directory=os.add_dll_directory(str(self.path.parent)) if dll is None and os.name=='nt' else None
        self.dll=dll or C.CDLL(str(self.path))
        specs={
            'mpv_client_api_version':([],C.c_ulong),'mpv_create':([],C.c_void_p),
            'mpv_set_option_string':([C.c_void_p,C.c_char_p,C.c_char_p],C.c_int),
            'mpv_initialize':([C.c_void_p],C.c_int),'mpv_error_string':([C.c_int],C.c_char_p),
            'mpv_command_node_async':([C.c_void_p,C.c_uint64,C.POINTER(Node)],C.c_int),
            'mpv_get_property_async':([C.c_void_p,C.c_uint64,C.c_char_p,C.c_int],C.c_int),
            'mpv_set_property_async':([C.c_void_p,C.c_uint64,C.c_char_p,C.c_int,C.c_void_p],C.c_int),
            'mpv_observe_property':([C.c_void_p,C.c_uint64,C.c_char_p,C.c_int],C.c_int),
            'mpv_abort_async_command':([C.c_void_p,C.c_uint64],None),
            'mpv_wait_event':([C.c_void_p,C.c_double],C.POINTER(Event)),
            'mpv_wakeup':([C.c_void_p],None),'mpv_terminate_destroy':([C.c_void_p],None),
            'mpv_get_time_us':([C.c_void_p],C.c_int64),
            'mpv_get_time_ns':([C.c_void_p],C.c_int64),
            'mpv_render_context_create':([C.POINTER(C.c_void_p),C.c_void_p,C.POINTER(RenderParam)],C.c_int),
            'mpv_render_context_set_update_callback':([C.c_void_p,C.c_void_p,C.c_void_p],None),
            'mpv_render_context_update':([C.c_void_p],C.c_uint64),
            'mpv_render_context_get_info':([C.c_void_p,RenderParam],C.c_int),
            'mpv_render_context_render':([C.c_void_p,C.POINTER(RenderParam)],C.c_int),
            'mpv_render_context_report_swap':([C.c_void_p],None),
            'mpv_render_context_free':([C.c_void_p],None)}
        for name,(arguments,result) in specs.items():
            function=getattr(self.dll,name);function.argtypes=arguments;function.restype=result
        self.version=int(self.mpv_client_api_version())
        if self.version>>16!=2 or self.version&0xffff<6:
            raise RuntimeError(f'libmpv client API 2.6 or later required, found {self.version>>16}.{self.version&0xffff}')
    def __getattr__(self,name):return getattr(self.dll,name)
    def error(self,code):return MpvError(code,(self.mpv_error_string(code) or b'unknown error').decode('utf-8','replace'))
    def create_cores(self,count=2,**kwargs):
        result=[]
        try:
            for index in range(count):result.append(Core(self,name=f'gpu-{index}',**kwargs))
            return tuple(result)
        except Exception:
            for core in result:core.close()
            raise


class _CommandFuture(Future):
    def __init__(self,core):super().__init__();self._core=core
    def result(self,timeout=None):
        if not self.done() and self._core._cannot_wait():
            raise RuntimeError('Blocking libmpv command waits are forbidden on render/event threads')
        return super().result(timeout)


@dataclass
class _Pending:
    future: Future
    deadline: float
    owner: object
    description: str
    property_name: str|None=None


DEFAULT_OBSERVATIONS=('path','time-pos','pause','seeking','hwdec-current','hwdec-interop',
    'video-params','video-dec-params','video-out-params','container-fps','estimated-vf-fps',
    'eof-reached','idle-active','speed','demuxer-cache-state')


class Core:
    def __init__(self,library=None,options=None,*,name='gpu',observations=DEFAULT_OBSERVATIONS):
        self.library=library or Library();self.name=name;self.handle=self.library.mpv_create()
        if not self.handle:raise RuntimeError('mpv_create failed')
        self._pending={};self._pending_lock=threading.Lock();self._ids=itertools.count(1)
        self._state_lock=threading.Lock();self._properties={};self._observe_errors={}
        self._file_generation=0;self._start_generation=0;self._render_generation=0;self._loading=False
        self._last_update=0.;self._event_error=None;self._overflow=False
        self._contexts=set();self._render_threads=set();self._stop=threading.Event();self._destroyed=False
        self._thread=None
        configuration={'config':'no','vo':'libmpv','hwdec':'d3d11va','hwdec-software-fallback':'no',
                       'audio':'no','pause':'yes','terminal':'no','keep-open':'yes','idle':'yes'}
        configuration.update(options or {})
        try:
            for key,value in configuration.items():
                if isinstance(value,bool):value='yes' if value else 'no'
                self._check(self.library.mpv_set_option_string(self.handle,_text(key),_text(str(value))))
            self._check(self.library.mpv_initialize(self.handle))
            for identity,key in enumerate(observations,1):
                result=self.library.mpv_observe_property(self.handle,identity,_text(key),NODE)
                if result<0:self._observe_errors[key]=result
            self._thread=threading.Thread(target=self._pump,name=f'libmpv-events-{name}',daemon=False)
            self._thread.start()
        except Exception:
            self.library.mpv_terminate_destroy(self.handle);self.handle=None;self._destroyed=True;raise
    def _check(self,result):
        if result<0:raise self.library.error(result)
        return result
    def _cannot_wait(self):
        identity=threading.get_ident()
        return identity in self._render_threads or (self._thread is not None and identity==self._thread.ident)
    def command_async(self,command,timeout=1.):
        if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not math.isfinite(timeout) or timeout<=0:
            raise ValueError('Command timeout must be positive and finite')
        if self._destroyed or self._stop.is_set():raise RuntimeError('libmpv core is closed')
        if not isinstance(command,(list,tuple,dict)) or not command:raise ValueError('Expected a libmpv command array/map')
        owner=OwnedNode(command);identity=next(self._ids);future=_CommandFuture(self)
        key=command[1] if isinstance(command,(list,tuple)) and len(command)>1 and command[0] in ('get_property','set_property') else None
        pending=_Pending(future,time.monotonic()+timeout,owner,str(command[0] if not isinstance(command,dict) else command.get('name')),key)
        with self._pending_lock:
            if self._stop.is_set():raise RuntimeError('libmpv core is closed')
            if len(self._pending)>=1024:raise RuntimeError('Too many pending libmpv requests')
            self._pending[identity]=pending
        try:
            if isinstance(command,(list,tuple)) and command[0]=='get_property':
                if len(command)!=2:raise ValueError('get_property expects one property name')
                result=self.library.mpv_get_property_async(self.handle,identity,_text(key),NODE)
            elif isinstance(command,(list,tuple)) and command[0]=='set_property':
                if len(command)!=3:raise ValueError('set_property expects a property name and value')
                pending.owner=OwnedNode(command[2])
                result=self.library.mpv_set_property_async(self.handle,identity,_text(key),NODE,C.byref(pending.owner.node))
            else:result=self.library.mpv_command_node_async(self.handle,identity,C.byref(owner.node))
            self._check(result)
        except Exception as error:
            with self._pending_lock:self._pending.pop(identity,None)
            future.set_exception(error)
        return future
    request=command_async
    def command(self,command,timeout=1.):
        if self._cannot_wait():raise RuntimeError('Blocking libmpv commands are forbidden on render/event threads')
        return self.command_async(command,timeout).result(timeout+.15)
    def _expire(self):
        now=time.monotonic();expired=[]
        with self._pending_lock:
            for identity,pending in list(self._pending.items()):
                if now>=pending.deadline:expired.append((identity,self._pending.pop(identity)))
        for identity,pending in expired:
            self.library.mpv_abort_async_command(self.handle,identity)
            if not pending.future.done():pending.future.set_exception(TimeoutError(f'libmpv {pending.description} deadline expired'))
    def _pump(self):
        try:
            while not self._stop.is_set():
                pointer=self.library.mpv_wait_event(self.handle,.01)
                if pointer:self._handle_event(pointer.contents)
                self._expire()
        except Exception as error:
            with self._state_lock:self._event_error=str(error)
        finally:
            self._stop.set()
            with self._pending_lock:pending=list(self._pending.values());self._pending.clear()
            for item in pending:
                if not item.future.done():item.future.set_exception(RuntimeError('libmpv event pump stopped'))
    def _handle_event(self,event):
        identity=int(event.reply_userdata);kind=event.event_id
        if kind in (EVENT_GET,EVENT_SET,EVENT_COMMAND):
            with self._pending_lock:pending=self._pending.pop(identity,None)
            if pending is None:return  # A late reply cannot complete another request.
            if event.error<0:result_error=self.library.error(event.error);result=None
            else:
                result_error=None;result=None
                if event.data and kind==EVENT_COMMAND:result=node_value(C.cast(event.data,C.POINTER(EventCommand)).contents.result)
                elif event.data and kind==EVENT_GET:result=property_value(C.cast(event.data,C.POINTER(EventProperty)).contents)
                if kind==EVENT_GET and pending.property_name:
                    with self._state_lock:self._properties[pending.property_name]=copy.deepcopy(result);self._last_update=time.monotonic()
            if not pending.future.done():
                if result_error:pending.future.set_exception(result_error)
                else:pending.future.set_result(result)
        elif kind==EVENT_PROPERTY and event.data:
            prop=C.cast(event.data,C.POINTER(EventProperty)).contents
            key=(prop.name or b'').decode('utf-8','replace');result=property_value(prop)
            with self._state_lock:self._properties[key]=result;self._last_update=time.monotonic()
        elif kind==EVENT_START:
            with self._state_lock:
                self._start_generation+=1;self._loading=True
                for key in ('video-params','video-dec-params','video-out-params','hwdec-current'):self._properties.pop(key,None)
        elif kind==EVENT_LOADED:
            with self._state_lock:self._file_generation+=1;self._loading=False
        elif kind in (EVENT_SEEK,EVENT_RESTART):
            with self._state_lock:self._properties['seeking']=kind==EVENT_SEEK
        elif kind==EVENT_OVERFLOW:
            with self._state_lock:self._overflow=True
        elif kind==EVENT_SHUTDOWN:self._stop.set()
    def frame_state(self,render_generation=None):
        with self._state_lock:
            result=copy.deepcopy(self._properties)
            result.update(file_generation=self._file_generation,start_generation=self._start_generation,
                render_generation=self._render_generation if render_generation is None else render_generation,
                loading=self._loading,last_property_update=self._last_update,event_error=self._event_error,
                event_queue_overflow=self._overflow,observe_errors=dict(self._observe_errors))
        return result
    def create_context(self,device_ptr,wake_event=None,advanced_control=True):
        if self._destroyed:raise RuntimeError('libmpv core is closed')
        if self._contexts:raise RuntimeError('Only one render context is allowed per libmpv core')
        context=RenderContext(self,device_ptr,wake_event,advanced_control)
        self._contexts.add(context);self._render_threads.add(context.owner_thread)
        return context
    def close(self,timeout=2.):
        if self._destroyed:return
        if self._contexts:raise RuntimeError('Free render contexts on the render thread before closing the core')
        self._stop.set();self.library.mpv_wakeup(self.handle)
        if self._thread is not None:
            if threading.get_ident()==self._thread.ident:raise RuntimeError('Cannot close a core from its event thread')
            self._thread.join(timeout)
            if self._thread.is_alive():raise TimeoutError('libmpv event pump failed to stop')
        self.library.mpv_terminate_destroy(self.handle);self.handle=None;self._destroyed=True


class RenderContext:
    def __init__(self,core,device_ptr,wake_event=None,advanced_control=True):
        self.core=core;self.library=core.library;self.owner_thread=threading.get_ident()
        self.handle=C.c_void_p();self.dirty=threading.Event();self.wake_event=wake_event
        api=C.create_string_buffer(b'd3d11');init=D3D11InitParams(_void_pointer(device_ptr))
        advanced=C.c_int(bool(advanced_control))
        params=(RenderParam*4)(RenderParam(1,C.cast(api,C.c_void_p)),
            RenderParam(21,C.cast(C.byref(init),C.c_void_p)),
            RenderParam(10,C.cast(C.byref(advanced),C.c_void_p)),RenderParam(0,None))
        core._check(self.library.mpv_render_context_create(C.byref(self.handle),core.handle,params))
        @RenderUpdateCallback
        def callback(unused):
            self.dirty.set()
            if self.wake_event is not None:self.wake_event.set()
        self._callback=callback
        self.library.mpv_render_context_set_update_callback(self.handle,C.cast(callback,C.c_void_p),None)
    def _assert_owner(self):
        if threading.get_ident()!=self.owner_thread:raise RuntimeError('Use the creating render thread for this context')
        if not self.handle:raise RuntimeError('Render context is closed')
    def update(self):
        self._assert_owner();self.dirty.clear()
        return int(self.library.mpv_render_context_update(self.handle))
    def get_frame_info(self):
        self._assert_owner();info=RenderFrameInfo()
        self.core._check(self.library.mpv_render_context_get_info(self.handle,RenderParam(11,C.cast(C.byref(info),C.c_void_p))))
        return {'flags':int(info.flags),'target_time':int(info.target_time),'present':bool(info.flags&1),
                'redraw':bool(info.flags&2),'repeat':bool(info.flags&4),'block_vsync':bool(info.flags&8)}
    def render_to_texture(self,texture_ptr,block_for_target_time=False):
        self._assert_owner();fbo=D3D11FBO(_void_pointer(texture_ptr));block=C.c_int(bool(block_for_target_time))
        params=(RenderParam*3)(RenderParam(22,C.cast(C.byref(fbo),C.c_void_p)),
            RenderParam(12,C.cast(C.byref(block),C.c_void_p)),RenderParam(0,None))
        self.core._check(self.library.mpv_render_context_render(self.handle,params))
        with self.core._state_lock:self.core._render_generation+=1;generation=self.core._render_generation
        return generation
    def report_swap(self):self._assert_owner();self.library.mpv_render_context_report_swap(self.handle)
    def time_us(self):self._assert_owner();return int(self.library.mpv_get_time_us(self.core.handle))
    def time_ns(self):
        """API 2.6 frame target_time uses ns in the shipped build (not us)."""
        self._assert_owner();return int(self.library.mpv_get_time_ns(self.core.handle))
    def free_context(self):
        if not self.handle:return
        self._assert_owner();self.library.mpv_render_context_set_update_callback(self.handle,None,None)
        self.library.mpv_render_context_free(self.handle);self.handle=C.c_void_p()
        self.core._contexts.discard(self);self.core._render_threads.discard(self.owner_thread)
    close=free_context


MpvLibrary=Library
MpvCore=Core
