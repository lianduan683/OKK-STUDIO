"""Recorded identities are read from video metadata, never file names/codecs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import camera_metadata as module
from camera_metadata import CameraMetadata,display_camera_metadata,normalize_model_label,read_camera_metadata
from exporter_core import CREATE_NO_WINDOW


def atom(kind,body):return struct.pack('>I4s',len(body)+8,kind)+body
def movie(*atoms):return atom(b'ftyp',b'qt  '+b'\0'*4+b'qt  ')+b''.join(atoms)
def sony_xml(model='ILCE-7M4'):
    return (f'<?xml version="1.0" encoding="UTF-8"?><NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00">'
            f'<Device manufacturer="Sony" modelName="{model}" serialNo="123"/></NonRealTimeMeta>').encode()


class CameraMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='camera_identity_')
        self.folder=Path(self.temporary.name)
        self.no_tools=patch.object(module,'_find_tool',return_value='');self.no_tools.start()
    def tearDown(self):self.no_tools.stop();self.temporary.cleanup()
    def write(self,data,name='video.mp4'):
        path=self.folder/name;path.write_bytes(data);return path

    def test_compact_model_labels_preserve_original_metadata(self):
        for make,model,label in [('SONY','ILCE-7M4','A7M4'),('Sony','ILCE-7M5','A7M5'),
            ('SONY','ILCE-7RM5','A7RM5'),('SONY','ILME-FX3','FX3'),
            ('NIKON CORPORATION','NIKON Z 8','Z8'),('Nikon','NIKON Z 9','Z9'),
            ('Nikon','NIKON Z 6II','Z6II'),('Canon','Canon EOS R5','R5'),
            ('Panasonic','DC-GH6','GH6')]:
            with self.subTest(model=model):
                meta=CameraMetadata(make,model,'recorded')
                self.assertEqual(meta.camera_label,label)
                self.assertEqual(display_camera_metadata(meta),label)
                self.assertEqual((meta.make,meta.model),(make,model))
        self.assertEqual(CameraMetadata('Sony').camera_label,'索尼')
        self.assertEqual(CameraMetadata('Canon').camera_label,'佳能')
        self.assertEqual(CameraMetadata().camera_label,'未知相机')

    def test_embedded_sony_nrt_xml_in_uuid_is_identified(self):
        path=self.write(movie(atom(b'uuid',b'\x13'*16+sony_xml())))
        before=path.read_bytes()
        meta=read_camera_metadata(path)
        self.assertEqual((meta.make,meta.model,meta.camera_label),('Sony','ILCE-7M4','A7M4'))
        self.assertTrue(meta.source.startswith('embedded:'))
        self.assertEqual(path.read_bytes(),before)

    def test_quicktime_user_data_make_and_model_boxes(self):
        def text(value):
            raw=value.encode();return struct.pack('>HH',len(raw),0)+raw
        data=movie(atom(b'moov',atom(b'udta',atom(b'\xa9mak',text('NIKON CORPORATION'))+
                                     atom(b'\xa9mod',text('NIKON Z 9')))))
        meta=read_camera_metadata(self.write(data))
        self.assertEqual((meta.make,meta.model,meta.camera_label),('NIKON CORPORATION','NIKON Z 9','Z9'))
        self.assertEqual(meta.source,'embedded:QuickTime')

    def test_embedded_tiff_identity_is_parsed_structurally(self):
        make=b'Canon\0';model=b'Canon EOS R6 Mark II\0'
        first=8+2+2*12+4
        tiff=b'II*\0'+struct.pack('<I',8)+struct.pack('<H',2)
        tiff+=struct.pack('<HHII',0x10f,2,len(make),first)
        tiff+=struct.pack('<HHII',0x110,2,len(model),first+len(make))+struct.pack('<I',0)+make+model
        path=self.write(movie(atom(b'uuid',b'\x14'*16+b'Exif\0\0'+tiff)))
        meta=read_camera_metadata(path)
        self.assertEqual((meta.make,meta.model,meta.camera_label),('Canon','Canon EOS R6 Mark II','R6 Mark II'))
        self.assertEqual(meta.source,'embedded:TIFF')

    def test_model_tag_without_make_can_use_only_a_reliable_recorded_model_family(self):
        result=module._tags({'model':'ILCE-7M5'},'recorded-tag')
        self.assertEqual((result.make,result.camera_label),('Sony','A7M5'))
        self.assertEqual(module._tags({'codec_name':'h264','filename':'SONY_A7M4.mp4'},'no-evidence'),CameraMetadata())

    def test_sony_matching_sidecar_and_sidecar_changes_are_detected(self):
        path=self.write(movie(atom(b'mdat',b'pixels')),'C0001.mp4')
        self.assertEqual(read_camera_metadata(path).camera_label,'未知相机')
        sidecar=path.with_name('C0001M01.XML');sidecar.write_bytes(sony_xml('ILME-FX3'))
        meta=read_camera_metadata(path)
        self.assertEqual(meta.camera_label,'FX3');self.assertTrue(meta.source.startswith('sidecar:'))
        sidecar.write_bytes(sony_xml('ILME-FX30'))
        self.assertEqual(read_camera_metadata(path).camera_label,'FX30')

    def test_xml_with_arbitrary_camera_words_or_entities_is_rejected(self):
        path=self.write(movie(atom(b'mdat',b'pixels')))
        sidecar=path.with_suffix('.XML')
        sidecar.write_text('<Description>SONY ILCE-7M4 Nikon Z9 Canon R5</Description>',encoding='utf-8')
        self.assertEqual(read_camera_metadata(path).camera_label,'未知相机')
        sidecar.write_bytes(b'<!DOCTYPE NonRealTimeMeta [<!ENTITY x "Sony">]>'+sony_xml())
        self.assertEqual(read_camera_metadata(path).camera_label,'未知相机')

    def test_embedded_xmp_tiff_fields_are_valid_identity(self):
        xml=(b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
             b'<rdf:Description xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Make="Nikon" tiff:Model="NIKON Z 8"/>'
             b'</rdf:RDF></x:xmpmeta>')
        meta=read_camera_metadata(self.write(movie(atom(b'uuid',b'\x11'*16+xml))))
        self.assertEqual((meta.make,meta.camera_label),('Nikon','Z8'))

    def test_camera_words_in_mdat_and_file_name_are_never_evidence(self):
        data=movie(atom(b'mdat',b'SONY\0NIKON\0CANON\0'+sony_xml()))
        path=self.write(data,'SONY_A7M4_NIKON_Z8_CANON_R5.mp4')
        self.assertEqual(read_camera_metadata(path),CameraMetadata())

    def test_large_pixel_box_is_skipped_without_scanning_its_contents(self):
        path=self.folder/'large.mp4'
        with path.open('wb') as file:
            file.write(movie())
            file.write(struct.pack('>I4sQ',1,b'mdat',128*1024*1024))
            file.seek(128*1024*1024+len(movie()))
            file.write(atom(b'uuid',b'\x12'*16+sony_xml('ILCE-7RM5')))
        began=time.monotonic();meta=read_camera_metadata(path)
        self.assertEqual(meta.camera_label,'A7RM5')
        self.assertLess(time.monotonic()-began,1.)

    def test_malformed_atom_and_missing_files_return_unknown(self):
        self.assertEqual(read_camera_metadata(self.folder/'missing.mp4'),CameraMetadata())
        for content in (b'',b'\xff'*20,struct.pack('>I4s',4,b'moov'),movie(atom(b'moov',b'bad'))):
            self.assertEqual(read_camera_metadata(self.write(content)),CameraMetadata())

    def test_probe_format_stream_vendor_tags_and_exiftool_family_tags(self):
        tags=[({'com.apple.quicktime.make':'SONY','com.apple.quicktime.model':'ILCE-7M4'},'A7M4'),
              ({'manufacturer':'NIKON CORPORATION','MODEL':'NIKON Z 8'},'Z8'),
              ({'Canon.Model':'Canon EOS R5'},'R5'),
              ({'IFD0:Make':'Canon','IFD0:Model':'Canon EOS R6'},'R6'),
              ({'SonyNonRealTimeMetaDeviceManufacturer':'Sony','SonyNonRealTimeMetaDeviceModelName':'ILME-FX3'},'FX3')]
        for values,label in tags:
            with self.subTest(values=values):self.assertEqual(module._tags(values,'probe').camera_label,label)
        payload={'format':{'tags':{'manufacturer':'Sony'}},'streams':[{'tags':{'model':'ILCE-7M5'}}]}
        with patch.object(module,'_tool_json',return_value=payload):
            meta=module._ffprobe(Path('not-used'),'ffprobe',None)
        self.assertEqual(meta.camera_label,'A7M5');self.assertIn('stream0',meta.source)

    def test_cached_probe_is_invalidated_by_source_replacement(self):
        path=self.write(movie())
        with patch.object(module,'_ffprobe',return_value=CameraMetadata('Sony','ILCE-7M4','fixture')) as probe:
            self.assertEqual(read_camera_metadata(path).camera_label,'A7M4')
            read_camera_metadata(path);self.assertEqual(probe.call_count,1)
            path.write_bytes(path.read_bytes()+b'changed')
            read_camera_metadata(path);self.assertEqual(probe.call_count,2)

    def test_cancel_does_no_probe_and_running_tool_is_terminated(self):
        path=self.write(movie());event=threading.Event();event.set()
        with patch.object(module,'_ffprobe') as probe:
            self.assertEqual(read_camera_metadata(path,cancel_event=event),CameraMetadata())
        probe.assert_not_called()
        event.clear();timer=threading.Timer(.05,event.set);timer.start()
        began=time.monotonic()
        try:self.assertIsNone(module._tool_json([sys.executable,'-c','import time;time.sleep(10)'],event,timeout=1))
        finally:timer.cancel()
        self.assertLess(time.monotonic()-began,1.)


class RealContainerCameraTagsTests(unittest.TestCase):
    def test_optional_official_exiftool_reads_real_camera_identity_as_fallback(self):
        app=Path(__file__).resolve().parents[1]
        executable=Path(os.environ.get('EXIFTOOL_PATH') or app/'bin'/'exiftool'/'exiftool.exe')
        ffmpeg=app/'bin'/'ffmpeg.exe'
        if not executable.is_file() or not ffmpeg.is_file():self.skipTest('optional bundled ExifTool unavailable')
        with tempfile.TemporaryDirectory(prefix='exiftool_camera_fixture_') as temporary:
            path=Path(temporary)/'unrelated_file_name.mov'
            result=subprocess.run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i','color=gray:s=128x72:r=5:d=0.4',
                '-an','-c:v','libx264','-preset','ultrafast','-crf','23','-threads','1',str(path)],
                capture_output=True,timeout=20,creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
            result=subprocess.run([str(executable),'-overwrite_original','-QuickTime:Make=Sony',
                '-QuickTime:Model=ILCE-7M5',str(path)],capture_output=True,timeout=20,creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
            before=path.read_bytes()
            # Simulate a proprietary atom not exposed by the primary readers.
            # The fallback itself must extract real bytes through official CLI.
            with patch.dict(os.environ,{'EXIFTOOL_PATH':str(executable)}), \
                    patch.object(module,'_ffprobe',return_value=CameraMetadata()), \
                    patch.object(module,'_embedded',return_value=CameraMetadata()):
                meta=read_camera_metadata(path)
            self.assertEqual((meta.make,meta.model,meta.camera_label,meta.source),('Sony','ILCE-7M5','A7M5','exiftool'))
            self.assertEqual(path.read_bytes(),before)

    def test_real_ffmpeg_written_camera_tags_from_video_file(self):
        app=Path(__file__).resolve().parents[1]
        ffmpeg,ffprobe=(app/'bin'/name for name in ('ffmpeg.exe','ffprobe.exe'))
        if not all(path.is_file() for path in (ffmpeg,ffprobe)):self.skipTest('bundled FFmpeg unavailable')
        with tempfile.TemporaryDirectory(prefix='recorded_camera_tags_') as temporary:
            folder=Path(temporary)
            for index,(make_tag,make,model_tag,model,label) in enumerate((
                ('com.apple.quicktime.make','SONY','com.apple.quicktime.model','ILCE-7M4','A7M4'),
                ('manufacturer','NIKON CORPORATION','model','NIKON Z 8','Z8'),
                ('make','Canon','Canon.model','Canon EOS R5','R5'),
            )):
                with self.subTest(label=label):
                    path=folder/f'arbitrary_name_{index}.mov'
                    command=[str(ffmpeg),'-v','error','-y','-f','lavfi','-i','color=gray:s=128x72:r=5:d=0.4',
                        '-an','-c:v','libx264','-preset','ultrafast','-crf','23','-threads','1',
                        '-metadata',make_tag+'='+make,'-metadata',model_tag+'='+model,
                        '-movflags','use_metadata_tags',str(path)]
                    result=subprocess.run(command,capture_output=True,timeout=20,creationflags=CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
                    before=path.read_bytes();meta=read_camera_metadata(path,str(ffprobe))
                    self.assertEqual((meta.make,meta.model,meta.camera_label),(make,model,label))
                    self.assertTrue(meta.source.startswith('ffprobe:'),meta)
                    self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
