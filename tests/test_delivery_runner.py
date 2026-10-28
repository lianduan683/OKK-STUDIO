"""A failing export observer cannot leave its encoder process running."""
from pathlib import Path
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtx5090_exporter import ExporterApp


class DeliveryRunnerTests(unittest.TestCase):
    def test_observer_exception_reaps_real_child_before_unregistering(self):
        children = []
        registrations = []
        original_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            self.addCleanup(lambda: child.kill() if child.poll() is None else None)
            return child

        def unregister(child):
            self.assertIsNotNone(child.poll(), 'child was unregistered while running')
            registrations.remove(child)

        app = SimpleNamespace(cancel_event=threading.Event(), events=queue.Queue(),
                              _register_process=registrations.append,
                              _unregister_process=unregister)
        app._terminate = lambda child: ExporterApp._terminate(app, child)

        def failing_observer(snapshot):
            raise RuntimeError('observer intentionally failed')

        with patch('rtx5090_exporter.subprocess.Popen', side_effect=launch):
            code, error = ExporterApp._run_editor_process(
                app, [sys.executable, '-u', '-c', 'import time; time.sleep(30)'],
                failing_observer)
        self.assertEqual(code, 1)
        self.assertIn('observer intentionally failed', error)
        self.assertEqual(registrations, [])
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stderr.closed)


if __name__ == '__main__':
    unittest.main()
