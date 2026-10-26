"""Read camera identity from recorded metadata without guessing from names.

FFprobe covers standard container/stream tags. A bounded ISO-BMFF reader
handles QuickTime make/model, embedded Sony NRT XML, XMP and TIFF identity.
An optional official ExifTool executable adds vendor-specific metadata support.
Only matching sidecars are considered, and all operations are read-only.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

from exporter_core import CREATE_NO_WINDOW


MAX_METADATA_BYTES = 4*1024*1024
MAX_ATOM_BYTES = 1024*1024
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_MAKERS = ((('sony','索尼'), '索尼'), (('nikon','尼康'), '尼康'),
           (('canon','佳能'), '佳能'), (('panasonic','lumix','松下'), '松下'),
           (('fujifilm','fuji photo','富士'), '富士'), (('dji','大疆'), '大疆'),
           (('gopro',), 'GoPro'), (('insta360','影石'), '影石'),
           (('blackmagic',), 'Blackmagic'), (('apple',), '苹果'),
           (('olympus','om digital','om system','奥林巴斯'), '奥林巴斯'),
           (('red digital','red.com'), 'RED'), (('arri',), 'ARRI'),
           (('samsung','三星'), '三星'), (('huawei','华为'), '华为'))


def _clean(value):
    if not isinstance(value,str): return ''
    value=' '.join(re.sub(r'[\x00-\x1f\x7f]',' ',value).split()).strip()
    return value[:160] if value.lower() not in {'unknown','none','n/a','null','undefined'} else ''


def _brand(make):
    value=_clean(make).casefold()
    for aliases,label in _MAKERS:
        if any(alias in value for alias in aliases): return label
    return _clean(make)


def _infer_make(model):
    """Only recognizable identity values from a model tag, never filenames."""
    model=_clean(model)
    if re.match(r'^(?:SONY\s+)?(?:ILCE-|ILME-|DSC-|NEX-)',model,re.I): return 'Sony'
    for name in ('Nikon','Canon','Panasonic','Fujifilm','GoPro','Blackmagic','Apple','DJI','Insta360','ARRI'):
        if model.casefold().startswith(name.casefold()+' '): return name
    if re.match(r'^EOS\s+[A-Z0-9]',model,re.I): return 'Canon'
    return ''


def normalize_model_label(make,model):
    """Compact display identity; the original make/model remain unchanged."""
    model=_clean(model)
    brand=_brand(make or _infer_make(model))
    if brand=='索尼':
        model=re.sub(r'^SONY\s+','',model,flags=re.I)
        if re.match(r'^ILCE[-_ ]',model,re.I): return 'A'+re.sub(r'^ILCE[-_ ]+','',model,flags=re.I).replace(' ','').upper()
        model=re.sub(r'^(?:ILME|DSC)[-_ ]+','',model,flags=re.I)
    elif brand=='尼康':
        model=re.sub(r'^NIKON(?:\s+CORPORATION)?\s+','',model,flags=re.I)
        if re.match(r'^Z\s*\d',model,re.I): model=re.sub(r'\s+','',model).upper()
    elif brand=='佳能':
        model=re.sub(r'^CANON\s+','',model,flags=re.I)
        model=re.sub(r'^EOS\s+','',model,flags=re.I)
    elif brand=='松下':
        model=re.sub(r'^(?:PANASONIC|LUMIX)\s+','',model,flags=re.I)
        model=re.sub(r'^(?:DMC|DC)-','',model,flags=re.I)
    else:
        clean_make=_clean(make)
        if clean_make and model.casefold().startswith(clean_make.casefold()+' '):
            model=model[len(clean_make):].lstrip()
    return model or brand or '未知相机'


@dataclass(frozen=True,slots=True)
class CameraMetadata:
    make: str=''
    model: str=''
    source: str=''

    @property
    def camera_label(self): return normalize_model_label(self.make,self.model)


def display_camera_metadata(meta):
    return normalize_model_label(getattr(meta,'make',''),getattr(meta,'model',''))


def _cancelled(event): return event is not None and event.is_set()


def _merge(first,second):
    if not second.make and not second.model: return first
    if not first.make and not first.model: return second
    make=first.make or second.make
    # Never pair an unrelated sidecar's model with an explicit other maker.
    compatible=not first.make or not second.make or _brand(first.make)==_brand(second.make)
    model=first.model or (second.model if compatible else '')
    source=first.source
    if (make,model)!=(first.make,first.model) and second.source!=source:
        source='+'.join(filter(None,(source,second.source)))
    return CameraMetadata(make,model,source)


def _tags(tags,source):
    if not isinstance(tags,dict): return CameraMetadata()
    make,model='',''
    for key,value in tags.items():
        key=re.sub(r'[^a-z0-9]','',str(key).casefold())
        value=_clean(value)
        if not value: continue
        if key in {'make','manufacturer','cameramake','devicemanufacturer','cameramanufacturer',
                   'comapplequicktimemake','comapplequicktimecameramake','comapplequicktimemanufacturer',
                   'comandroidmanufacturer'} or any(
                       key.endswith(suffix) for suffix in ('devicemanufacturer','cameramanufacturer')):
            if not make: make=value
        elif key in {'model','modelname','cameramodel','cameramodelname','devicemodelname',
                     'comapplequicktimemodel','comapplequicktimecameramodel','comandroidmodel'} or any(
                       key.endswith(suffix) for suffix in ('cameramodelname','devicemodelname')):
            if not model: model=value
        # ExifTool family groups and common qualified vendor metadata tags.
        elif any(key.startswith(prefix) for prefix in ('quicktime','keys','userdata','ifd0','exif','sony','nikon','canon','panasonic','xmp')):
            if key.endswith(('make','manufacturer')) and not make: make=value
            if key.endswith(('model','modelname')) and not model: model=value
    if model and not make: make=_infer_make(model)
    return CameraMetadata(make,model,source if make or model else '')


def _tool_json(args,event,timeout=5):
    if _cancelled(event): return None
    from cache_paths import cache_dir
    with tempfile.TemporaryFile(dir=cache_dir('temporary')) as output:
        try:
            process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.DEVNULL,
                                     creationflags=CREATE_NO_WINDOW)
        except OSError: return None
        try:
            deadline=time.monotonic()+timeout
            while process.poll() is None:
                if _cancelled(event) or time.monotonic()>deadline or os.fstat(output.fileno()).st_size>MAX_METADATA_BYTES:
                    return None
                time.sleep(.02)
            if process.returncode or os.fstat(output.fileno()).st_size>MAX_METADATA_BYTES: return None
            output.seek(0)
            try: return json.loads(output.read(MAX_METADATA_BYTES).decode('utf-8-sig','replace'))
            except (ValueError,UnicodeError): return None
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=.5)


def _find_tool(name,preferred=''):
    base=Path(__file__).resolve().parent
    candidates=[preferred] if preferred else []
    if name=='exiftool':
        candidates += [os.environ.get('EXIFTOOL_PATH',''),str(base/'bin'/'exiftool'/'exiftool.exe'),str(base/'bin'/'exiftool.exe')]
    else: candidates.append(str(base/'bin'/(name+'.exe')))
    candidates += [shutil.which(name) or '']
    return next((str(Path(item).resolve()) for item in candidates if item and Path(item).is_file()),'')


def _ffprobe(path,tool,event):
    if not tool: return CameraMetadata()
    payload=_tool_json([tool,'-v','error','-probesize','2097152','-analyzeduration','1000000',
        '-show_entries','format_tags:stream_tags','-of','json',str(path)],event)
    if not isinstance(payload,dict): return CameraMetadata()
    found=_tags(payload.get('format',{}).get('tags',{}),'ffprobe:format')
    for index,stream in enumerate(payload.get('streams',[])):
        if isinstance(stream,dict): found=_merge(found,_tags(stream.get('tags',{}),f'ffprobe:stream{index}'))
    return found


def _exiftool(path,tool,event):
    if not tool: return CameraMetadata()
    payload=_tool_json([tool,'-j','-G1','-s','-fast','-Make','-Model','-CameraModelName',
        '-DeviceManufacturer','-DeviceModelName','-Manufacturer','-ModelName',str(path)],event)
    return _tags(payload[0],'exiftool') if isinstance(payload,list) and payload and isinstance(payload[0],dict) else CameraMetadata()


def _xml_metadata(data,source):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper(): return CameraMetadata()
    if data.startswith((b'\xff\xfe',b'\xfe\xff')):
        try: data=data.decode('utf-16').encode('utf-8')
        except UnicodeError:return CameraMetadata()
    candidates=[]
    for match in re.finditer(rb'<(?P<p>[A-Za-z_][\w.-]*:)?(?P<r>NonRealTimeMeta|NRTMetadataXML|xmpmeta|RDF)\b',data):
        end=data.find(b'</'+(match.group('p') or b'')+match.group('r')+b'>',match.end())
        if end>=0:candidates.append(data[match.start():end+len(match.group('p') or b'')+len(match.group('r'))+3])
    for fragment in candidates:
        try: root=ET.fromstring(fragment)
        except ET.ParseError: continue
        local=lambda value:value.rsplit('}',1)[-1].split(':')[-1].casefold()
        nrt=local(root.tag) in {'nonrealtimemeta','nrtmetadataxml'}
        for element in root.iter():
            if nrt and local(element.tag) not in {'device','camera'}: continue
            values={local(key):value for key,value in element.attrib.items()}
            if not nrt:
                for child in element:
                    if local(child.tag) in {'make','model','manufacturer','modelname'} and child.text:
                        values[local(child.tag)]=child.text
            found=_tags(values,source)
            if found.make or found.model:return found
    return CameraMetadata()


def _tiff_metadata(data,source):
    for magic in (b'II*\x00',b'MM\x00*'):
        index=data.find(magic)
        if index<0:continue
        body=data[index:];endian='<' if magic[:2]==b'II' else '>'
        try:
            offset=struct.unpack_from(endian+'I',body,4)[0]
            count=struct.unpack_from(endian+'H',body,offset)[0]
            if not 8<=offset<len(body) or count>1000:continue
            values={}
            for item in range(count):
                position=offset+2+12*item
                tag,kind,length=struct.unpack_from(endian+'HHI',body,position)
                if tag not in (0x010f,0x0110) or kind!=2 or not 1<=length<=256:continue
                start=position+8 if length<=4 else struct.unpack_from(endian+'I',body,position+8)[0]
                if start+length>len(body):continue
                values['make' if tag==0x010f else 'model']=body[start:start+length].decode('utf-8','replace').rstrip('\0')
            found=_tags(values,source)
            if found.make or found.model:return found
        except (struct.error,OverflowError):continue
    return CameraMetadata()


def _decode_atom_text(body):
    # Apple ItemList data box: size/type, data type, locale, UTF-8 value.
    if len(body)>=16 and body[4:8]==b'data': body=body[16:]
    elif len(body)>=4 and 0<struct.unpack('>H',body[:2])[0]<=len(body)-4:
        size=struct.unpack('>H',body[:2])[0];body=body[4:4+size]
    return _clean(body.decode('utf-8','replace').strip('\x00'))


def _embedded(path,event):
    found=CameraMetadata();budget=MAX_METADATA_BYTES;atoms=0
    containers={b'moov',b'udta',b'meta',b'ilst',b'trak',b'mdia',b'minf',b'stbl'}
    leaves={b'uuid',b'XMP_',b'xml ',b'nrtm',b'\xa9mak',b'\xa9mod',b'@mak',b'@mod',b'manu',b'modl',b'CMT1',b'NCDT'}
    try:
        with path.open('rb') as handle:
            size=path.stat().st_size
            def walk(start,end,depth=0):
                nonlocal found,budget,atoms
                if depth>8 or budget<=0:return
                position=start
                while position+8<=end and atoms<10000 and not _cancelled(event):
                    handle.seek(position);header=handle.read(8);atoms+=1
                    if len(header)<8:return
                    length,kind=struct.unpack('>I4s',header);head=8
                    if length==1:
                        extended=handle.read(8)
                        if len(extended)<8:return
                        length=struct.unpack('>Q',extended)[0];head=16
                    elif length==0:length=end-position
                    if length<head or position+length>end:return
                    data_start=position+head;data_size=length-head
                    if kind in containers:
                        if kind==b'meta':
                            take=min(data_size,MAX_ATOM_BYTES,budget)
                            handle.seek(data_start);body=handle.read(take);budget-=len(body)
                            found=_merge(found,_xml_metadata(body,'embedded:Sony-NRT/XML'))
                            walk(data_start+4,position+length,depth+1)
                        else:walk(data_start,position+length,depth+1)
                    elif kind in leaves and data_size<=MAX_ATOM_BYTES and data_size<=budget:
                        handle.seek(data_start);body=handle.read(data_size);budget-=len(body)
                        if kind in {b'\xa9mak',b'@mak',b'manu'}:
                            found=_merge(found,_tags({'make':_decode_atom_text(body)},'embedded:QuickTime'))
                        elif kind in {b'\xa9mod',b'@mod',b'modl'}:
                            found=_merge(found,_tags({'model':_decode_atom_text(body)},'embedded:QuickTime'))
                        else:
                            found=_merge(found,_xml_metadata(body,'embedded:Sony-NRT/XML'))
                            found=_merge(found,_tiff_metadata(body,'embedded:TIFF'))
                    if found.make and found.model:return
                    position+=length
            walk(0,size)
    except (OSError,ValueError,OverflowError,struct.error):pass
    return found


def _sidecars(path):
    return [path.with_suffix('.XML'),path.with_suffix('.xml'),
            path.with_name(path.stem+'M01.XML'),path.with_name(path.stem+'M01.xml'),
            path.with_suffix('.xmp'),path.with_suffix('.XMP')]


def read_camera_metadata(path,ffprobe_path='',*,cancel_event=None):
    if _cancelled(cancel_event):return CameraMetadata()
    try:
        path=Path(path)
        info=path.stat()
        if not path.is_file():return CameraMetadata()
        sidecars=[];stamps=[]
        for candidate in dict.fromkeys(_sidecars(path)):
            if candidate.is_file():
                stamp=candidate.stat();stamps.append((str(candidate),stamp.st_size,stamp.st_mtime_ns));sidecars.append(candidate)
        probe=_find_tool('ffprobe',ffprobe_path);exif=_find_tool('exiftool')
        key=(str(path.resolve()),info.st_size,info.st_mtime_ns,probe,exif,tuple(stamps))
    except (OSError,ValueError,TypeError):return CameraMetadata()
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key);return _CACHE[key]
    found=_ffprobe(path,probe,cancel_event)
    if not (found.make and found.model) and not _cancelled(cancel_event):found=_merge(found,_embedded(path,cancel_event))
    if not (found.make and found.model) and not _cancelled(cancel_event):found=_merge(found,_exiftool(path,exif,cancel_event))
    if not (found.make and found.model):
        for candidate in sidecars:
            if _cancelled(cancel_event):break
            try:
                if candidate.stat().st_size>MAX_ATOM_BYTES:continue
                found=_merge(found,_xml_metadata(candidate.read_bytes(),'sidecar:'+candidate.name))
            except OSError:continue
            if found.make and found.model:break
    if not _cancelled(cancel_event):
        with _CACHE_LOCK:
            _CACHE[key]=found;_CACHE.move_to_end(key)
            while len(_CACHE)>512:_CACHE.popitem(last=False)
    return found
