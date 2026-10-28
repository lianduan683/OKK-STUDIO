"""D3D11 texture compositor through documented Windows COM APIs.

Decoded video stays in GPU textures. CPU readback exists only as an explicit
diagnostic method; normal presentation never calls it. No helper EXE is used.
"""
from __future__ import annotations

import ctypes as C
from functools import lru_cache
from ctypes import wintypes as W
import os
from pathlib import Path
import struct
import uuid

P=C.c_void_p; U=C.c_uint; F=C.c_float; HR=C.c_long


class GUID(C.Structure):
    _fields_=[('bytes',C.c_ubyte*16)]
    def __init__(self,value):super().__init__((C.c_ubyte*16).from_buffer_copy(uuid.UUID(value).bytes_le))

class Rational(C.Structure):_fields_=[('num',U),('den',U)]
class Sample(C.Structure):_fields_=[('count',U),('quality',U)]
class Mode(C.Structure):
    _fields_=[('w',U),('h',U),('rate',Rational),('fmt',U),('scan',U),('scale',U)]
class SwapDesc(C.Structure):
    _fields_=[('mode',Mode),('sample',Sample),('usage',U),('count',U),('window',P),('windowed',W.BOOL),('effect',U),('flags',U)]
class AdapterDesc(C.Structure):
    _fields_=[('description',W.WCHAR*128),('vendor',U),('device',U),('subsys',U),('revision',U),
              ('dedicated',C.c_size_t),('system',C.c_size_t),('shared',C.c_size_t),('luid',C.c_longlong),('flags',U)]
class TexDesc(C.Structure):
    _fields_=[('w',U),('h',U),('mips',U),('arrays',U),('fmt',U),('sample',Sample),('usage',U),('bind',U),('cpu',U),('misc',U)]
class BufferDesc(C.Structure):
    _fields_=[('bytes',U),('usage',U),('bind',U),('cpu',U),('misc',U),('stride',U)]
class SamplerDesc(C.Structure):
    _fields_=[('filter',U),('u',U),('v',U),('w',U),('bias',F),('anisotropy',U),('comparison',U),
              ('border',F*4),('min_lod',F),('max_lod',F)]
class RasterDesc(C.Structure):
    _fields_=[('fill',U),('cull',U),('counterclockwise',W.BOOL),('bias',C.c_int),('bias_clamp',F),('slope',F),
              ('depth_clip',W.BOOL),('scissor',W.BOOL),('multisample',W.BOOL),('antialias',W.BOOL)]
class Viewport(C.Structure):
    _fields_=[('x',F),('y',F),('w',F),('h',F),('min_z',F),('max_z',F)]
class Mapped(C.Structure):_fields_=[('data',P),('row',U),('depth',U)]
class Constants(C.Structure):_fields_=[('transition',F*4),('grades',F*1224)]

_methods={}
def call(obj,index,restype,argtypes,*args):
    if not obj:raise RuntimeError('D3D11 COM object is unavailable')
    table=C.cast(obj,C.POINTER(C.POINTER(P))).contents
    address=table[index]
    key=(address,restype,tuple(argtypes))
    function=_methods.get(key)
    if function is None:
        function=C.WINFUNCTYPE(restype,P,*argtypes)(address);_methods[key]=function
    return function(obj,*args)

def check(result,label):
    if result<0:raise RuntimeError(f'{label} failed: 0x{result&0xffffffff:08x}')
    return result

class Com:
    def __init__(self,value=None):self.value=P(value) if isinstance(value,int) else value or P()
    def release(self):
        if self.value and self.value.value:
            call(self.value,2,C.c_ulong,[]);self.value=P()
    def __del__(self):
        try:self.release()
        except Exception:pass

class VideoTexture:
    def __init__(self,device,width,height):
        self.texture=Com();self.srv=Com();self.rtv=Com()
        desc=TexDesc(width,height,1,1,10,Sample(1,0),0,0x28,0,0)
        check(call(device,5,HR,[C.POINTER(TexDesc),P,C.POINTER(P)],C.byref(desc),None,C.byref(self.texture.value)),'Create video texture')
        check(call(device,7,HR,[P,P,C.POINTER(P)],self.texture.value,None,C.byref(self.srv.value)),'Create video SRV')
        check(call(device,9,HR,[P,P,C.POINTER(P)],self.texture.value,None,C.byref(self.rtv.value)),'Create video RTV')
    def close(self):
        self.srv.release();self.rtv.release();self.texture.release()


