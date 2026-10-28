"""ABI, owned nodes, asynchronous deadlines and cached render-thread state."""
from __future__ import annotations
import ctypes as C
from pathlib import Path
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gpu_mpv_api as api


class FakeLibrary:
    def __init__(self):
        self.next_handle=100;self.events={};self.current={};self.calls=[];self.destroyed=[]
        self.properties={};self.options=[];self.aborted=[];self.render_calls=[]
    def mpv_create(self):
        self.next_handle+=1;self.events[self.next_handle]=queue.Queue();return self.next_handle
    def mpv_initialize(self,handle):return 0
    def mpv_set_option_string(self,handle,key,value):self.options.append((handle,key.decode(),value.decode()));return 0
    def mpv_observe_property(self,handle,identity,key,kind):return 0
    def error(self,code):return api.MpvError(code,'fixture error')
    def push(self,handle,event,*owners):self.events[handle].put((event,owners))
    def mpv_wait_event(self,handle,timeout):
        try:item=self.events[handle].get(timeout=timeout)
        except queue.Empty:item=(api.Event(),())
        self.current[handle]=item
        return C.pointer(item[0])
    def reply(self,handle,identity,value=None,error=0):
        owner=api.OwnedNode(value);payload=api.EventCommand(owner.node)
        self.push(handle,api.Event(api.EVENT_COMMAND,error,identity,C.addressof(payload)),owner,payload)
    def prop(self,handle,name,value,kind=api.EVENT_PROPERTY,identity=0):
        owner=api.OwnedNode(value);payload=api.EventProperty(name.encode(),api.NODE,C.addressof(owner.node))
        self.push(handle,api.Event(kind,0,identity,C.addressof(payload)),owner,payload)
    def mpv_command_node_async(self,handle,identity,node):
        value=api.node_value(C.cast(node,C.POINTER(api.Node)).contents);self.calls.append((handle,identity,value))
        if value[0]=='reject':return -12
        if value[0]=='never':return 0
        self.reply(handle,identity,value[1] if len(value)>1 else None,-7 if value[0]=='fail' else 0)
        return 0
    def mpv_get_property_async(self,handle,identity,name,kind):
        name=name.decode();self.calls.append((handle,identity,['get_property',name]))
        self.prop(handle,name,self.properties.get(name),api.EVENT_GET,identity);return 0
    def mpv_set_property_async(self,handle,identity,name,kind,node):
        name=name.decode();value=api.node_value(C.cast(node,C.POINTER(api.Node)).contents)
        self.calls.append((handle,identity,['set_property',name,value]));self.properties[name]=value
        self.push(handle,api.Event(api.EVENT_SET,0,identity,None));return 0
    def mpv_abort_async_command(self,handle,identity):self.aborted.append((handle,identity))
    def mpv_wakeup(self,handle):self.push(handle,api.Event())
    def mpv_terminate_destroy(self,handle):self.destroyed.append(handle)
    def mpv_render_context_create(self,result,handle,params):
        C.cast(result,C.POINTER(C.c_void_p)).contents.value=handle+1000
        self.render_calls.append(('create',threading.get_ident(),[p.type for p in params]));return 0
    def mpv_render_context_set_update_callback(self,handle,callback,context):
        if callback:C.cast(callback,api.RenderUpdateCallback)(None)
    def mpv_render_context_update(self,handle):return 1
    def mpv_render_context_get_info(self,handle,param):
        value=C.cast(param.data,C.POINTER(api.RenderFrameInfo)).contents;value.flags=1;value.target_time=123456;return 0
    def mpv_render_context_render(self,handle,params):
        fbo=C.cast(params[0].data,C.POINTER(api.D3D11FBO)).contents
        block=C.cast(params[1].data,C.POINTER(C.c_int)).contents.value
        self.render_calls.append(('render',fbo.tex,block));return 0
    def mpv_render_context_report_swap(self,handle):self.render_calls.append(('swap',handle))
    def mpv_render_context_free(self,handle):self.render_calls.append(('free',handle))
    def mpv_get_time_us(self,handle):return 123400
    def mpv_get_time_ns(self,handle):return 123400000


