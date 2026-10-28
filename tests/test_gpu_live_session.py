"""Render-loop contracts without a GPU or a client command on render thread."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gpu_live_session import GpuLiveSession
from cache_paths import cache_dir


class FakeCore:
    def __init__(self):
        self.state={'render_generation': 1, 'time-pos': 0., 'hwdec-current': 'd3d11va'}
        self.context=None; self.closed=False
    def frame_state(self): return dict(self.state)
    def create_context(self, device, wake): return self.context
    def close(self): self.closed=True


class FakeContext:
    def __init__(self, core, fixture):
        self.core=core; self.fixture=fixture; self.updated=False; self.rendered_at=[]
        self.free=False; self.swaps=0
    def update(self):
        if not self.updated: self.updated=True; return 1
        return 0
    def get_frame_info(self):
        return {'present':True,'redraw':False,'target_time':101_000_000_000}
    def time_ns(self): return 100_000_000_000+self.fixture['presents']*1_000_000_000
    def time_us(self): raise AssertionError('Frame target time must use nanoseconds')
    def render_to_texture(self, pointer):
        self.rendered_at.append(self.time_ns()); self.core.state['render_generation']+=1
    def report_swap(self): self.swaps+=1
    def free_context(self): self.free=True


class FakeGraphics:
    def __init__(self, fixture):
        self.fixture=fixture; self.adapter_name='fake hardware'; self.window=0; self.closed=False
        class Pointer:
            def __init__(self, value): self.value=value
        self.device=Pointer(Pointer(100))
        class Texture:
            texture=Pointer(Pointer(200))
        self.videos=[Texture(),Texture()]
        self.draws=[]; self.clears=0;self.visibility=[]
    def client_size(self): return (320,180)
    def pump_messages(self): pass
    def set_visible(self,visible):self.visibility.append(visible)
    def resize(self,*args): return False
    def draw(self,*args): self.draws.append(args)
    def clear_output(self): self.clears+=1
    def present(self):
        self.fixture['presents']+=1
        if self.fixture['presents']>=3: self.fixture['session']._stop.set()
    def close(self): self.closed=True


class SessionLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='session_unit_',dir=cache_dir('temporary'))
        self.runtime=Path(self.temporary.name)
        for name in ('libmpv-2.dll','vertex.cso','composite.cso'): (self.runtime/name).touch()
        (self.runtime/'shader_layout.json').write_text(json.dumps({'side_uniforms':[str(n) for n in range(612)]}))
        self.session=GpuLiveSession(0,runtime=self.runtime,width=320,height=180)
        self.fixture={'presents':0,'session':self.session}
        self.graphics=FakeGraphics(self.fixture)
        self.cores=[FakeCore(),FakeCore()]
        for core in self.cores: core.context=FakeContext(core,self.fixture)
        self.session._cores=dict(zip(('a','b'),self.cores))
    def tearDown(self):
        self.session.close(); self.temporary.cleanup()
    def run_loop(self):
        with patch('gpu_live_session.D3D11Compositor',return_value=self.graphics): self.session._run()
        self.assertIsNone(self.session._error)
    def test_frame_deadline_uses_nanoseconds_and_pending_survives_no_new_update(self):
        self.run_loop()
        for core in self.cores:
            self.assertEqual(core.context.rendered_at,[101_000_000_000])
            self.assertTrue(core.context.free)
        self.assertEqual(self.fixture['presents'],3)
        self.assertTrue(self.graphics.closed)
    def test_blank_composition_clears_output_without_sampling_stale_video(self):
        self.session.compose(blank=True)
        self.run_loop()
        self.assertEqual(self.graphics.clears,3)
        self.assertEqual(self.graphics.draws,[])
    def test_regular_composition_draws_both_selected_slots(self):
        self.session.compose(front_slot='b',other_slot='a',active=True,kind='wipeleft',progress=.4)
        self.run_loop()
        self.assertEqual(len(self.graphics.draws),3)
        self.assertEqual(self.graphics.draws[0][:4],(1,0,2,.4))
        self.assertEqual(self.graphics.clears,0)
        self.assertEqual(self.session._presented_render_generations,{'a':2,'b':2})

    def test_hidden_compositor_does_no_draw_and_resumes_same_contexts(self):
        self.session.set_visible(False);pumps=[]
        def pump():
            pumps.append(self.fixture['presents'])
            if len(pumps)<=3:self.assertEqual(self.graphics.draws,[])
            if len(pumps)==3:self.session.set_visible(True)
        with patch.object(self.graphics,'pump_messages',side_effect=pump):self.run_loop()
        self.assertEqual(self.graphics.visibility,[False,True])
        self.assertEqual(pumps[:3],[0,0,0])
        self.assertEqual(self.fixture['presents'],3)
        self.assertEqual(self.session._cores,dict(zip(('a','b'),self.cores)))
        with self.assertRaises(ValueError):self.session.set_visible(1)

    def test_close_releases_other_core_when_one_fails_and_can_retry(self):
        broken=self.cores[0]
        with patch.object(broken,'close',side_effect=RuntimeError('temporary core close failure')):
            with self.assertRaisesRegex(RuntimeError,'temporary core'):
                self.session.close()
        self.assertTrue(self.cores[1].closed)
        self.assertEqual(list(self.session._cores),['a'])
        self.session.close()
        self.assertTrue(broken.closed)
        self.assertEqual(self.session._cores,{})
    def test_no_readback_in_normal_session(self):
        with self.assertRaisesRegex(RuntimeError,'readback'): self.session.screenshot('unused.bmp')
        self.run_loop()
        self.assertEqual(self.session._readbacks,0)
    def test_explicit_vendor_uses_one_shared_device_for_both_libmpv_contexts(self):
        self.session.adapter_preference='amd';devices=[]
        for core in self.cores:
            context=core.context
            core.create_context=lambda device,wake,context=context:(devices.append(device),context)[1]
        with patch('gpu_live_session.D3D11Compositor',return_value=self.graphics) as factory:
            self.session._run()
        self.assertIsNone(self.session._error)
        self.assertEqual(devices,[100,100])
        factory.assert_called_once_with(self.runtime,0,320,180,adapter_preference='amd')
    def test_render_failure_frees_contexts_and_compositor(self):
        with patch.object(self.graphics,'draw',side_effect=RuntimeError('device lost')):
            with patch('gpu_live_session.D3D11Compositor',return_value=self.graphics): self.session._run()
        self.assertIn('device lost',self.session._error)
        self.assertTrue(all(core.context.free for core in self.cores))
        self.assertTrue(self.graphics.closed)


if __name__=='__main__': unittest.main(verbosity=2)
