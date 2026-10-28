from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import MediaInfo  # noqa: E402
from media_pool_core import (  # noqa: E402
    ALL_BIN_ID,
    SUPPORTED_VIDEO_EXTENSIONS,
    UNGROUPED_BIN_ID,
    MediaPoolModel,
    collect_video_paths,
    is_supported_video,
    media_key,
)


def make_info(path: str, *, duration: float = 0.0, width: int = 0, height: int = 0,
              fps: float = 0.0, video_codec: str = "unknown", bit_depth: int = 8,
              audio_codec: str = "none") -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        video_codec=video_codec,
        bit_depth=bit_depth,
        audio_codec=audio_codec,
    )


class TempFilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def touch(self, name: str, directory: Path | None = None) -> Path:
        directory = directory or self.root
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        target.write_bytes(b"")
        return target


class SupportedExtensionTests(unittest.TestCase):
    def test_all_listed_extensions_supported(self) -> None:
        for ext in SUPPORTED_VIDEO_EXTENSIONS:
            self.assertTrue(is_supported_video(Path("clip") / f"clip{ext}"), ext)

    def test_extension_case_insensitive(self) -> None:
        self.assertTrue(is_supported_video("A.MP4"))
        self.assertTrue(is_supported_video("B.Mov"))

    def test_unsupported_extensions_rejected(self) -> None:
        self.assertFalse(is_supported_video("notes.txt"))
        self.assertFalse(is_supported_video("image.png"))
        self.assertFalse(is_supported_video("no_extension"))
        self.assertFalse(is_supported_video("archive.mp4.bak"))


class MediaKeyTests(unittest.TestCase):
    def test_key_ignores_case_and_separators(self) -> None:
        a = media_key(r"C:\Media\Clip01.MP4")
        b = media_key(r"c:/media/clip01.mp4")
        self.assertEqual(a, b)

    def test_key_differs_for_different_files(self) -> None:
        self.assertNotEqual(media_key("a.mp4"), media_key("b.mp4"))


class CollectVideoPathsTests(TempFilesTestCase):
    def test_collects_recursively_and_skips_non_video(self) -> None:
        first = self.touch("a.mp4")
        second = self.touch("b.mov", self.root / "sub")
        third = self.touch("c.mkv", self.root / "sub" / "deep")
        self.touch("notes.txt")
        self.touch("picture.png", self.root / "sub")
        self.touch("raw.mxf.bak")

        collected = collect_video_paths([self.root])
        keys = {media_key(item) for item in collected}
        self.assertEqual(
            keys,
            {media_key(first), media_key(second), media_key(third)},
        )

    def test_accepts_single_file_and_missing_paths(self) -> None:
        single = self.touch("only.mp4")
        self.assertEqual(collect_video_paths([single]), [str(single.resolve())])
        missing = self.root / "ghost.mp4"
        self.assertEqual(collect_video_paths([missing]), [])

    def test_duplicate_spellings_deduplicated(self) -> None:
        single = self.touch("dup.mp4")
        collected = collect_video_paths([single, self.root / "dup.MP4", single])
        self.assertEqual(len(collected), 1)


