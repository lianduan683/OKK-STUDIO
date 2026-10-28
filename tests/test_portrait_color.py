"""Selective portrait brightening: safety, persistence, CPU pixels and GPU parity."""
from __future__ import annotations

from array import array
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))

from creative_core import (ColorGrade,SKIN_RANGES,grade_filter,grade_is_identity,
                           grade_from_dict,grade_to_dict,validate_grade)
from portrait_color import portrait_rgb,skin_mask,portrait_lut_path
from realtime_grade import ALL_CONTROLS,RealtimeGrade,grade_uniforms,shader_source
from exporter_core import CREATE_NO_WINDOW


def run(command,**kwargs):
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          creationflags=CREATE_NO_WINDOW,timeout=40,**kwargs)
    if result.returncode: raise RuntimeError(result.stderr.decode('utf-8','replace')[-3000:])
    return result.stdout


class PortraitModelTests(unittest.TestCase):
    def test_old_grade_defaults_neutral_and_new_controls_round_trip(self):
        original=grade_from_dict({'exposure':.25})
        self.assertEqual((original.skin_lighten,original.skin_saturation),(0.,0.))
        self.assertTrue(grade_is_identity(ColorGrade()))
        grade=ColorGrade(exposure=.25,skin_lighten=.45,skin_saturation=.22)
        self.assertEqual(grade_from_dict(grade_to_dict(grade)),grade)
        self.assertFalse(grade_is_identity(grade))
        self.assertTrue(grade_is_identity(replace(grade,enabled=False)))
        for name in SKIN_RANGES:
            for value in (-.1,1.1,True,float('nan'),float('inf')):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    validate_grade(ColorGrade(**{name:value}))

    def test_colour_selection_protects_neutrals_green_blue_and_saturated_red(self):
        self.assertGreater(skin_mask(.7,.5,.4),.9)
        self.assertGreater(skin_mask(.3,.17,.12),.9)
        for rgb in ((.5,.5,.5),(.1,.7,.2),(.1,.2,.8),(.95,.05,.05)):
            self.assertEqual(portrait_rgb(rgb,.45,.22),rgb)
        for rgb in ((.7,.5,.4),(.3,.17,.12)):
            changed=portrait_rgb(rgb,.45,.22)
            self.assertGreater(sum(changed),sum(rgb))
            self.assertLess(max(changed)-min(changed),max(rgb)-min(rgb))

    def test_highlights_are_protected_and_all_outputs_remain_bounded(self):
        bright=(.96,.92,.90)
        self.assertLess(max(abs(a-b) for a,b in zip(bright,portrait_rgb(bright,.45,.22))),.001)
        for r in range(9):
            for g in range(9):
                for b in range(9):
                    self.assertTrue(all(0<=v<=1 for v in portrait_rgb((r/8,g/8,b/8),1,1)))

    def test_skin_uniforms_are_dynamic_and_disposed_controller_is_forgotten(self):
        values,_=grade_uniforms([ColorGrade(skin_lighten=.45,skin_saturation=.22)])
        self.assertEqual(values['g0_skin_lighten'],.45)
        self.assertEqual(values['g1_skin_lighten'],0)
        self.assertIn('skin_lighten',ALL_CONTROLS)
        self.assertIn('//!PARAM g0_skin_lighten\n//!TYPE DYNAMIC float',shader_source())
        class Controller:
            def __init__(self): self.calls=[]
            def command(self,payload): self.calls.append(payload)
        engine=RealtimeGrade();self.addCleanup(engine.close);controller=Controller()
        engine.apply(controller,[ColorGrade(skin_lighten=.3)])
        engine.apply(controller,[ColorGrade(skin_lighten=.4)])
        self.assertEqual(sum(command[0]=='change-list' for command in controller.calls),1)
        self.assertIn(id(controller),engine._states)
        engine.forget(controller)
        self.assertNotIn(id(controller),engine._states)


