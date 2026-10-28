from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gpu_profiles import classify_gpu  # noqa: E402


class GpuProfileTests(unittest.TestCase):
    def test_supported_rtx_20_and_30_desktop_profiles(self) -> None:
        cases = {
            "NVIDIA GeForce RTX 2060": "Turing",
            "NVIDIA GeForce RTX 2060 SUPER": "Turing",
            "NVIDIA GeForce RTX 2070 SUPER": "Turing",
            "NVIDIA GeForce RTX 2080 Ti": "Turing",
            "NVIDIA GeForce RTX 3050": "Ampere",
            "NVIDIA GeForce RTX 3060 Ti": "Ampere",
            "NVIDIA GeForce RTX 3070": "Ampere",
            "NVIDIA GeForce RTX 3080 Ti": "Ampere",
            "NVIDIA GeForce RTX 3090 Ti": "Ampere",
        }
        for name, architecture in cases.items():
            with self.subTest(name=name):
                profile = classify_gpu(name)
                self.assertTrue(profile.in_supported_range)
                self.assertEqual(profile.architecture, architecture)
                self.assertEqual(profile.nvenc_generation, 7)
                self.assertEqual(profile.nvenc_engines, 1)
                self.assertFalse(profile.hardware_av1)

    def test_supported_rtx_20_and_30_laptop_profiles(self) -> None:
        cases = {
            "NVIDIA GeForce RTX 2050": "Ampere",
            "NVIDIA GeForce RTX 2050 Laptop GPU": "Ampere",
            "NVIDIA GeForce RTX 2060 Laptop GPU": "Turing",
            "NVIDIA GeForce RTX 2070 Max-Q": "Turing",
            "NVIDIA GeForce RTX 2080 SUPER Max-Q": "Turing",
            "NVIDIA GeForce RTX 3050 Ti Laptop GPU": "Ampere",
            "NVIDIA GeForce RTX 3060 Laptop GPU": "Ampere",
            "NVIDIA GeForce RTX 3070 Ti Laptop GPU": "Ampere",
            "NVIDIA GeForce RTX 3080 Ti Laptop GPU": "Ampere",
        }
        for name, architecture in cases.items():
            with self.subTest(name=name):
                profile = classify_gpu(name)
                self.assertTrue(profile.in_supported_range)
                self.assertEqual(profile.form_factor, "Laptop")
                self.assertEqual(profile.architecture, architecture)
                self.assertEqual(profile.nvenc_generation, 7)
                self.assertEqual(profile.nvenc_engines, 1)
                self.assertEqual(profile.recommended_proxy_parallel, 1)
                self.assertFalse(profile.hardware_av1)

    def test_supported_desktop_engine_counts(self) -> None:
        cases = {
            "NVIDIA GeForce RTX 4060": ("Ada Lovelace", 1),
            "NVIDIA GeForce RTX 4060 Ti": ("Ada Lovelace", 1),
            "NVIDIA GeForce RTX 4070 SUPER": ("Ada Lovelace", 1),
            "NVIDIA GeForce RTX 4070 Ti SUPER": ("Ada Lovelace", 2),
            "NVIDIA GeForce RTX 4080 SUPER": ("Ada Lovelace", 2),
            "NVIDIA GeForce RTX 4090": ("Ada Lovelace", 2),
            "NVIDIA GeForce RTX 5050": ("Blackwell", 1),
            "NVIDIA GeForce RTX 5060 Ti": ("Blackwell", 1),
            "NVIDIA GeForce RTX 5070": ("Blackwell", 1),
            "NVIDIA GeForce RTX 5070 Ti": ("Blackwell", 2),
            "NVIDIA GeForce RTX 5080": ("Blackwell", 2),
            "NVIDIA GeForce RTX 5090": ("Blackwell", 3),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                profile = classify_gpu(name)
                self.assertEqual((profile.architecture, profile.nvenc_engines), expected)
                self.assertTrue(profile.in_supported_range)
                self.assertTrue(profile.hardware_av1)

    def test_supported_laptop_engine_counts(self) -> None:
        cases = {
            "NVIDIA GeForce RTX 4060 Laptop GPU": 1,
            "NVIDIA GeForce RTX 4070 Laptop GPU": 1,
            "NVIDIA GeForce RTX 4080 Laptop GPU": 2,
            "NVIDIA GeForce RTX 4090 Laptop GPU": 2,
            "NVIDIA GeForce RTX 5070 Ti Laptop GPU": 1,
            "NVIDIA GeForce RTX 5080 Laptop GPU": 2,
            "NVIDIA GeForce RTX 5090 Laptop GPU": 3,
        }
        for name, engines in cases.items():
            with self.subTest(name=name):
                profile = classify_gpu(name)
                self.assertEqual(profile.form_factor, "Laptop")
                self.assertEqual(profile.nvenc_engines, engines)
                self.assertTrue(profile.in_supported_range)

    def test_future_rtx_uses_conservative_compatibility_profile(self) -> None:
        profile = classify_gpu("NVIDIA GeForce RTX 6060 Laptop GPU")
        self.assertTrue(profile.in_supported_range)
        self.assertEqual(profile.nvenc_engines, 1)
        self.assertIn("兼容模式", profile.architecture)

    def test_workstation_name_is_not_misclassified_as_geforce_50_series(self) -> None:
        profile = classify_gpu("NVIDIA RTX 5000 Ada Generation")
        self.assertFalse(profile.in_supported_range)
        self.assertIsNone(profile.model)


if __name__ == "__main__":
    unittest.main()