class AddPathsTests(TempFilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.model = MediaPoolModel()

    def test_add_single_file_creates_pending_ungrouped_clip(self) -> None:
        path = self.touch("clip.mp4")
        added = self.model.add_paths([path])
        self.assertEqual(len(added), 1)
        clip = added[0]
        self.assertEqual(clip.bin_id, None)
        self.assertEqual(clip.status, "pending")
        self.assertEqual(clip.error, "")
        self.assertEqual(clip.name, "clip.mp4")
        self.assertIn(clip.key, self.model.clips)

    def test_add_same_path_twice_deduplicated(self) -> None:
        path = self.touch("same.mov")
        first = self.model.add_paths([path])
        second = self.model.add_paths([self.root / "same.MOV"])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(self.model.clips), 1)

    def test_same_filename_in_different_directories_both_added(self) -> None:
        dir_a = self.root / "cam_a"
        dir_b = self.root / "cam_b"
        self.touch("take01.mp4", dir_a)
        self.touch("take01.mp4", dir_b)
        added = self.model.add_paths([dir_a, dir_b])
        self.assertEqual(len(added), 2)
        self.assertNotEqual(added[0].key, added[1].key)
        self.assertEqual({clip.name for clip in added}, {"take01.mp4"})

    def test_add_into_bin_and_reject_invalid_targets(self) -> None:
        media_bin = self.model.create_bin("B-Roll")
        path = self.touch("b1.mp4")
        added = self.model.add_paths([path], bin_id=media_bin.id)
        self.assertEqual(added[0].bin_id, media_bin.id)
        self.touch("b2.mp4")
        self.touch("b3.mp4")
        with self.assertRaises(ValueError):
            self.model.add_paths([self.root / "b2.mp4"], bin_id=ALL_BIN_ID)
        with self.assertRaises(ValueError):
            self.model.add_paths([self.root / "b3.mp4"], bin_id="missing-bin")
        self.assertEqual(len(self.model.clips), 1)


class BinManagementTests(TempFilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.model = MediaPoolModel()

    def test_create_bin(self) -> None:
        media_bin = self.model.create_bin("  rushes  ")
        self.assertEqual(media_bin.name, "rushes")
        self.assertIs(self.model.bins[media_bin.id], media_bin)

    def test_create_duplicate_name_case_insensitive(self) -> None:
        self.model.create_bin("Rushes")
        with self.assertRaises(ValueError):
            self.model.create_bin("rushes")

    def test_create_empty_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.model.create_bin("   ")

    def test_rename_bin(self) -> None:
        media_bin = self.model.create_bin("old")
        renamed = self.model.rename_bin(media_bin.id, " new ")
        self.assertEqual(renamed.name, "new")

    def test_rename_to_own_name_allowed(self) -> None:
        media_bin = self.model.create_bin("keep")
        self.assertEqual(self.model.rename_bin(media_bin.id, "KEEP").name, "KEEP")

    def test_rename_to_existing_name_rejected(self) -> None:
        first = self.model.create_bin("one")
        self.model.create_bin("two")
        with self.assertRaises(ValueError):
            self.model.rename_bin(first.id, "TWO")

    def test_rename_missing_or_virtual_bin_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.model.rename_bin("nope", "x")
        with self.assertRaises(ValueError):
            self.model.rename_bin(ALL_BIN_ID, "x")
        with self.assertRaises(ValueError):
            self.model.rename_bin(UNGROUPED_BIN_ID, "x")

    def test_delete_bin_returns_clips_to_ungrouped(self) -> None:
        media_bin = self.model.create_bin("temp")
        first = self.touch("a.mp4")
        second = self.touch("b.mp4")
        self.model.add_paths([first, second], bin_id=media_bin.id)
        self.model.delete_bin(media_bin.id)
        self.assertNotIn(media_bin.id, self.model.bins)
        ungrouped = self.model.clips_for_bin(UNGROUPED_BIN_ID)
        self.assertEqual(len(ungrouped), 2)
        self.assertTrue(all(clip.bin_id is None for clip in ungrouped))

    def test_delete_missing_or_virtual_bin_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.model.delete_bin("nope")
        with self.assertRaises(ValueError):
            self.model.delete_bin(ALL_BIN_ID)
        with self.assertRaises(ValueError):
            self.model.delete_bin(UNGROUPED_BIN_ID)

    def test_delete_bin_does_not_touch_other_bins(self) -> None:
        keep = self.model.create_bin("keep")
        gone = self.model.create_bin("gone")
        self.model.add_paths([self.touch("a.mp4")], bin_id=keep.id)
        self.model.add_paths([self.touch("b.mp4")], bin_id=gone.id)
        self.model.delete_bin(gone.id)
        kept = self.model.clips_for_bin(keep.id)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].bin_id, keep.id)