class PortraitCpuPixels(unittest.TestCase):
    ffmpeg=APP/'bin'/'ffmpeg.exe'

    @classmethod
    def setUpClass(cls):
        if not cls.ffmpeg.is_file(): raise unittest.SkipTest('bundled FFmpeg unavailable')

    def test_actual_rgb_pixels_brighten_skin_and_protect_the_background(self):
        pixels=[(179,128,102),(77,43,31),(26,179,51),(26,51,204),(128,128,128),(245,235,230)]
        # At least 2 rows avoids RGB conversion constraints in some builds.
        source=bytes(value for rgb in pixels for value in rgb)*2
        grade=ColorGrade(skin_lighten=.45,skin_saturation=.22)
        raw=run([str(self.ffmpeg),'-v','error','-f','rawvideo','-pix_fmt','rgb24',
                 '-s','6x2','-i','pipe:0','-frames:v','1','-vf',grade_filter(grade),
                 '-pix_fmt','rgb24','-f','rawvideo','pipe:1'],input=source)
        for index,rgb in enumerate(pixels):
            actual=tuple(raw[index*3:index*3+3])
            expected=portrait_rgb(tuple(value/255 for value in rgb),.45,.22)
            self.assertLessEqual(max(abs(a-b*255) for a,b in zip(actual,expected)),2)
            if index<2: self.assertGreater(sum(actual),sum(rgb))
            elif index<5: self.assertLessEqual(max(abs(a-b) for a,b in zip(actual,rgb)),1)
            else: self.assertLessEqual(max(abs(a-b) for a,b in zip(actual,rgb)),1)

    def test_sixteen_bit_precision_and_lut_cache_reuse(self):
        width,height=512,2
        red=[round((.4+.2*index/(width-1))*65535) for index in range(width)]*height
        green=[round(value*.7) for value in red];blue=[round(value*.55) for value in red]
        packed=array('H',green+blue+red).tobytes()
        grade=ColorGrade(skin_lighten=.25,skin_saturation=.12)
        path=Path(portrait_lut_path(.25,.12));stamp=path.stat().st_mtime_ns
        self.assertEqual(Path(portrait_lut_path(.25,.12)).stat().st_mtime_ns,stamp)
        command=[str(self.ffmpeg),'-v','error','-f','rawvideo','-pix_fmt','gbrp16le',
                 '-s',f'{width}x{height}','-i','pipe:0','-frames:v','1','-vf',grade_filter(grade),
                 '-pix_fmt','gbrp16le','-f','rawvideo','pipe:1']
        actual=array('H');actual.frombytes(run(command,input=packed));count=width*height
        self.assertGreater(len(set(actual[count*2:])),256)
        for index in range(0,width,31):
            expected=portrait_rgb((red[index]/65535,green[index]/65535,blue[index]/65535),.25,.12)
            pixel=(actual[count*2+index],actual[index],actual[count+index])
            self.assertLess(max(abs(a/65535-b) for a,b in zip(pixel,expected)),1/255)
        custom=ColorGrade(curve_master=((0.,0.),(.5,.6),(1.,1.)),skin_lighten=.25,lut_path='user.cube')
        # No dependency on a user LUT is needed to verify placement.
        with tempfile.TemporaryDirectory() as temporary:
            lut=Path(temporary)/'user.cube';lut.write_text('LUT_3D_SIZE 2\n'+'0 0 0\n'*8)
            custom.lut_path=str(lut)
            chain=grade_filter(custom)
            self.assertLess(chain.index('curves='),chain.index('okk_portrait_luts_'))
            self.assertLess(chain.index('okk_portrait_luts_'),chain.index('user.cube'))


