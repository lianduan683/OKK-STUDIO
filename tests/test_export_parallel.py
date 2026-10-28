"""Memory capacity policy preserves manual controls and source-size budgets."""
from __future__ import annotations
from copy import deepcopy
from types import SimpleNamespace
import unittest

from exporter_core import Capabilities, MediaInfo
from export_parallel import choose_export_workers


def config(**changes):
    values=dict(codec='hevc_nvenc',decode_mode='cuda',resolution='4k',bit_depth='auto',
                parallel_workers=1,auto_parallel=True)
    values.update(changes)
    return SimpleNamespace(**values)


def caps(memory=24576,**changes):
    values=dict(gpu_memory_mb=memory,nvenc_codecs={'hevc_nvenc','h264_nvenc'},cuda_decode=True)
    values.update(changes)
    return Capabilities(**values)


def picture(width=7680,height=4320,depth=10,format='yuv420p10le'):
    return MediaInfo('synthetic.mov',width=width,height=height,bit_depth=depth,pixel_format=format)


class ParallelExportTests(unittest.TestCase):
    def test_manual_counts_remain_exact_for_all_gpu_memory_sizes(self):
        for manual in range(1,5):
            for memory in (0,2048,8192,24576):
                with self.subTest(manual=manual,memory=memory):
                    self.assertEqual(choose_export_workers(config(auto_parallel=False,parallel_workers=manual),
                                                         caps(memory),{'camera':picture()}),manual)
    def test_legacy_settings_without_auto_flag_keep_manual(self):
        value=config(parallel_workers=3);del value.auto_parallel
        self.assertEqual(choose_export_workers(value,caps(),[picture()]),3)
    def test_cpu_or_unavailable_hardware_routes_keep_manual_even_when_auto_enabled(self):
        for value,hardware in ((config(codec='libx265'),caps()),(config(decode_mode='cpu'),caps()),
                               (config(),caps(cuda_decode=False)),(config(),caps(nvenc_codecs=set()))):
            value.parallel_workers=3
            with self.subTest(settings=value,caps=hardware):
                self.assertEqual(choose_export_workers(value,hardware,[picture()]),3)
    def test_24_gib_5090_8k10_to_4k_can_allocate_four_workers(self):
        self.assertEqual(choose_export_workers(config(),caps(),{'8k':picture()}),4)
    def test_8k10_low_vram_limits_workers_and_preserves_two_gib_reserve(self):
        expected={2048:1,4096:1,8192:1,12288:2,16384:2,24576:4}
        for memory,count in expected.items():
            with self.subTest(memory=memory):
                self.assertEqual(choose_export_workers(config(),caps(memory),[picture()]),count)
    def test_source_ten_bit_costs_more_even_when_output_forces_eight_bit(self):
        value=config(bit_depth='8-bit')
        eight=choose_export_workers(value,caps(8192),[picture(depth=8,format='yuv420p')])
        ten=choose_export_workers(value,caps(8192),[picture()])
        self.assertGreater(eight,ten)
        self.assertEqual(ten,1)
    def test_unknown_vram_never_selects_more_than_two(self):
        for unknown in (0,None,-1,float('nan')):
            for source in (picture(),picture(1920,1080,8,'yuv420p')):
                with self.subTest(memory=unknown,source=source.width):
                    self.assertIn(choose_export_workers(config(),caps(unknown),[source]),(1,2))
    def test_maximum_source_is_budgeted_independently_of_mapping_order(self):
        large=picture();small=picture(1920,1080,8,'yuv420p')
        hardware=caps(8192)
        self.assertEqual(choose_export_workers(config(),hardware,{'first':small,'last':large}),1)
        self.assertEqual(choose_export_workers(config(),hardware,{'first':large,'last':small}),1)
        self.assertGreater(choose_export_workers(config(),hardware,[small]),1)
    def test_larger_output_and_chroma_surfaces_cannot_increase_parallelism(self):
        hardware=caps(12288)
        normal=choose_export_workers(config(),hardware,[picture()])
        self.assertLessEqual(choose_export_workers(config(resolution='8k'),hardware,[picture()]),normal)
        self.assertLessEqual(choose_export_workers(config(),hardware,[picture(format='yuv444p10le')]),normal)
    def test_unknown_picture_metadata_remains_conservative(self):
        for media in ([],{},[MediaInfo('unprobed')]):
            self.assertIn(choose_export_workers(config(),caps(),media),(1,2))
    def test_upper_bound_is_four_on_large_vram_and_objects_are_not_mutated(self):
        value=config();hardware=caps(128*1024);media={'camera':picture(1920,1080,8,'yuv420p')}
        original=deepcopy((vars(value),hardware,media))
        self.assertEqual(choose_export_workers(value,hardware,media),4)
        self.assertEqual((vars(value),hardware,media),original)
    def test_invalid_manual_count_is_rejected_instead_of_silently_clamped(self):
        for invalid in (0,5,True,1.5,'2'):
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                choose_export_workers(config(parallel_workers=invalid),caps(),[picture()])
    def test_resolved_original_delivery_size_keeps_largest_source_decode_budget(self):
        mixed={'reference':picture(3840,2160,10),'other':picture()}
        value=config(resolution='original');hardware=caps(24576)
        self.assertEqual(choose_export_workers(value,hardware,mixed,output_size=(3840,2160)),4)
        self.assertLess(choose_export_workers(value,hardware,mixed,output_size=(7680,4320)),4)
        self.assertEqual(choose_export_workers(value,hardware,mixed),2)
        self.assertEqual(choose_export_workers(value,caps(8192),mixed,output_size=(3840,2160)),1)
    def test_resolved_delivery_size_requires_two_positive_even_integers(self):
        for invalid in ((0,2160),(3840,1),(3839,2160),(3840,2161),(3840.,2160),
                        (True,2160),(3840,),[3840,2160,1],'3840x2160',{'w':3840,'h':2160}):
            with self.subTest(size=invalid),self.assertRaises(ValueError):
                choose_export_workers(config(),caps(),[picture()],output_size=invalid)
        self.assertIn(choose_export_workers(config(),caps(),[picture()],output_size=[3840,2160]),range(1,5))
    def test_single_media_object_and_generator_match_mapping(self):
        source=picture();hardware=caps(12288)
        expected=choose_export_workers(config(),hardware,{'one':source})
        self.assertEqual(choose_export_workers(config(),hardware,source),expected)
        self.assertEqual(choose_export_workers(config(),hardware,(v for v in [source])),expected)


if __name__=='__main__':unittest.main(verbosity=2)
