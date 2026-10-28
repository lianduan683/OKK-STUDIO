"""Non-system cache selection without scanning or altering persistent settings."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cache_paths


class CachePathTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'SystemDrive':'C:'})
        self.environment.start();self.addCleanup(self.environment.stop)
        os.environ.pop('OKK_CACHE_DIR',None)
        self.memo=patch.multiple(cache_paths,_selected_root=None,_selection_key=None)
        self.memo.start();self.addCleanup(self.memo.stop)
        self.drives=patch.object(cache_paths,'_logical_fixed_drives',return_value=[Path('C:/'),Path('D:/'),Path('E:/')])
        self.fixed=self.drives.start();self.addCleanup(self.drives.stop)
        self.writable=patch.object(cache_paths,'_ensure_writable')
        self.probe=self.writable.start();self.addCleanup(self.writable.stop)

    def test_default_prefers_d_even_when_fixed_drives_are_not_sorted(self):
        self.fixed.return_value=[Path('E:/'),Path('C:/'),Path('D:/')]
        self.assertEqual(cache_paths.cache_root(),Path('D:/OKK STUDIO Cache'))
        self.probe.assert_called_once_with(Path('D:/OKK STUDIO Cache'))

    def test_unwritable_d_falls_back_to_another_fixed_non_system_drive(self):
        def writable(path):
            if path.drive=='D:':raise PermissionError('fixture read-only D')
        self.probe.side_effect=writable
        self.assertEqual(cache_paths.cache_root(),Path('E:/OKK STUDIO Cache'))
        self.assertEqual([call.args[0].drive for call in self.probe.call_args_list],['D:','E:'])

    def test_no_d_uses_other_non_system_fixed_drive(self):
        self.fixed.return_value=[Path('C:/'),Path('E:/')]
        self.assertEqual(cache_paths.cache_root(),Path('E:/OKK STUDIO Cache'))

    def test_only_system_drive_raises_without_writing_there(self):
        self.fixed.return_value=[Path('C:/')]
        with self.assertRaisesRegex(ValueError,'找不到可写的非系统盘'):cache_paths.cache_root()
        self.probe.assert_not_called()

    def test_all_non_system_drives_unwritable_never_falls_back_to_c(self):
        self.probe.side_effect=PermissionError('fixture unavailable')
        with self.assertRaisesRegex(ValueError,'未使用 C 盘'):cache_paths.cache_root()
        self.assertEqual([call.args[0].drive for call in self.probe.call_args_list],['D:','E:'])

    def test_explicit_override_can_select_another_fixed_non_system_root(self):
        os.environ['OKK_CACHE_DIR']='E:/Video Cache'
        self.assertEqual(cache_paths.cache_root(),Path('E:/Video Cache'))
        self.probe.assert_called_once_with(Path('E:/Video Cache'))

    def test_override_rejects_c_actual_system_drive_relative_and_network_paths(self):
        for override,system in (('C:/Cache','C:'),('D:/Cache','D:'),('relative/cache','C:'),
                                ('//server/share/cache','C:')):
            with self.subTest(override=override,system=system):
                os.environ['SystemDrive']=system;os.environ['OKK_CACHE_DIR']=override
                with self.assertRaises(ValueError):cache_paths.cache_root()
        self.probe.assert_not_called()

    def test_explicit_unwritable_override_reports_error_without_trying_other_drives(self):
        os.environ['OKK_CACHE_DIR']='E:/Chosen Cache'
        self.probe.side_effect=PermissionError('fixture denied')
        with self.assertRaisesRegex(ValueError,'指定缓存目录无法写入'):cache_paths.cache_root()
        self.probe.assert_called_once_with(Path('E:/Chosen Cache'))

    def test_memoized_selection_is_rechecked_and_failure_is_explicit(self):
        selected=cache_paths.cache_root()
        self.probe.reset_mock();self.probe.side_effect=PermissionError('fixture disconnected')
        with self.assertRaisesRegex(ValueError,'已选缓存目录无法写入'):cache_paths.cache_root()
        self.probe.assert_called_once_with(selected)
        self.fixed.assert_called_once()

    def test_changing_override_reselects_cache_root(self):
        self.assertEqual(cache_paths.cache_root().drive,'D:')
        os.environ['OKK_CACHE_DIR']='E:/Explicit Cache'
        self.assertEqual(cache_paths.cache_root(),Path('E:/Explicit Cache'))
        self.assertEqual(self.fixed.call_count,2)

    def test_safe_categories_are_created_beneath_the_selected_root(self):
        for kind in ('stabilization/image','stabilization/gyroflow','previews/transitions',
                     'previews/effects','temporary','audio_sync','models/faster-whisper'):
            with self.subTest(kind=kind):
                self.assertEqual(cache_paths.cache_dir(kind),Path('D:/OKK STUDIO Cache')/kind)
        self.assertTrue(all(call.args[0].drive=='D:' for call in self.probe.call_args_list))

    def test_invalid_categories_are_rejected_before_root_selection(self):
        for kind in (None,'','/absolute','C:/cache','../escape','x/../escape','x//y',
                     'x\\y','x/','./x','x.y','x\nname','CON','models/NUL','LPT1'):
            with self.subTest(kind=kind),self.assertRaises(ValueError):cache_paths.cache_dir(kind)
        self.fixed.assert_not_called();self.probe.assert_not_called()

    def test_initialize_changes_only_current_process_temp_configuration(self):
        previous=tempfile.tempdir;self.addCleanup(setattr,tempfile,'tempdir',previous)
        root=cache_paths.initialize_cache_policy()
        expected=str(root/'temporary')
        self.assertEqual(tempfile.tempdir,expected)
        self.assertEqual(tempfile.gettempdir(),expected)
        self.assertEqual(os.environ['TMP'],expected)
        self.assertEqual(os.environ['TEMP'],expected)
        self.assertEqual(root,Path('D:/OKK STUDIO Cache'))

    def test_import_does_not_create_delete_or_select_cache_directories(self):
        before=dict(os.environ);previous=tempfile.tempdir
        with patch.object(Path,'mkdir') as mkdir,patch.object(Path,'unlink') as unlink,\
                patch.object(Path,'open') as opened:
            state=runpy.run_path(str(Path(cache_paths.__file__)),run_name='cache_paths_import_fixture')
        mkdir.assert_not_called();unlink.assert_not_called();opened.assert_not_called()
        self.assertIsNone(state['_selected_root'])
        self.assertEqual(dict(os.environ),before);self.assertEqual(tempfile.tempdir,previous)


class NativeDriveEnumerationTests(unittest.TestCase):
    def test_windows_api_only_returns_fixed_drives(self):
        logical=Mock(return_value=sum(1<<(ord(letter)-65) for letter in 'CDEF'))
        drive_type=Mock(side_effect=lambda root:3 if root[0] in 'CDE' else 2)
        kernel=SimpleNamespace(GetLogicalDrives=logical,GetDriveTypeW=drive_type)
        with patch.object(cache_paths.os,'name','nt'),patch.object(cache_paths.ctypes,'WinDLL',return_value=kernel,create=True):
            roots=cache_paths._logical_fixed_drives()
        self.assertEqual(roots,[Path('C:/'),Path('D:/'),Path('E:/')])
        self.assertEqual(drive_type.call_count,4)

    def test_enumeration_failure_is_explicit(self):
        kernel=SimpleNamespace(GetLogicalDrives=Mock(return_value=0),GetDriveTypeW=Mock())
        with patch.object(cache_paths.os,'name','nt'),patch.object(cache_paths.ctypes,'WinDLL',return_value=kernel,create=True):
            with self.assertRaisesRegex(ValueError,'无法枚举'):cache_paths._logical_fixed_drives()


if __name__=='__main__':unittest.main()
