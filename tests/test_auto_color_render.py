"""An automatic decision reaches actual CPU pixels with selective skin output."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from auto_color import analyze_rgb_samples,build_auto_grade
from creative_core import grade_filter
from exporter_core import CREATE_NO_WINDOW


class AutomaticPortraitRenderTests(unittest.TestCase):
    def test_auto_brightens_dim_content_protects_highlights_and_selectively_treats_skin(self):
        ffmpeg=APP/'bin'/'ffmpeg.exe'
        if not ffmpeg.is_file(): self.skipTest('Bundled FFmpeg unavailable')
        width,height=96,16
        colors=((78,45,32),(55,55,55),(20,150,40),(240,240,240))
        raw=(b''.join(bytes(color)*24 for color in colors))*height
        analysis=analyze_rgb_samples([raw]*3)
        natural=build_auto_grade(analysis,'natural')
        portrait=build_auto_grade(analysis,'portrait_clear')
        without_skin=replace(portrait,skin_lighten=0.,skin_saturation=0.)
        def render(grade):
            command=[str(ffmpeg),'-hide_banner','-loglevel','error','-f','rawvideo','-pix_fmt','rgb24',
                     '-s',f'{width}x{height}','-r','10','-i','pipe:0','-frames:v','1',
                     '-vf',grade_filter(grade),'-pix_fmt','rgb24','-f','rawvideo','pipe:1']
            result=subprocess.run(command,input=raw,capture_output=True,creationflags=CREATE_NO_WINDOW,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
            offsets=[((height//2)*width+12+24*index)*3 for index in range(4)]
            return [tuple(result.stdout[offset:offset+3]) for offset in offsets]
        automatic=render(natural)
        unmasked=render(without_skin)
        treated=render(portrait)
        self.assertGreater(sum(automatic[0]),sum(colors[0]))
        self.assertGreater(sum(automatic[1]),sum(colors[1]))
        self.assertLessEqual(max(automatic[3]),254)
        self.assertGreater(sum(treated[0]),sum(unmasked[0]))
        for index in (1,2,3):
            self.assertLessEqual(max(abs(a-b) for a,b in zip(treated[index],unmasked[index])),1)
        self.assertLessEqual(max(treated[3]),254)
        self.assertEqual(build_auto_grade(analysis,'portrait_clear',base_grade=portrait),portrait)
        report={'successful':True,'tests':1,'passed':1,'input_pixels':colors,'automatic_pixels':automatic,
                'portrait_without_skin':unmasked,'portrait_pixels':treated,
                'exposure_ev':natural.exposure,'gamma':natural.gamma,
                'highlights_not_newly_clipped':True,'skin_selective':True,'non_cumulative':True}
        (APP.parent/'auto_color_render_validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':unittest.main()
