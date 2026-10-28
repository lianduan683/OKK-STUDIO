"""A cache proxy is bound to file identity metadata, never just C0001.mp4."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
import proxy_cache


class ProxyCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='proxy_index_')
        self.folder=Path(self.temp.name)
        self.index=self.folder/'cache'/'proxies'/'index';self.index.mkdir(parents=True)
        self.source=self.folder/"原片'机位.mp4";self.source.write_bytes(b'original camera picture')
        self.proxy=self.folder/"代理'机位.mp4";self.proxy.write_bytes(b'generated proxy')
        proxy_cache._directory_key=None;proxy_cache._directory=None;proxy_cache._records.clear()
        self.cachedir=patch.object(proxy_cache,'cache_dir',return_value=self.index)
        self.cache=self.cachedir.start()

    def tearDown(self):
        self.cachedir.stop()
        proxy_cache._directory_key=None;proxy_cache._directory=None;proxy_cache._records.clear()
        self.temp.cleanup()

    def normalized(self,path):return os.path.normcase(str(path.resolve()))

    def test_register_find_and_repeat_lookup_avoid_directory_write_probes_and_json_reloads(self):
        self.assertTrue(proxy_cache.register_proxy(self.source,self.proxy))
        proxy_cache._records.clear()
        with patch.object(proxy_cache.json,'loads',wraps=json.loads) as loads:
            for _ in range(20):self.assertEqual(proxy_cache.find_proxy(self.source),self.normalized(self.proxy))
            self.assertEqual(loads.call_count,1)
        self.assertEqual(self.cache.call_count,1)
        self.cache.assert_called_once_with('proxies/index')
        self.assertEqual(self.source.read_bytes(),b'original camera picture')

    def test_same_camera_filename_in_two_directories_never_cross_matches(self):
        first=self.folder/'shoot_a'/'C0001.mp4';first.parent.mkdir();first.write_bytes(b'same metadata')
        second=self.folder/'shoot_b'/'C0001.mp4';second.parent.mkdir();second.write_bytes(first.read_bytes())
        info=first.stat();os.utime(second,ns=(info.st_atime_ns,info.st_mtime_ns))
        other=self.folder/'other_proxy.mp4';other.write_bytes(b'second proxy')
        self.assertTrue(proxy_cache.register_proxy(first,self.proxy))
        self.assertEqual(proxy_cache.find_proxy(second),'')
        self.assertTrue(proxy_cache.register_proxy(second,other))
        self.assertEqual(proxy_cache.find_proxy(first),self.normalized(self.proxy))
        self.assertEqual(proxy_cache.find_proxy(second),self.normalized(other))
        self.assertEqual(len(list(self.index.glob('*.json'))),2)

    def test_modified_or_missing_source_and_proxy_are_rejected(self):
        for name,action in (
            ('source_size',lambda:self.source.write_bytes(b'changed source with different size')),
            ('source_mtime',lambda:os.utime(self.source,ns=(self.source.stat().st_atime_ns,self.source.stat().st_mtime_ns+1000000))),
            ('proxy_size',lambda:self.proxy.write_bytes(b'changed proxy data')),
            ('proxy_mtime',lambda:os.utime(self.proxy,ns=(self.proxy.stat().st_atime_ns,self.proxy.stat().st_mtime_ns+1000000))),
            ('missing_source',lambda:self.source.unlink()),('missing_proxy',lambda:self.proxy.unlink())):
            with self.subTest(name=name):
                self.source.write_bytes(b'original camera picture');self.proxy.write_bytes(b'generated proxy')
                self.assertTrue(proxy_cache.register_proxy(self.source,self.proxy))
                action()
                self.assertEqual(proxy_cache.find_proxy(self.source),'')

    def test_corrupt_missing_and_wrong_source_records_are_misses_and_external_repairs_are_seen(self):
        self.assertTrue(proxy_cache.register_proxy(self.source,self.proxy))
        index=next(self.index.glob('*.json'));valid=index.read_bytes()
        for damaged in (b'{broken json',b'\xff\xfe',b'[]',b'{"version":1}',
                        json.dumps({'version':1,'source':{'path':'relative','size':1,'mtime_ns':1},'proxy':{}}).encode()):
            with self.subTest(damaged=damaged):
                index.write_bytes(damaged)
                self.assertEqual(proxy_cache.find_proxy(self.source),'')
                index.write_bytes(valid)
                self.assertEqual(proxy_cache.find_proxy(self.source),self.normalized(self.proxy))
        record=json.loads(valid);record['source']['path']=self.normalized(self.folder/'other.mp4')
        index.write_text(json.dumps(record),encoding='utf-8')
        self.assertEqual(proxy_cache.find_proxy(self.source),'')
        index.unlink();self.assertEqual(proxy_cache.find_proxy(self.source),'')
        index.write_bytes(valid)
        self.assertEqual(proxy_cache.find_proxy(self.source),self.normalized(self.proxy))

    def test_failed_atomic_replace_preserves_old_binding_and_cleans_temporary_index(self):
        self.assertTrue(proxy_cache.register_proxy(self.source,self.proxy))
        index=next(self.index.glob('*.json'));before=index.read_bytes()
        alternative=self.folder/'new.mp4';alternative.write_bytes(b'completed other proxy')
        with patch.object(proxy_cache.os,'replace',side_effect=PermissionError('index temporarily locked')):
            self.assertFalse(proxy_cache.register_proxy(self.source,alternative))
        self.assertEqual(index.read_bytes(),before)
        self.assertEqual(proxy_cache.find_proxy(self.source),self.normalized(self.proxy))
        self.assertEqual(list(self.index.glob('*.tmp')),[])
        self.assertEqual(alternative.read_bytes(),b'completed other proxy')

    def test_unavailable_index_does_not_fail_completed_proxy_and_registration_retries(self):
        self.cache.side_effect=ValueError('cache drive unavailable')
        for _ in range(10):self.assertEqual(proxy_cache.find_proxy(self.source),'')
        self.assertEqual(self.cache.call_count,1)
        self.assertFalse(proxy_cache.register_proxy(self.source,self.proxy))
        self.assertEqual(self.source.read_bytes(),b'original camera picture')
        self.assertEqual(self.proxy.read_bytes(),b'generated proxy')
        self.cache.side_effect=None
        self.assertTrue(proxy_cache.register_proxy(self.source,self.proxy))
        self.assertEqual(proxy_cache.find_proxy(self.source),self.normalized(self.proxy))

    def test_register_after_miss_is_visible_and_invalid_inputs_never_raise(self):
        self.assertEqual(proxy_cache.find_proxy(self.source),'')
        self.assertTrue(proxy_cache.register_proxy(self.source,self.proxy))
        self.assertEqual(proxy_cache.find_proxy(self.source),self.normalized(self.proxy))
        self.assertFalse(proxy_cache.register_proxy(self.source,self.source))
        for missing in (self.folder/'missing.mp4',None,self.folder):
            with self.subTest(missing=missing):
                self.assertFalse(proxy_cache.register_proxy(missing,self.proxy))
                self.assertFalse(proxy_cache.register_proxy(self.source,missing))
                self.assertEqual(proxy_cache.find_proxy(missing),'')


if __name__=='__main__':unittest.main()