def _adapter_matches(vendor, name, preference):
    if preference is None:return True
    known={'nvidia':0x10de,'amd':0x1002,'intel':0x8086}
    requested=preference.strip().lower()
    return vendor==known[requested] if requested in known else requested in name.lower()


def _adapter_priority(dedicated, index):
    """Prefer a discrete/high-memory device without excluding any vendor."""
    return max(0,int(dedicated)), -int(index)


def _create_factory():
    owner=Com();create=C.WinDLL('dxgi.dll').CreateDXGIFactory1
    create.argtypes=[C.POINTER(GUID),C.POINTER(P)];create.restype=HR
    iid=GUID('770aae78-f26f-4dba-a829-253c83d1b387')
    check(create(C.byref(iid),C.byref(owner.value)),'CreateDXGIFactory1')
    return owner


def _hardware_candidates(factory, preference=None):
    candidates=[]
    for index in range(16):
        pointer=P();result=call(factory,12,HR,[U,C.POINTER(P)],index,C.byref(pointer))
        if result<0:break
        adapter=Com(pointer);desc=AdapterDesc()
        if (call(pointer,10,HR,[C.POINTER(AdapterDesc)],C.byref(desc))>=0 and not desc.flags&2
                and _adapter_matches(desc.vendor,str(desc.description),preference)):
            candidates.append((_adapter_priority(desc.dedicated,index),adapter,str(desc.description)))
        else:adapter.release()
    candidates.sort(key=lambda x:x[0],reverse=True)
    return candidates


@lru_cache(maxsize=1)
def preferred_adapter_name():
    """One fast DXGI query per process; no shell or vendor-specific utility."""
    if os.name!='nt':return ''
    factory=None;candidates=[]
    try:
        factory=_create_factory()
        candidates=_hardware_candidates(factory.value)
        return candidates[0][2] if candidates else ''
    except (OSError,RuntimeError,AttributeError):
        return ''  # MPV's own adapter selection remains a valid fallback.
    finally:
        for _,adapter,_ in candidates:adapter.release()
        if factory is not None:factory.release()