class NodeTests(unittest.TestCase):
    def test_x64_abi_sizes_and_alignment_match_bundled_headers(self):
        self.assertEqual(C.sizeof(C.c_void_p),8)
        self.assertEqual({kind.__name__:C.sizeof(kind) for kind in (api.Node,api.NodeList,api.ByteArray,
            api.Event,api.EventProperty,api.EventCommand,api.RenderParam,api.RenderFrameInfo)},
            {'Node':16,'NodeList':24,'ByteArray':16,'Event':24,'EventProperty':24,
             'EventCommand':16,'RenderParam':16,'RenderFrameInfo':16})
        self.assertEqual(api.Event.data.offset,16);self.assertEqual(api.Node.format.offset,8)
    def test_nested_typed_nodes_roundtrip_unicode_bools_ints_maps_and_binary(self):
        value=['set_property','data',{'中文':['x',True,False,None,-2**63,2**63-1,1.25],
                                    'empty':{},'array':[],'blob':b'a\0b'}]
        owner=api.OwnedNode(value)
        self.assertEqual(api.node_value(owner.node),value)
    def test_invalid_nodes_reject_nul_nonfinite_large_integer_and_bad_map_keys(self):
        for value in ('bad\0text',float('nan'),float('inf'),2**63,{1:'bad'},object()):
            with self.subTest(value=repr(value)),self.assertRaises((ValueError,TypeError)):
                api.OwnedNode(value)
    def test_native_property_values_are_copied_by_requested_format(self):
        values=((api.FLAG,C.c_int(1),True),(api.INT64,C.c_int64(123),123),
                (api.DOUBLE,C.c_double(1.5),1.5),(api.STRING,C.c_char_p('机位'.encode()),'机位'))
        for kind,scalar,expected in values:
            prop=api.EventProperty(b'value',kind,C.addressof(scalar))
            self.assertEqual(api.property_value(prop),expected)


