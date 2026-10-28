"""Creative project persistence, legacy import, portability, and edit history."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creative_core import ColorGrade, Transition
from multicam_core import CameraClip, Cut, MulticamProject
from subtitle_core import SubtitleCue, SubtitleStyle, SubtitleTrack, parse_srt, write_ass


class CreativeProjectTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="creative_project_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.project_path = self.root / "工程" / "演出.multicam.json"
        self.project_path.parent.mkdir()

    def project(self):
        return MulticamProject(
            clips=[CameraClip("a", 1, str(self.root / "媒体" / "机位一.mp4"), -1.25, 11.25,
                              str(self.root / "代理" / "一.mp4")),
                   CameraClip("b", 2, str(self.root / "媒体" / "机位二.mov"), 0, 10)],
            cuts=[Cut(0, 1), Cut(3, 2), Cut(7, 1)],
            master_audio=str(self.root / "媒体" / "录音.wav"), master_duration=10,
            fps=30000 / 1001, range_start=.25, range_end=9.75,
            global_grade=ColorGrade(exposure=.7, contrast=1.1, saturation=.85, temperature=.15,
                                    tint=-.1, gamma=1.05, shadows=.2, highlights=-.25,
                                    lut_path=str(self.root / "LUT" / "总调色.cube"), enabled=True),
            camera_grades={1: ColorGrade(exposure=-.3, contrast=1.25,
                                        lut_path=str(self.root / "LUT" / "机位一.3dl")),
                           2: ColorGrade(saturation=.75, tint=.2, enabled=False)},
            transitions=[Transition(3, "dissolve", .6), Transition(7, "slideleft", .8)],
            subtitles=SubtitleTrack(
                cues=[SubtitleCue("s1", .5, 2.75, "第一句\nSecond line"),
                      SubtitleCue("s2", 7, 9.8, "结束，字幕！")],
                style=SubtitleStyle(font_family="Microsoft YaHei", font_size=56.5,
                                    color="#1020A0", outline_color="#001122", outline_width=3.5,
                                    shadow=2.25, bold=True, italic=True, alignment=8, margin_x=42,
                                    margin_y=35, box=True, background_color="#445566",
                                    background_opacity=.65),
                offset=-.125, enabled=True, burn_in=False,
                source_path=str(self.root / "字幕" / "原始.srt")),
        )

    def payload(self):
        self.project().save(self.project_path)
        return json.loads(self.project_path.read_text(encoding="utf-8"))

    def load_payload(self, payload):
        self.project_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return MulticamProject.load(self.project_path)

    def test_version_four_roundtrip_preserves_every_creative_field(self):
        project = self.project()
        before = copy.deepcopy(project)
        project.save(self.project_path)
        payload = json.loads(self.project_path.read_text(encoding="utf-8"))
        self.assertEqual(("okk-multicam", 8), (payload["format"], payload["version"]))
        self.assertEqual(project, before, "saving made source paths or effects destructive")
        loaded = MulticamProject.load(self.project_path)
        self.assertEqual(asdict(project), asdict(loaded))
        self.assertEqual({1, 2}, set(loaded.camera_grades))
        self.assertTrue(all(isinstance(key, int) for key in loaded.camera_grades))
        self.assertIsInstance(loaded.global_grade, ColorGrade)
        self.assertTrue(all(isinstance(item, Transition) for item in loaded.transitions))
        self.assertTrue(all(isinstance(item, SubtitleCue) for item in loaded.subtitles.cues))
        self.assertIsInstance(loaded.subtitles.style, SubtitleStyle)

    def test_paths_are_relative_and_move_with_the_project_tree(self):
        project = self.project()
        for path in (project.global_grade.lut_path, project.camera_grades[1].lut_path,
                     project.subtitles.source_path, project.clips[0].path,
                     project.clips[0].proxy_path, project.master_audio):
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"local asset")
        project.save(self.project_path)
        payload = json.loads(self.project_path.read_text(encoding="utf-8"))
        relative_paths = [payload["global_grade"]["lut_path"],
                          payload["camera_grades"]["1"]["lut_path"],
                          payload["subtitles"]["source_path"], payload["clips"][0]["path"],
                          payload["clips"][0]["proxy_path"], payload["master_audio"]]
        self.assertTrue(all(not Path(value).is_absolute() for value in relative_paths))
        self.assertEqual(os.path.relpath(project.global_grade.lut_path, self.project_path.parent),
                         payload["global_grade"]["lut_path"])
        relocated = self.root / "搬到另一台电脑"
        shutil.copytree(self.root / "工程", relocated / "工程")
        for directory in ("媒体", "代理", "字幕", "LUT"):
            shutil.copytree(self.root / directory, relocated / directory)
        loaded = MulticamProject.load(relocated / "工程" / self.project_path.name)
        for original, new in ((project.global_grade.lut_path, loaded.global_grade.lut_path),
                              (project.camera_grades[1].lut_path, loaded.camera_grades[1].lut_path),
                              (project.subtitles.source_path, loaded.subtitles.source_path),
                              (project.clips[0].path, loaded.clips[0].path),
                              (project.clips[0].proxy_path, loaded.clips[0].proxy_path),
                              (project.master_audio, loaded.master_audio)):
            self.assertEqual(relocated / Path(original).relative_to(self.root), Path(new))
            self.assertTrue(Path(new).is_file())
        self.assertEqual(project.subtitles.cues, loaded.subtitles.cues)

    def test_legacy_version_one_without_effect_fields_loads_and_upgrades(self):
        legacy = {"format": "okk-multicam", "version": 1,
                  "clips": [{"id": "old", "camera": 1, "path": "old-camera.mp4",
                             "start": -1, "duration": 8}],
                  "cuts": [{"time": 0, "camera": 1}], "master_audio": "master.wav",
                  "master_duration": 7, "fps": 25, "range_start": 0, "range_end": 7}
        project = self.load_payload(legacy)
        self.assertEqual(project.global_grade, ColorGrade())
        self.assertEqual(project.camera_grades, {})
        self.assertEqual(project.transitions, [])
        self.assertEqual(project.subtitles, SubtitleTrack())
        self.assertEqual(project.clips[0].path, str(self.project_path.parent / "old-camera.mp4"))
        project.save(self.project_path)
        upgraded = json.loads(self.project_path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["version"], 8)
        self.assertEqual(project, MulticamProject.load(self.project_path))

    def test_legacy_version_two_creative_fields_load_with_neutral_wheels_and_no_edits(self):
        legacy = self.payload()
        legacy['version'] = 2
        legacy.pop('edit_points', None)
        legacy.pop('clip_grades', None)
        for grade in [legacy['global_grade'], *legacy['camera_grades'].values()]:
            for key in list(grade):
                if key.startswith(('lift_', 'gamma_', 'gain_', 'offset_')):
                    grade.pop(key)
        project = self.load_payload(legacy)
        self.assertEqual(project, self.project())
        self.assertEqual(project.edit_points, [])
        self.assertEqual(project.clip_grades, [])
        self.assertEqual(project.global_grade.lift_r, 0.)
        project.save(self.project_path)
        self.assertEqual(json.loads(self.project_path.read_text(encoding='utf-8'))['version'], 8)
        self.assertEqual(MulticamProject.load(self.project_path), project)

    def test_embedded_subtitles_survive_original_srt_being_deleted(self):
        source = self.root / "原始字幕.srt"
        source.write_text("1\n00:00:01,250 --> 00:00:03,500\n中文字幕\nsecond line\n", encoding="utf-8")
        project = self.project()
        project.subtitles = SubtitleTrack(cues=parse_srt(source), source_path=str(source))
        project.save(self.project_path)
        source.unlink()
        loaded = MulticamProject.load(self.project_path)
        self.assertFalse(Path(loaded.subtitles.source_path).exists())
        self.assertEqual(project.subtitles.cues, loaded.subtitles.cues)
        rendered = write_ass(loaded.subtitles, self.root / "still-renders.ass", 1920, 1080)
        self.assertIn("中文字幕\\Nsecond line", rendered.read_text(encoding="utf-8-sig"))

    def test_subtitle_only_project_can_be_saved_before_importing_media(self):
        project = MulticamProject(subtitles=SubtitleTrack(
            cues=[SubtitleCue("only", 10, 15, "先导入字幕，再导入视频")], source_path=""))
        project.save(self.project_path)
        loaded = MulticamProject.load(self.project_path)
        self.assertEqual(loaded.clips, [])
        self.assertEqual(loaded.subtitles, project.subtitles)
        self.assertEqual(loaded.segments(), [])

    def test_default_creative_settings_are_independent_between_projects(self):
        first, second = MulticamProject(), MulticamProject()
        first.global_grade.exposure = 1
        first.camera_grades[1] = ColorGrade()
        first.transitions.append(Transition(1))
        first.subtitles.cues.append(SubtitleCue("new", 1, 2, "独立"))
        first.subtitles.style.color = "#ABCDEF"
        self.assertEqual(second, MulticamProject())

    def test_deepcopy_and_workspace_undo_redo_restore_nested_effects(self):
        # Call the real history methods on a display-free owner. Only view
        # refresh/dirty callbacks are stubs; project snapshots stay authentic.
        from multicam_ui import MulticamWorkspace
        workspace = SimpleNamespace(project=self.project(), undo_stack=[], redo_stack=[], sync_results={},
                                    _available=lambda: True, _mark_dirty=lambda: None, _refresh=lambda: None,
                                    _cancel_grade_drafts=lambda: None)
        before = copy.deepcopy(workspace.project)
        MulticamWorkspace._remember(workspace)
        edited = workspace.project
        edited.global_grade.exposure = -1
        edited.camera_grades[1].contrast = 2
        edited.transitions[0].kind = "fadewhite"
        edited.subtitles.cues[0].text = "修改后的字幕"
        edited.subtitles.style.color = "#FF0000"
        after = copy.deepcopy(edited)
        MulticamWorkspace.undo(workspace)
        self.assertEqual(workspace.project, before)
        edited.subtitles.cues[0].text = "旧对象继续变化"
        self.assertEqual(workspace.redo_stack[-1], after)
        MulticamWorkspace.redo(workspace)
        self.assertEqual(workspace.project, after)
        workspace.project.camera_grades[1].exposure = 2
        self.assertEqual(workspace.undo_stack[-1], before)

    def test_invalid_grading_fields_in_json_are_rejected(self):
        base = self.payload()
        for grade in ([], {"exposure": "1"}, {"exposure": True}, {"gamma": 0},
                      {"saturation": float("nan")}, {"enabled": 1},
                      {"lut_path": "script.lua"}, {"lut_path": "bad\x00.cube"},
                      {"unknown_effect": .5}):
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                payload = copy.deepcopy(base)
                payload["global_grade"] = grade
                self.load_payload(payload)
        for cameras in ([], {"0": {}}, {"one": {}}, {"1.5": {}}, {"1": {}, "01": {}}):
            with self.subTest(cameras=cameras), self.assertRaises(ValueError):
                payload = copy.deepcopy(base)
                payload["camera_grades"] = cameras
                self.load_payload(payload)

    def test_invalid_transition_json_is_rejected(self):
        base = self.payload()
        for transitions in ({}, [{}], [{"time": 3, "kind": "arbitrary-plugin"}],
                            [{"time": 3, "duration": -1}], [{"time": 3, "duration": float("inf")}],
                            [{"time": 3, "unknown": True}], [{"time": 4, "kind": "fade"}],
                            [{"time": 0, "duration": .5}], [{"time": 10, "duration": .5}],
                            [{"time": 3, "duration": 1}, {"time": 3, "duration": .5}]):
            with self.subTest(transitions=transitions), self.assertRaises(ValueError):
                payload = copy.deepcopy(base)
                payload["transitions"] = transitions
                self.load_payload(payload)

    def test_invalid_subtitle_style_and_cues_in_json_are_rejected(self):
        base = self.payload()
        styles = (["not-an-object"], {"font_family": "Arial\n[Events]"}, {"font_family": "{\\pos(0,0)}"},
                  {"font_size": float("inf")}, {"color": "white"}, {"alignment": True},
                  {"margin_y": -10}, {"bold": "true"}, {"unknown_style": "ignored?"})
        for style in styles:
            with self.subTest(style=style), self.assertRaises(ValueError):
                payload = copy.deepcopy(base)
                payload["subtitles"]["style"] = style
                self.load_payload(payload)
        for changes in ({"cues": {}}, {"enabled": 1}, {"offset": float("nan")},
                        {"source_path": "bad\x00.srt"},
                        {"cues": [{"id": "bad", "start": 2, "end": 1, "text": "x"}]},
                        {"cues": [{"id": "bad", "start": 1, "end": 2, "text": "x", "unknown": True}]},
                        {"cues": [{"id": "same", "start": 1, "end": 2, "text": "x"},
                                  {"id": "same", "start": 3, "end": 4, "text": "y"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                payload = copy.deepcopy(base)
                payload["subtitles"].update(changes)
                self.load_payload(payload)

    def test_unknown_project_or_track_fields_and_invalid_versions_are_rejected(self):
        base = self.payload()
        for field, value in (("version", True), ("version", "2"), ("version", 9),
                             ("clips", {}), ("cuts", "not a list"),
                             ("unexpected_executable", "run-me.py")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                payload = copy.deepcopy(base)
                payload[field] = value
                self.load_payload(payload)
        payload = copy.deepcopy(base)
        payload["subtitles"]["unknown_track_option"] = True
        with self.assertRaises(ValueError):
            self.load_payload(payload)

    def test_invalid_save_and_replace_failure_preserve_previous_project(self):
        project = self.project()
        project.save(self.project_path)
        original = self.project_path.read_bytes()
        project.global_grade.gamma = 0
        with self.assertRaises(ValueError):
            project.save(self.project_path)
        self.assertEqual(original, self.project_path.read_bytes())
        project.global_grade.gamma = 1
        with patch("multicam_core.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                project.save(self.project_path)
        self.assertEqual(original, self.project_path.read_bytes())
        self.assertEqual([], list(self.project_path.parent.glob(".okk_project_*")))


if __name__ == "__main__":
    unittest.main()