class D3D11Compositor:
    """Owned by one render thread, with two source textures and one output."""
    def __init__(self,runtime,parent_hwnd=None,width=640,height=360,*,adapter_preference=None):
        if os.name!='nt':raise RuntimeError('GPU realtime rendering requires Windows D3D11')
        if adapter_preference is not None and (not isinstance(adapter_preference,str) or not adapter_preference.strip()):
            raise ValueError('GPU adapter preference must be a non-empty name or vendor')
        self.adapter_preference=adapter_preference
        self.runtime=Path(runtime);self.parent=int(parent_hwnd or 0);self.window=0;self._closed=False
        self._wndproc=None;self._class_name=None
        for name in ('factory','adapter','device','context','swapchain','target','rtv','vertex','pixel','sampler','raster','constant'):
            setattr(self,name,Com())
        self.videos=[];self.width=0;self.height=0;self.adapter_name=''
        try:
            self._device()
            if self.parent:self._window()
            self._shaders()
            self.resize(width,height)
        except Exception:self.close();raise

    def _device(self):
        self.factory=_create_factory()
        candidates=_hardware_candidates(self.factory.value,self.adapter_preference)
        if not candidates:raise RuntimeError('No matching hardware D3D11 adapter is available')
        create=C.WinDLL('d3d11.dll').D3D11CreateDevice
        create.argtypes=[P,U,P,U,C.POINTER(U),U,U,C.POINTER(P),C.POINTER(U),C.POINTER(P)]
        create.restype=HR
        last_error=None
        for _,adapter,name in candidates:
            self.adapter,self.adapter_name=adapter,name
            levels=(U*2)(0xb100,0xb000);level=U()
            result=create(adapter.value,0,None,0x20|0x800,levels,2,7,C.byref(self.device.value),C.byref(level),C.byref(self.context.value))
            if result<0 and result&0xffffffff==0x80070057:
                # Some older Intel/AMD D3D runtimes reject an unknown 11.1
                # entry instead of selecting the supported 11.0 level.
                self.context.release();self.device.release()
                levels=(U*1)(0xb000)
                result=create(adapter.value,0,None,0x20|0x800,levels,1,7,C.byref(self.device.value),C.byref(level),C.byref(self.context.value))
            if result>=0:
                for _,other,_ in candidates:
                    if other is not adapter:other.release()
                return
            last_error=f'{name}: 0x{result&0xffffffff:08x}'
            self.context.release();self.device.release();adapter.release()
        raise RuntimeError('No usable hardware D3D11 adapter: '+str(last_error))

    def _window(self):
        user=C.WinDLL('user32',use_last_error=True);kernel=C.WinDLL('kernel32')
        proc_type=C.WINFUNCTYPE(C.c_ssize_t,W.HWND,U,W.WPARAM,W.LPARAM)
        class WC(C.Structure):
            _fields_=[('style',U),('proc',proc_type),('cb_class',C.c_int),('cb_window',C.c_int),('instance',P),
                      ('icon',P),('cursor',P),('brush',P),('menu',W.LPCWSTR),('name',W.LPCWSTR)]
        user.DefWindowProcW.argtypes=[W.HWND,U,W.WPARAM,W.LPARAM];user.DefWindowProcW.restype=C.c_ssize_t
        def procedure(window,message,wparam,lparam):return user.DefWindowProcW(window,message,wparam,lparam)
        self._wndproc=proc_type(procedure);self._class_name='OKK_GPU_'+uuid.uuid4().hex
        kernel.GetModuleHandleW.argtypes=[W.LPCWSTR];kernel.GetModuleHandleW.restype=P
        self._instance=kernel.GetModuleHandleW(None)
        wc=WC(0,self._wndproc,0,0,self._instance,None,None,None,None,self._class_name)
        user.RegisterClassW.argtypes=[C.POINTER(WC)];user.RegisterClassW.restype=W.ATOM
        if not user.RegisterClassW(C.byref(wc)):raise C.WinError(C.get_last_error())
        user.CreateWindowExW.argtypes=[U,W.LPCWSTR,W.LPCWSTR,U,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,P,P,P]
        user.CreateWindowExW.restype=W.HWND
        self.window=user.CreateWindowExW(0,self._class_name,'OKK GPU output',0x40000000|0x10000000|0x04000000,
                                       0,0,16,16,self.parent,None,self._instance,None)
        if not self.window:raise C.WinError(C.get_last_error())

    def _shaders(self):
        for name,index,filename in (('vertex',12,'vertex.cso'),('pixel',15,'composite.cso')):
            code=(self.runtime/filename).read_bytes();blob=C.create_string_buffer(code)
            check(call(self.device.value,index,HR,[P,C.c_size_t,P,C.POINTER(P)],blob,len(code),None,
                       C.byref(getattr(self,name).value)),'Create '+name+' shader')
        desc=BufferDesc(C.sizeof(Constants),2,4,0x10000,0,0)
        check(call(self.device.value,3,HR,[C.POINTER(BufferDesc),P,C.POINTER(P)],C.byref(desc),None,C.byref(self.constant.value)),'Create constant buffer')
        sampler=SamplerDesc(0x15,3,3,3,0.,1,8,(F*4)(0,0,0,0),0.,3.4e38)
        check(call(self.device.value,23,HR,[C.POINTER(SamplerDesc),C.POINTER(P)],C.byref(sampler),C.byref(self.sampler.value)),'Create sampler')
        raster=RasterDesc(3,1,False,0,0,0,True,False,False,False)
        check(call(self.device.value,22,HR,[C.POINTER(RasterDesc),C.POINTER(P)],C.byref(raster),C.byref(self.raster.value)),'Create rasterizer')

    def client_size(self):
        if not self.parent:return self.width,self.height
        rectangle=W.RECT();user=C.WinDLL('user32');user.GetClientRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
        if not user.GetClientRect(self.parent,C.byref(rectangle)):return self.width,self.height
        return max(16,rectangle.right),max(16,rectangle.bottom)

    def set_visible(self,visible):
        # Executed on this compositor's owning render thread; never recreates
        # the swapchain/window or steals focus from a floating panel.
        if self.window:
            user=C.WinDLL('user32');user.ShowWindow.argtypes=[W.HWND,C.c_int]
            user.ShowWindow(self.window,8 if visible else 0)  # SW_SHOWNA / SW_HIDE

    def resize(self,width,height):
        width=max(16,min(8192,int(width)));height=max(16,min(8192,int(height)))
        if (width,height)==(self.width,self.height):return False
        call(self.context.value,110,None,[])
        self.rtv.release();self.target.release()
        for texture in self.videos:texture.close()
        self.videos=[]
        if self.window:
            user=C.WinDLL('user32');user.SetWindowPos.argtypes=[W.HWND,W.HWND,C.c_int,C.c_int,C.c_int,C.c_int,U]
            user.SetWindowPos(self.window,0,0,0,width,height,0x0010|0x0004)
            if not self.swapchain.value.value:
                desc=SwapDesc(Mode(width,height,Rational(0,1),87,0,0),Sample(1,0),0x20,2,self.window,True,4,0)
                check(call(self.factory.value,10,HR,[P,C.POINTER(SwapDesc),C.POINTER(P)],self.device.value,C.byref(desc),C.byref(self.swapchain.value)),'Create swap chain')
            else:check(call(self.swapchain.value,13,HR,[U,U,U,U,U],0,width,height,0,0),'Resize swap chain')
            iid=GUID('6f15aaf2-d208-4e89-9ab4-489535d34f9c')
            check(call(self.swapchain.value,9,HR,[U,C.POINTER(GUID),C.POINTER(P)],0,C.byref(iid),C.byref(self.target.value)),'Get swap buffer')
        else:
            desc=TexDesc(width,height,1,1,87,Sample(1,0),0,0x28,0,0)
            check(call(self.device.value,5,HR,[C.POINTER(TexDesc),P,C.POINTER(P)],C.byref(desc),None,C.byref(self.target.value)),'Create offscreen target')
        check(call(self.device.value,9,HR,[P,P,C.POINTER(P)],self.target.value,None,C.byref(self.rtv.value)),'Create output RTV')
        self.videos=[VideoTexture(self.device.value,width,height) for _ in range(2)]
        for texture in self.videos:self.clear_texture(texture,(0,0,0,1))
        self.width,self.height=width,height
        return True

    def clear_texture(self,texture,color):
        values=(F*4)(*color)
        call(self.context.value,50,None,[P,C.POINTER(F)],texture.rtv.value,values)

    def clear_output(self):
        values=(F*4)(0.,0.,0.,1.)
        call(self.context.value,50,None,[P,C.POINTER(F)],self.rtv.value,values)

    def draw(self,front=0,other=1,kind=-1,progress=0.,grade_a=None,grade_b=None):
        constants=Constants();constants.transition[:]=(float(kind),float(progress),self.width,self.height)
        if grade_a is not None:
            if len(grade_a)!=612:raise ValueError('GPU grade A must contain 612 controls')
            constants.grades[:612]=grade_a
        if grade_b is not None:
            if len(grade_b)!=612:raise ValueError('GPU grade B must contain 612 controls')
            constants.grades[612:]=grade_b
        mapped=Mapped()
        check(call(self.context.value,14,HR,[P,U,U,U,C.POINTER(Mapped)],self.constant.value,0,4,0,C.byref(mapped)),'Map shader constants')
        C.memmove(mapped.data,C.byref(constants),C.sizeof(constants))
        call(self.context.value,15,None,[P,U],self.constant.value,0)
        context=self.context.value
        call(context,110,None,[])
        target=(P*1)(self.rtv.value)
        call(context,33,None,[U,C.POINTER(P),P],1,target,None)
        viewport=Viewport(0,0,self.width,self.height,0,1)
        call(context,44,None,[U,C.POINTER(Viewport)],1,C.byref(viewport))
        call(context,43,None,[P],self.raster.value)
        call(context,17,None,[P],None)
        call(context,24,None,[U],4)
        call(context,11,None,[P,P,U],self.vertex.value,None,0)
        call(context,9,None,[P,P,U],self.pixel.value,None,0)
        textures=(P*2)(self.videos[front].srv.value,self.videos[other].srv.value)
        call(context,8,None,[U,U,C.POINTER(P)],0,2,textures)
        samplers=(P*1)(self.sampler.value)
        call(context,10,None,[U,U,C.POINTER(P)],0,1,samplers)
        buffers=(P*1)(self.constant.value)
        call(context,16,None,[U,U,C.POINTER(P)],0,1,buffers)
        call(context,13,None,[U,U],3,0)
        # mpv may render into these resources on the next frame.
        nulls=(P*2)()
        call(context,8,None,[U,U,C.POINTER(P)],0,2,nulls)

    def present(self):
        if self.swapchain.value.value:check(call(self.swapchain.value,8,HR,[U,U],1,0),'Present')
        else:call(self.context.value,111,None,[])

    def diagnostic_pixels(self):
        """Explicit test-only GPU readback, never used by normal presentation."""
        staging=Com();desc=TexDesc(self.width,self.height,1,1,87,Sample(1,0),3,0,0x20000,0)
        try:
            check(call(self.device.value,5,HR,[C.POINTER(TexDesc),P,C.POINTER(P)],C.byref(desc),None,C.byref(staging.value)),'Create diagnostic staging texture')
            call(self.context.value,47,None,[P,P],staging.value,self.target.value)
            mapped=Mapped()
            check(call(self.context.value,14,HR,[P,U,U,U,C.POINTER(Mapped)],staging.value,0,1,0,C.byref(mapped)),'Read diagnostic texture')
            try:return b''.join(C.string_at(mapped.data+y*mapped.row,self.width*4) for y in range(self.height))
            finally:call(self.context.value,15,None,[P,U],staging.value,0)
        finally:staging.release()

    def diagnostic_bmp(self,path):
        pixels=self.diagnostic_pixels();path=Path(path)
        with path.open('xb') as output:
            output.write(struct.pack('<2sIHHI',b'BM',54+len(pixels),0,0,54))
            output.write(struct.pack('<IiiHHIIiiII',40,self.width,-self.height,1,32,0,len(pixels),0,0,0,0))
            output.write(pixels)
        return str(path)

    def pump_messages(self):
        if not self.window:return
        class MSG(C.Structure):
            _fields_=[('hwnd',W.HWND),('message',U),('wparam',W.WPARAM),('lparam',W.LPARAM),('time',W.DWORD),('point',W.POINT),('private',W.DWORD)]
        user=C.WinDLL('user32');user.PeekMessageW.argtypes=[C.POINTER(MSG),W.HWND,U,U,U]
        user.TranslateMessage.argtypes=[C.POINTER(MSG)];user.DispatchMessageW.argtypes=[C.POINTER(MSG)]
        user.DispatchMessageW.restype=C.c_ssize_t
        message=MSG()
        while user.PeekMessageW(C.byref(message),None,0,0,1):
            user.TranslateMessage(C.byref(message));user.DispatchMessageW(C.byref(message))

    def close(self):
        if self._closed:return
        self._closed=True
        if self.context.value.value:
            try:call(self.context.value,110,None,[]);call(self.context.value,111,None,[])
            except Exception:pass
        for texture in self.videos:texture.close()
        self.videos=[]
        for name in ('rtv','target','constant','raster','sampler','pixel','vertex','swapchain','context','device','adapter','factory'):
            value=getattr(self,name,None)
            if value:value.release()
        if self.window:
            user=C.WinDLL('user32');user.DestroyWindow.argtypes=[W.HWND]
            user.DestroyWindow(self.window);self.window=0
        if self._class_name:
            user=C.WinDLL('user32');user.UnregisterClassW.argtypes=[W.LPCWSTR,P]
            user.UnregisterClassW(self._class_name,self._instance);self._class_name=None