@unittest.skipUnless(os.name=='nt' and os.environ.get('OKK_TEST_MPV_GPU')=='1','set OKK_TEST_MPV_GPU=1 for actual GPU pixels')
class PortraitGpuPixels(unittest.TestCase):
    def test_actual_paused_gpu_matches_export_without_seek_reload_or_shader_recompile(self):
        import tkinter as tk
        from mpv_controller import MpvController
        root=tk.Tk();root.title('人物提亮 GPU 验证');root.geometry('340x230+20+20')
        frame=tk.Frame(root,width=320,height=180,bg='black');frame.pack();root.update()
        engine=RealtimeGrade();controller=MpvController(str(APP/'bin'/'mpv.exe'),frame.winfo_id())
        ffmpeg=APP/'bin'/'ffmpeg.exe'
        with tempfile.TemporaryDirectory(prefix='portrait_pixels_') as temporary:
            folder=Path(temporary);source=folder/'source.mp4'
            bars=('color=c=0xb38066:s=320x180:r=30:d=2,'
                  'drawbox=x=80:y=0:w=80:h=180:color=0x1a33cc:t=fill,'
                  'drawbox=x=160:y=0:w=80:h=180:color=0x1ab333:t=fill,'
                  'drawbox=x=240:y=0:w=80:h=180:color=0xf5ebe6:t=fill')
            run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i',bars,'-c:v','libx264',
                 '-preset','ultrafast','-crf','18','-pix_fmt','yuv420p',str(source)])
            calls=[];report={}
            try:
                controller.start();backend=controller.load(str(source))
                position=controller.command(['get_property','time-pos'])
                command=controller.command
                def trace(payload,**kwargs): calls.append(payload);return command(payload,**kwargs)
                controller.command=trace
                screenshot=folder/'frame.png'
                offsets=[(90*320+x)*3 for x in (40,120,200,280)]
                def pixels():
                    root.update();time.sleep(.035)
                    controller.command(['screenshot-to-file',str(screenshot),'video'])
                    raw=run([str(ffmpeg),'-v','error','-i',str(screenshot),'-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
                    return [tuple(raw[offset:offset+3]) for offset in offsets]
                neutral=pixels();maximum=0
                cases=[(.25,.12),(.45,.22),(1.,1.)]
                actual_cases=[]
                for lighten,saturation in cases:
                    grade=ColorGrade(skin_lighten=lighten,skin_saturation=saturation)
                    engine.apply(controller,[grade]);actual=pixels()
                    raw=run([str(ffmpeg),'-v','error','-i',str(source),'-frames:v','1','-vf',grade_filter(grade),
                             '-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
                    expected=[tuple(raw[offset:offset+3]) for offset in offsets]
                    error=max(abs(a-b) for left,right in zip(actual,expected) for a,b in zip(left,right))
                    maximum=max(maximum,error);self.assertLessEqual(error,4,(actual,expected))
                    self.assertGreater(sum(actual[0]),sum(neutral[0]))
                    for index in (1,2,3):self.assertLessEqual(max(abs(a-b) for a,b in zip(actual[index],neutral[index])),1)
                    actual_cases.append({'lighten':lighten,'saturation':saturation,'pixels':actual,'max_error':error})
                self.assertTrue(controller.command(['get_property','pause']))
                self.assertEqual(controller.command(['get_property','time-pos']),position)
                self.assertEqual(controller.command(['get_property','hwdec-current']),backend['hwdec'])
                self.assertFalse(any(call[0] in ('seek','loadfile') for call in calls))
                self.assertEqual(sum(call[0]=='change-list' for call in calls),1)
                report={'successful':True,'backend':backend,'max_rgb_error_255':maximum,
                        'neutral_pixels':neutral,'cases':actual_cases,'pause_retained':True,
                        'time_pos_unchanged':True,'decoder_unchanged':True,'shader_compilations':1,
                        'seek_or_reload':False,'colour_range_selection_not_face_recognition':True}
            finally:
                process=controller.process;controller.stop()
                if process:process.wait(timeout=3)
                engine.close();root.destroy()
            (APP.parent/'portrait_gpu_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':unittest.main()
