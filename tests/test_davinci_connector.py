import importlib.util
import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONNECTOR = ROOT / "davinci" / "OKK_Export.py"


def load_connector():
    spec = importlib.util.spec_from_file_location("okk_davinci_connector", CONNECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeProject:
    def __init__(self) -> None:
        self.selected = None

    def GetRenderFormats(self):
        return {"QuickTime": "mov", "MP4": "mp4"}

    def GetRenderCodecs(self, format_code):
        self.format_code = format_code
        return {"H.264": "H264", "DNxHR HQX 12-bit": "DNxHRHQX12"}

    def SetCurrentRenderFormatAndCodec(self, format_code, codec_code):
        self.selected = (format_code, codec_code)
        return True


class DaVinciConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connector = load_connector()

    def test_selects_quicktime_and_dnxhr_hqx_dynamically(self) -> None:
        project = FakeProject()
        selected = self.connector._configure_intermediate(project)
        self.assertEqual(selected, ("mov", "DNxHRHQX12"))
        self.assertEqual(project.selected, selected)

    def test_safe_timeline_filename(self) -> None:
        self.assertEqual(self.connector._safe_name('Show: A/B?*'), "Show_ A_B__")

    def test_installer_targets_resolve_utility_scripts(self) -> None:
        installer = (ROOT / "一键安装达芬奇连接器.bat").read_text(encoding="utf-8")
        self.assertIn(r"DaVinci Resolve\Support\Fusion\Scripts\Utility\OKK GROUP", installer)
        self.assertIn("OKK_Export.py", installer)
        source = CONNECTOR.read_text(encoding="utf-8")
        self.assertIn('"--headless-export"', source)
        self.assertIn('"--codec", "hevc_nvenc"', source)
        self.assertIn('"DNxHR HQX 12-bit"', source)
        self.assertIn("StopRendering", source)
        build_script = (ROOT / "build_exe.bat").read_text(encoding="utf-8")
        self.assertIn("OKK_STUDIO.spec", build_script)
        build_tree = ast.parse((ROOT / "OKK_STUDIO.spec").read_text(encoding="utf-8"))
        analysis = next(node for node in ast.walk(build_tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'Analysis')
        hidden = next(item.value for item in analysis.keywords if item.arg == 'hiddenimports')
        self.assertIsInstance(hidden, ast.List)
        self.assertIn('okk_cli', [item.value for item in hidden.elts if isinstance(item, ast.Constant)])


if __name__ == "__main__":
    unittest.main()