class AsyncTests(unittest.TestCase):
    def setUp(self):
        self.lib=FakeLibrary();self.core=api.Core(self.lib)
        self.addCleanup(self.core.close)
    def wait(self,predicate):
        until=time.monotonic()+1
        while time.monotonic()<until:
            if predicate():return
            time.sleep(.002)
        self.fail('event pump condition timed out')
    def test_async_typed_commands_return_future_and_unique_request_ids(self):
        first=self.core.command_async(['echo',{'yes':True,'value':1.5}])
        second=self.core.command_async(['echo','second'])
        self.assertEqual(first.result(1),{'yes':True,'value':1.5});self.assertEqual(second.result(1),'second')
        self.assertNotEqual(self.lib.calls[0][1],self.lib.calls[1][1])
        options={name:value for handle,name,value in self.lib.options}
        self.assertEqual((options['vo'],options['hwdec'],options['hwdec-software-fallback'],options['audio']),
                         ('libmpv','d3d11va','no','no'))
        self.assertEqual((options['cache-on-disk'], options['demuxer-max-bytes'],
                          options['demuxer-max-back-bytes']), ('no', '32MiB', '8MiB'))

    def test_seek_completion_has_a_render_floor_separate_from_redraw(self):
        self.core._render_generation=7
        self.lib.push(self.core.handle,api.Event(api.EVENT_SEEK,0,0,None))
        self.wait(lambda:self.core.frame_state()['seek_generation']==1)
        state=self.core.frame_state()
        self.assertEqual((state['seek_render_floor'],state['restart_generation']), (7,0))
        self.assertTrue(state['seeking'])
        self.lib.push(self.core.handle,api.Event(api.EVENT_RESTART,0,0,None))
        self.wait(lambda:self.core.frame_state()['restart_generation']==1)
        self.assertFalse(self.core.frame_state()['seeking'])
    def test_get_set_property_use_dedicated_async_apis_and_cache_get_reply(self):
        self.assertIsNone(self.core.command(['set_property','pause',True]))
        self.assertTrue(self.core.command(['get_property','pause']))
        self.assertTrue(self.core.frame_state()['pause'])
        self.assertEqual(self.lib.calls[-1][2],['get_property','pause'])
    def test_immediate_and_event_errors_complete_futures_without_stopping_pump(self):
        for command in ('reject','fail'):
            with self.subTest(command=command),self.assertRaises(api.MpvError):
                self.core.command_async([command]).result(1)
        self.assertEqual(self.core.command(['echo','still alive']),'still alive')
    def test_deadline_expires_and_late_reply_cannot_complete_next_request(self):
        began=time.monotonic();future=self.core.command_async(['never'],timeout=.04)
        identity=self.lib.calls[-1][1]
        with self.assertRaises(TimeoutError):future.result(1)
        self.assertLess(time.monotonic()-began,.2)
        self.assertIn((self.core.handle,identity),self.lib.aborted)
        self.lib.reply(self.core.handle,identity,'late stale result')
        self.assertEqual(self.core.command(['echo','new result']),'new result')
    def test_property_events_and_file_generations_return_isolated_cached_snapshots(self):
        self.lib.prop(self.core.handle,'video-params',{'w':1920,'h':1080})
        self.lib.prop(self.core.handle,'path','source.mp4')
        self.lib.push(self.core.handle,api.Event(api.EVENT_LOADED,0,0,None))
        self.wait(lambda:self.core.frame_state()['file_generation']==1)
        first=self.core.frame_state(render_generation=9);first['video-params']['w']=1
        self.assertEqual(self.core.frame_state()['video-params']['w'],1920)
        self.assertEqual(first['render_generation'],9)
        self.lib.push(self.core.handle,api.Event(api.EVENT_START,0,0,None))
        self.wait(lambda:self.core.frame_state()['loading'])
        self.assertNotIn('video-params',self.core.frame_state())
        self.assertEqual(self.core.frame_state()['file_generation'],1)
    def test_two_cores_have_independent_request_and_property_state(self):
        other=api.Core(self.lib,name='second');self.addCleanup(other.close)
        self.lib.prop(self.core.handle,'time-pos',1.5);self.lib.prop(other.handle,'time-pos',3.)
        self.wait(lambda:'time-pos' in other.frame_state() and 'time-pos' in self.core.frame_state())
        self.assertEqual(self.core.frame_state()['time-pos'],1.5);self.assertEqual(other.frame_state()['time-pos'],3.)
    def test_close_fails_pending_requests_and_reaps_event_pump(self):
        future=self.core.command_async(['never'],timeout=2)
        handle=self.core.handle;self.core.close()
        with self.assertRaises(RuntimeError):future.result(.1)
        self.assertFalse(self.core._thread.is_alive());self.assertIn(handle,self.lib.destroyed)
    def test_render_context_only_uses_render_api_and_blocks_ordinary_waits(self):
        context=self.core.create_context(C.c_void_p(0x1234));self.addCleanup(context.free_context)
        self.assertTrue(context.dirty.is_set());self.assertEqual(context.update(),1)
        self.assertTrue(context.get_frame_info()['present']);self.assertEqual(context.time_us(),123400)
        self.assertEqual(context.time_ns(),123400000)
        self.assertEqual(context.render_to_texture(C.c_void_p(0x5678)),1)
        context.report_swap();self.assertEqual(self.core.frame_state()['render_generation'],1)
        with self.assertRaises(RuntimeError):self.core.command(['echo','forbidden'])
        pending=self.core.command_async(['never'],timeout=.03)
        with self.assertRaises(RuntimeError):pending.result(.1)
        with self.assertRaises(RuntimeError):self.core.close()
        self.assertIn(('render',0x5678,0),self.lib.render_calls)
        context.free_context()
    def test_render_context_rejects_other_threads(self):
        context=self.core.create_context(0x1234);self.addCleanup(context.free_context)
        errors=[]
        def foreign():
            try:context.update()
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=foreign);thread.start();thread.join()
        self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],RuntimeError)


if __name__=='__main__':unittest.main()
