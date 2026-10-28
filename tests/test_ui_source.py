import ast
import queue
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rtx5090_exporter import ExporterApp  # noqa: E402
from exporter_core import Capabilities, MediaInfo, build_encode_command


class TkinterSourceTests(unittest.TestCase):
    def test_ai_subtitle_tab_is_removed_and_concert_workflow_is_combined(self):
        source_path = Path(__file__).resolve().parents[1] / "rtx5090_exporter.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn('notebook.add(subtitles, text="  AI 字幕  ")', source)
        self.assertNotIn('toolbox.add(audio_panel, text="  替换音频  ")', source)
        self.assertIn('toolbox.add(join_panel, text="  拼接 / 换音  ")', source)
        self.assertIn('text="对齐、拼接并导出"', source)
        self.assertIn('text="试听对齐结果"', source)
        self.assertIn('controller.load_aligned_preview(', source)
        self.assertIn('text="手动对齐"', source)
        self.assertIn('text="应用时间"', source)
        self.assertIn('text="提前"', source)
        self.assertIn('text="延后"', source)
        self.assertIn('"PCM 32-bit 整数（MOV/MKV）"', source)
        self.assertIn('"PCM 32-bit 浮点（MOV/MKV）"', source)
        self.assertIn('"裁掉开头画面（从主音频 00:00 开始）"', source)
        self.assertIn('"保留开头画面（主音频开始前静音）"', source)
        self.assertIn('"裁掉结尾画面（到主音频结束）"', source)
        self.assertIn('"保留结尾画面（主音频结束后静音）"', source)
        self.assertIn('text="成片开始（留空自动）"', source)
        self.assertIn('text="成片结束（留空按上方）"', source)
        self.assertIn('textvariable=self.editor_join_range_start_var', source)
        self.assertIn('textvariable=self.editor_join_range_end_var', source)
        self.assertIn('"MOV / HEVC H.265（NVENC）"', source)
        self.assertIn('join_video_output_combo.bind("<<ComboboxSelected>>"', source)
        self.assertIn('text="◀ 10 秒"', source)
        self.assertIn('text="10 秒 ▶"', source)
        self.assertIn('["seek", float(seconds), "relative+exact"]', source)

    def test_join_page_scrolls_and_uses_unified_export_parameters(self):
        source_path = Path(__file__).resolve().parents[1] / "rtx5090_exporter.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn('canvas = tk.Canvas(panel, bg=PANEL_ALT', source)
        self.assertIn('canvas.yview_scroll(delta * 3, "units")', source)
        self.assertIn('textvariable=self.editor_join_shared_settings_var', source)
        self.assertIn('encode_settings = self._settings()', source)
        self.assertIn('footer, text="对齐、拼接并导出"', source)
        # Shared values must reach the real command builder, regardless of
        # whether the compact join pane duplicates their widgets.
        app = object.__new__(ExporterApp)
        values = {'codec_var':'H.264 (NVENC)', 'preset_var':'均衡 P4',
                  'resolution_var':'1080p', 'depth_var':'8-bit', 'quality_var':17,
                  'maxrate_var':'20M', 'decode_var':'CPU 兼容解码',
                  'split_var':'强制 2 路', 'parallel_var':3, 'audio_var':'AAC 192 kbps'}
        for name, value in values.items():
            setattr(app, name, SimpleNamespace(get=lambda name=name: values[name]))
        summary = []
        app.editor_join_shared_settings_var = SimpleNamespace(set=summary.append)
        app._update_join_shared_settings_summary()
        self.assertIn('CQ 17', summary[-1])
        self.assertIn('并行 3 路', summary[-1])
        settings = app._settings()
        self.assertEqual((settings.codec, settings.preset, settings.quality, settings.decode_mode),
                         ('h264_nvenc', 'p4', 17, 'cpu'))
        self.assertEqual((settings.resolution, settings.bit_depth, settings.maxrate,
                          settings.split_encode, settings.audio_bitrate),
                         ('1080p', '8-bit', '20M', '2', '192k'))
        command = build_encode_command('ffmpeg', 'source.mp4', 'output.mp4', settings,
            MediaInfo('source.mp4', duration=8, width=3840, height=2160, fps=30, has_audio=True),
            Capabilities())
        for flag, value in (('-c:v','h264_nvenc'),('-preset','p4'),('-cq','17'),('-maxrate','20M')):
            self.assertEqual(command[command.index(flag)+1], value)
        self.assertIn('scale=-2:1080', command[command.index('-vf')+1])
        values['preset_var'] = '最快 P1'; values['decode_var'] = 'NVIDIA CUDA（推荐）'
        self.assertEqual((app._settings().preset, app._settings().decode_mode), ('p1','cuda'))

    def test_global_footer_reserves_height_before_flexible_notebook(self):
        source_path = Path(__file__).resolve().parents[1] / "rtx5090_exporter.py"
        source = source_path.read_text(encoding="utf-8")
        build_ui = source[source.index("    def _build_ui"):source.index("    def _build_export_tab")]
        self.assertLess(build_ui.index("self._build_footer(outer)"), build_ui.index("main = ttk.Frame(outer)"))
        self.assertIn('footer.pack(side="bottom", fill="x")', source)

    def test_widget_internal_padding_uses_single_screen_distance(self):
        source_path = Path(__file__).resolve().parents[1] / "rtx5090_exporter.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        invalid = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id in {"tk", "ttk"}
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg in {"padx", "pady"} and isinstance(keyword.value, (ast.Tuple, ast.List)):
                    invalid.append((node.lineno, keyword.arg))

        self.assertEqual(
            invalid,
            [],
            "Tk/ttk widget padx/pady must be a single screen distance; use pack/grid for asymmetric padding",
        )

    def test_all_editor_exports_share_footer_progress(self):
        source_path = Path(__file__).resolve().parents[1] / "rtx5090_exporter.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("def _begin_shared_progress", source)
        self.assertIn("def _editor_progress_reporter", source)
        self.assertIn('"editor_export_progress"', source)
        self.assertIn('self._begin_shared_progress(f"{operation} 0.0%")', source)
        self.assertIn('self._begin_shared_progress(f"{action}：准备素材…")', source)
        self.assertIn('self._begin_shared_progress("音视频分离 0.0%")', source)
        self.assertIn('self._begin_shared_progress("替换音频 0.0%")', source)
        self.assertIn("FPS", source)
        self.assertIn("stderr=subprocess.PIPE", source)

    def test_editor_runner_reads_clean_live_progress_separately_from_stderr(self):
        app = object.__new__(ExporterApp)
        app.process_lock = threading.Lock()
        app.processes = []
        app.cancel_event = threading.Event()
        app.events = queue.Queue()
        snapshots = []
        script = (
            "import sys;"
            "print('frame=12', flush=True);"
            "print('fps=59.94', flush=True);"
            "print('out_time_us=1000000', flush=True);"
            "print('speed=1.25x', flush=True);"
            "print('progress=continue', flush=True);"
            "sys.stderr.write('diagnostic warning\\n');sys.stderr.flush();"
            "print('frame=24', flush=True);"
            "print('fps=61.50', flush=True);"
            "print('out_time_us=2000000', flush=True);"
            "print('speed=1.30x', flush=True);"
            "print('progress=end', flush=True)"
        )

        code, details = app._run_editor_process([sys.executable, "-c", script], snapshots.append)

        self.assertEqual(code, 0)
        self.assertIn("diagnostic warning", details)
        self.assertGreaterEqual(len(snapshots), 3)  # initial 0%, continue, end
        self.assertEqual(snapshots[-1]["fps"], "61.50")
        self.assertEqual(snapshots[-1]["speed"], "1.30x")

    def test_start_script_prefers_current_source_over_stale_exe(self):
        start = (Path(__file__).resolve().parents[1] / "start.bat").read_text(encoding="utf-8")
        self.assertLess(start.index("where py"), start.index('if exist "RTX_Video_Render_Studio.exe"'))


if __name__ == "__main__":
    unittest.main()
