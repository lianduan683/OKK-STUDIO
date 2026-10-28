"""White-balance gains and selective yellow-skin correction, including pixels."""
from __future__ import annotations

from array import array
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from creative_core import (ColorGrade,WB_RANGES,grade_filter,grade_from_dict,grade_to_dict,
                           grade_is_identity,validate_grade)
from portrait_color import portrait_rgb,portrait_lut_path
from realtime_grade import RealtimeGrade,grade_uniforms,shader_source
from exporter_core import CREATE_NO_WINDOW


def run(command,**kwargs):
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,timeout=50,**kwargs)
    if result.returncode:raise RuntimeError(result.stderr.decode('utf-8','replace')[-2500:])
    return result.stdout


class NeutralPortraitModelTests(unittest.TestCase):
    def test_defaults_backward_compatible_and_new_controls_serialize(self):
        grade=grade_from_dict({'exposure':.25,'skin_lighten':.45,'skin_saturation':.22})
        self.assertEqual((grade.wb_red,grade.wb_green,grade.wb_blue,grade.skin_deyellow),(1.,1.,1.,0.))
        self.assertTrue(grade_is_identity(ColorGrade()))
        enhanced=replace(grade,wb_red=.87,wb_green=.92,skin_deyellow=.75)
        self.assertEqual(grade_from_dict(grade_to_dict(enhanced)),enhanced)
        self.assertFalse(grade_is_identity(ColorGrade(wb_red=.99)))
        self.assertFalse(grade_is_identity(ColorGrade(skin_deyellow=.01)))
        self.assertTrue(grade_is_identity(replace(enhanced,enabled=False)))
        for name in WB_RANGES:
            for value in (.49,2.01,True,float('inf'),float('nan')):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    validate_grade(ColorGrade(**{name:value}))
        for value in (-.01,1.01,True,float('nan')):
            with self.assertRaises(ValueError):validate_grade(ColorGrade(skin_deyellow=value))

    def test_yellow_skin_moves_toward_neutral_pink_without_background_wash(self):
        warm=(.76,.62,.43)
        changed=portrait_rgb(warm,0,0,.75)
        self.assertGreater(changed[2],warm[2]+.1)
        self.assertLess(changed[1]-changed[2],(warm[1]-warm[2])*.45)
        self.assertGreater(changed[0],changed[1])
        for rgb in ((.1,.2,.8),(.1,.7,.2),(.4,.18,.55),(.6,.6,.6),(.75,.64,.67)):
            self.assertEqual(portrait_rgb(rgb,0,0,.75),rgb)
        highlight=(.96,.92,.90)
        self.assertLess(max(abs(a-b) for a,b in zip(highlight,portrait_rgb(highlight,.55,.18,.75))),.002)

    def test_lut_keys_include_deyellow_and_legacy_call_has_same_math(self):
        self.assertEqual(portrait_rgb((.7,.5,.4),.45,.22),portrait_rgb((.7,.5,.4),.45,.22,0.))
        original=portrait_lut_path(.45,.22)
        improved=portrait_lut_path(.45,.22,.75)
        self.assertNotEqual(original,improved)
        stamp=Path(improved).stat().st_mtime_ns
        self.assertEqual(Path(portrait_lut_path(.45,.22,.75)).stat().st_mtime_ns,stamp)

    def test_wb_and_deyellow_use_dynamic_uniforms(self):
        values,_=grade_uniforms([ColorGrade(wb_red=.87,wb_green=.92,skin_deyellow=.75)])
        self.assertEqual(values['g0_wb_red'],.87)
        self.assertEqual(values['g1_wb_red'],1.)
        self.assertEqual(values['g0_skin_deyellow'],.75)
        for field in ('wb_red','wb_green','wb_blue','skin_deyellow'):
            self.assertIn(f'//!PARAM g0_{field}\n//!TYPE DYNAMIC float',shader_source())


class NeutralPortraitCpuTests(unittest.TestCase):
    ffmpeg=APP/'bin'/'ffmpeg.exe'
    @classmethod
    def setUpClass(cls):
        if not cls.ffmpeg.is_file():raise unittest.SkipTest('FFmpeg unavailable')

    def render(self,pixels,grade):
        source=bytes(value for rgb in pixels for value in rgb)*2
        output=run([str(self.ffmpeg),'-v','error','-f','rawvideo','-pix_fmt','rgb24','-s',f'{len(pixels)}x2',
            '-i','pipe:0','-frames:v','1','-vf',grade_filter(grade),'-pix_fmt','rgb24','-f','rawvideo','pipe:1'],input=source)
        return [tuple(output[index*3:index*3+3]) for index in range(len(pixels))]

    def test_real_wb_neutralizes_yellow_white_and_precedes_exposure(self):
        white=(245,232,213)
        output=self.render([white],ColorGrade(wb_red=213/245,wb_green=213/232))[0]
        self.assertLessEqual(max(output)-min(output),1)
        self.assertLess(max(output),255)
        source=(204,128,64)
        actual=self.render([source],ColorGrade(wb_red=.6,exposure=1.))[0]
        expected=(min(255,round(204*.6*2)),255,128)
        self.assertLessEqual(max(abs(a-b) for a,b in zip(actual,expected)),1)

    def test_real_selective_deyellow_matches_reference_and_protects_colours(self):
        pixels=[(194,158,110),(245,232,213),(26,51,204),(102,46,140),(191,163,171),(245,235,230)]
        grade=ColorGrade(skin_lighten=.55,skin_saturation=.18,skin_deyellow=.75)
        output=self.render(pixels,grade)
        for index,(original,actual) in enumerate(zip(pixels,output)):
            expected=portrait_rgb(tuple(value/255 for value in original),.55,.18,.75)
            self.assertLessEqual(max(abs(a-b*255) for a,b in zip(actual,expected)),3)
            if index in (2,3):self.assertLessEqual(max(abs(a-b) for a,b in zip(original,actual)),1)
        self.assertLess(output[0][1]-output[0][2],pixels[0][1]-pixels[0][2])
        self.assertLessEqual(max(output[-1]),max(pixels[-1])+1)

    def test_new_chain_keeps_more_than_eight_bit_precision(self):
        width,height=512,2
        red=[round((.4+.2*index/(width-1))*65535) for index in range(width)]*height
        green=[round(value*.82) for value in red];blue=[round(value*.55) for value in red]
        packed=array('H',green+blue+red).tobytes()
        grade=ColorGrade(wb_red=.92,wb_green=.97,skin_lighten=.35,skin_saturation=.12,skin_deyellow=.75)
        output=run([str(self.ffmpeg),'-v','error','-f','rawvideo','-pix_fmt','gbrp16le','-s',f'{width}x{height}',
            '-i','pipe:0','-frames:v','1','-vf',grade_filter(grade),'-pix_fmt','gbrp16le','-f','rawvideo','pipe:1'],input=packed)
        actual=array('H');actual.frombytes(output);count=width*height
        self.assertGreater(len(set(actual[:width])),256)
        for index in range(0,width,41):
            expected=portrait_rgb((red[index]/65535*.92,green[index]/65535*.97,blue[index]/65535),.35,.12,.75)
            pixel=(actual[2*count+index],actual[index],actual[count+index])
            self.assertLessEqual(max(abs(a-b*65535) for a,b in zip(pixel,expected)),600)


