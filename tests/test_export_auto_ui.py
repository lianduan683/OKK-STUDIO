"""Automatic delivery scheduling and accurate telemetry using isolated Tk."""
import unittest
from delivery_panel import make_delivery_settings
try:
    from . import test_program_edit_ui as fixture
except ImportError:
    import test_program_edit_ui as fixture

def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)

class AutoDeliveryUiTests(unittest.TestCase):
    setUp=fixture.ProgramEditWorkspaceTests.setUp
    tearDown=fixture.ProgramEditWorkspaceTests.tearDown

    def parallel_widget(self):
        return next(w for w in descendants(self.workspace.delivery_panel)
                    if 'textvariable' in w.keys() and str(w.cget('textvariable'))==str(self.root.parallel_var))

    def test_auto_is_default_and_manual_count_stays_available(self):
        ws=self.workspace;self.root.parallel_var.set(2)
        self.assertTrue(ws.delivery_auto_parallel_var.get())
        settings,_=make_delivery_settings(ws)
        self.assertTrue(settings.auto_parallel);self.assertEqual(settings.parallel_workers,2)
        self.assertEqual(str(self.parallel_widget().cget('state')),'disabled')
        ws.delivery_auto_parallel_var.set(False);self.root.update()
        settings,_=make_delivery_settings(ws)
        self.assertFalse(settings.auto_parallel);self.assertEqual(settings.parallel_workers,2)
        self.assertEqual(str(self.parallel_widget().cget('state')),'readonly')

    def test_cpu_export_does_not_enable_automatic_gpu_scheduling(self):
        ws=self.workspace;ws.export_mode.set('CPU 兼容');self.root.parallel_var.set(3)
        settings,_=make_delivery_settings(ws)
        self.assertFalse(settings.auto_parallel);self.assertEqual(settings.parallel_workers,3)
        self.assertEqual(settings.decode_mode,'cpu')
        self.assertEqual(str(self.parallel_widget().cget('state')),'readonly')

    def test_average_rate_and_log_path_are_retained_without_duplicate_logs(self):
        ws=self.workspace;self.root._log.reset_mock()
        snapshot=dict(stage='encoding',phase='CUDA 硬解 + GPU 调色',progress_percent=25.,
            fps=80.,speed=80/30,average_fps=61.,active_workers=4,elapsed=12.,eta=24.,
            diagnostic_path='D:/OKK STUDIO Cache/logs/exports/test.jsonl')
        ws._update_export_metrics(snapshot);ws._update_export_metrics(snapshot)
        self.assertIn('80.0 FPS',ws.export_speed_var.get())
        self.assertIn('全程 61.0 FPS',ws.export_speed_var.get())
        self.assertEqual(ws._export_diagnostic_path,snapshot['diagnostic_path'])
        self.root._log.assert_called_once()

if __name__=='__main__':unittest.main()