class MoveRemoveTests(TempFilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.model = MediaPoolModel()
        self.clip_a = self.touch("a.mp4")
        self.clip_b = self.touch("b.mp4")
        added = self.model.add_paths([self.clip_a, self.clip_b])
        self.key_a, self.key_b = (clip.key for clip in added)

    def test_move_clips_between_bins(self) -> None:
        media_bin = self.model.create_bin("archive")
        self.model.move_clips([self.key_a], media_bin.id)
        self.assertEqual(self.model.clips[self.key_a].bin_id, media_bin.id)
        self.assertIsNone(self.model.clips[self.key_b].bin_id)

    def test_move_to_ungrouped(self) -> None:
        media_bin = self.model.create_bin("archive")
        self.model.move_clips([self.key_a], media_bin.id)
        self.model.move_clips([self.key_a], UNGROUPED_BIN_ID)
        self.assertIsNone(self.model.clips[self.key_a].bin_id)

    def test_move_to_all_rejected_and_unknown_keys_ignored(self) -> None:
        with self.assertRaises(ValueError):
            self.model.move_clips([self.key_a], ALL_BIN_ID)
        self.model.move_clips(["ghost-key"], None)
        self.assertIn(self.key_a, self.model.clips)

    def test_remove_clips_keeps_files_on_disk(self) -> None:
        self.model.remove_clips([self.key_a])
        self.assertNotIn(self.key_a, self.model.clips)
        self.assertIn(self.key_b, self.model.clips)
        self.assertTrue(self.clip_a.is_file())
        self.assertTrue(self.clip_b.is_file())

    def test_remove_unknown_key_is_noop(self) -> None:
        self.model.remove_clips(["ghost-key"])
        self.assertEqual(len(self.model.clips), 2)


class ClipsForBinTests(TempFilesTestCase):
    def test_bin_views(self) -> None:
        model = MediaPoolModel()
        media_bin = model.create_bin("group")
        in_bin = model.add_paths([self.touch("in.mp4")], bin_id=media_bin.id)[0]
        ungrouped = model.add_paths([self.touch("out.mp4")])[0]

        self.assertEqual(
            {clip.key for clip in model.clips_for_bin(ALL_BIN_ID)},
            {in_bin.key, ungrouped.key},
        )
        self.assertEqual(
            [clip.key for clip in model.clips_for_bin(UNGROUPED_BIN_ID)],
            [ungrouped.key],
        )
        self.assertEqual(
            [clip.key for clip in model.clips_for_bin(media_bin.id)],
            [in_bin.key],
        )
        with self.assertRaises(ValueError):
            model.clips_for_bin("missing-bin")


class SearchAndSortTests(TempFilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.model = MediaPoolModel()
        group = self.model.create_bin("group")
        clips = {}
        for name, directory in (("Alpha_Interview.mp4", self.root), ("beta_tour.mp4", self.root / "sub")):
            path = self.touch(name, directory)
            clips[name] = self.model.add_paths([path], bin_id=group.id)[0]
        clips["Gamma_High.mp4"] = self.model.add_paths(
            [self.touch("Gamma_High.mp4")], bin_id=UNGROUPED_BIN_ID
        )[0]
        self.clips = clips

    def set_info(self, name: str, **kwargs) -> None:
        self.clips[name].info = make_info(self.clips[name].path, **kwargs)

    def test_search_matches_name_case_insensitive(self) -> None:
        found = self.model.visible_clips(search="  ALPHA ")
        self.assertEqual([clip.name for clip in found], ["Alpha_Interview.mp4"])
        self.assertEqual(len(self.model.visible_clips(search="nothing")), 0)

    def test_search_scoped_to_bin(self) -> None:
        group = next(iter(self.model.bins.values()))
        found = self.model.visible_clips(group.id, search="mp4")
        self.assertEqual(len(found), 2)

    def test_sort_by_name_string(self) -> None:
        names = [clip.name for clip in self.model.visible_clips(sort_key="name")]
        self.assertEqual(names, sorted(names, key=str.casefold))
        reversed_names = [clip.name for clip in self.model.visible_clips(sort_key="name", reverse=True)]
        self.assertEqual(reversed_names, sorted(names, key=str.casefold, reverse=True))

    def test_sort_numeric_duration_with_missing_info_sentinel(self) -> None:
        self.set_info("Alpha_Interview.mp4", duration=30.0)
        self.set_info("beta_tour.mp4", duration=10.0)
        ordered = self.model.visible_clips(sort_key="duration")
        self.assertEqual(
            [clip.name for clip in ordered],
            ["Gamma_High.mp4", "beta_tour.mp4", "Alpha_Interview.mp4"],
        )

    def test_sort_by_resolution_uses_pixel_area(self) -> None:
        self.set_info("Alpha_Interview.mp4", width=1920, height=1080)
        self.set_info("beta_tour.mp4", width=2560, height=1440)
        self.set_info("Gamma_High.mp4", width=3840, height=2160)
        ordered = self.model.visible_clips(sort_key="resolution")
        self.assertEqual(
            [clip.name for clip in ordered],
            ["Alpha_Interview.mp4", "beta_tour.mp4", "Gamma_High.mp4"],
        )

    def test_sort_by_fps(self) -> None:
        self.set_info("Alpha_Interview.mp4", fps=23.976)
        self.set_info("beta_tour.mp4", fps=59.94)
        self.set_info("Gamma_High.mp4", fps=25.0)
        ordered = self.model.visible_clips(sort_key="fps")
        self.assertEqual(
            [clip.name for clip in ordered],
            ["Alpha_Interview.mp4", "Gamma_High.mp4", "beta_tour.mp4"],
        )

    def test_sort_by_codec_string(self) -> None:
        self.set_info("Alpha_Interview.mp4", video_codec="h264")
        self.set_info("beta_tour.mp4", video_codec="H265")
        self.set_info("Gamma_High.mp4", video_codec="prores")
        ordered = self.model.visible_clips(sort_key="codec")
        self.assertEqual(
            [clip.name for clip in ordered],
            ["Alpha_Interview.mp4", "beta_tour.mp4", "Gamma_High.mp4"],
        )

    def test_sort_by_status(self) -> None:
        self.clips["Alpha_Interview.mp4"].status = "ready"
        ordered = self.model.visible_clips(sort_key="status")
        self.assertEqual(
            [clip.name for clip in ordered],
            ["beta_tour.mp4", "Gamma_High.mp4", "Alpha_Interview.mp4"],
        )

    def test_unknown_sort_key_falls_back_to_name(self) -> None:
        self.set_info("Alpha_Interview.mp4", duration=999.0)
        by_name = [clip.name for clip in self.model.visible_clips(sort_key="name")]
        by_unknown = [clip.name for clip in self.model.visible_clips(sort_key="nonsense")]
        self.assertEqual(by_name, by_unknown)


class OfflineRestoreTests(TempFilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.model = MediaPoolModel()
        self.path = self.touch("online.mp4")
        (self.clip,) = self.model.add_paths([self.path])

    def test_missing_file_marked_offline(self) -> None:
        self.path.unlink()
        self.model.refresh_file_states()
        self.assertEqual(self.clip.status, "offline")
        self.assertNotEqual(self.clip.error, "")

    def test_restored_file_returns_to_pending(self) -> None:
        self.path.unlink()
        self.model.refresh_file_states()
        self.path.write_bytes(b"")
        self.model.refresh_file_states()
        self.assertEqual(self.clip.status, "pending")
        self.assertEqual(self.clip.error, "")

    def test_healthy_clip_stays_pending(self) -> None:
        self.model.refresh_file_states()
        self.assertEqual(self.clip.status, "pending")
        self.assertEqual(self.clip.error, "")

    def test_offline_clip_stays_offline_while_missing(self) -> None:
        self.path.unlink()
        self.model.refresh_file_states()
        self.model.refresh_file_states()
        self.assertEqual(self.clip.status, "offline")


if __name__ == "__main__":
    unittest.main()