@unittest.skipUnless(os.name=='nt' and os.environ.get('OKK_TEST_MPV_GPU')=='1','set OKK_TEST_MPV_GPU=1')
class NeutralPortraitGpuTests(unittest.TestCase):
    def test_gpu_matches_ffmpeg_and_retains_decoder_clock(self):
        import tkinter as tk
        from mpv_controller import MpvController
        root=tk.Tk();root.title('去黄与白平衡 GPU 验证');root.geometry('500x230+20+20')
        frame=tk.Frame(root,width=480,height=180,bg='black');frame.pack();root.update()
        controller=MpvController(str(APP/'bin'/'mpv.exe'),frame.winfo_id());engine=RealtimeGrade()
        ffmpeg=APP/'bin'/'ffmpeg.exe';report={}
        with tempfile.TemporaryDirectory(prefix='neutral_portrait_gpu_') as folder_name:
            folder=Path(folder_name);source=folder/'bars.mp4'
            colours=('0xc29e6e','0xf5e8d5','0x1a33cc','0x662e8c','0xbfa3ab','0xf5ebe6')
            graph='color=c='+colours[0]+':s=480x180:r=30:d=2'
            for index,colour in enumerate(colours[1:],1):
                graph+=f',drawbox=x={index*80}:y=0:w=80:h=180:color={colour}:t=fill'
            run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i',graph,'-c:v','libx264',
                 '-preset','ultrafast','-crf','18','-pix_fmt','yuv420p',str(source)])
            calls=[]
            try:
                controller.start();backend=controller.load(str(source))
                position=controller.command(['get_property','time-pos']);command=controller.command
                def trace(payload,**kwargs):calls.append(payload);return command(payload,**kwargs)
                controller.command=trace
                screenshot=folder/'frame.png';offsets=[(90*480+40+index*80)*3 for index in range(6)]
                def pixels():
                    root.update();time.sleep(.045)
                    controller.command(['screenshot-to-file',str(screenshot),'video'])
                    raw=run([str(ffmpeg),'-v','error','-i',str(screenshot),'-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
                    return [tuple(raw[offset:offset+3]) for offset in offsets]
                original=pixels();maximum=0;cases=[]
                for grade in (ColorGrade(skin_deyellow=.75),
                              ColorGrade(skin_lighten=.55,skin_saturation=.18,skin_deyellow=.75),
                              ColorGrade(wb_red=.87,wb_green=.92),
                              ColorGrade(exposure=.1,wb_red=.87,wb_green=.92,skin_lighten=.55,skin_saturation=.18,skin_deyellow=.75)):
                    engine.apply(controller,[grade]);actual=pixels()
                    raw=run([str(ffmpeg),'-v','error','-i',str(source),'-frames:v','1','-vf',grade_filter(grade),
                        '-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
                    expected=[tuple(raw[offset:offset+3]) for offset in offsets]
                    error=max(abs(a-b) for left,right in zip(actual,expected) for a,b in zip(left,right))
                    self.assertLessEqual(error,4,(actual,expected));maximum=max(maximum,error)
                    if grade.wb_red==1.:
                        for index in (2,3):self.assertLessEqual(max(abs(a-b) for a,b in zip(actual[index],original[index])),1)
                    cases.append({'grade':grade_to_dict(grade),'pixels':actual,'max_error_255':error})
                self.assertTrue(controller.command(['get_property','pause']))
                self.assertEqual(controller.command(['get_property','time-pos']),position)
                self.assertEqual(controller.command(['get_property','hwdec-current']),backend['hwdec'])
                self.assertFalse(any(call[0] in ('seek','loadfile') for call in calls))
                self.assertEqual(sum(call[0]=='change-list' for call in calls),1)
                report={'successful':True,'backend':backend,'max_rgb_error_255':maximum,'original_pixels':original,
                    'cases':cases,'pause_retained':True,'time_pos_unchanged':True,'decoder_retained':True,
                    'seek_or_reload':False,'shader_compilations':1}
            finally:
                process=controller.process;controller.stop()
                if process:process.wait(timeout=4)
                engine.close();root.destroy()
            (APP.parent/'neutral_portrait_gpu_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':unittest.main()
