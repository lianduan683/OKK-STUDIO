"""Cancellable 1080p proxies for multicamera monitoring; originals stay intact."""
from __future__ import annotations

from collections import deque
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import stat
import subprocess
import tempfile
import threading
import time

from cache_paths import cache_dir
from cache_publish import publish_completed
from exporter_core import (CREATE_NO_WINDOW, ExportCancelled, parse_progress_line, progress_seconds,
                           _media_probe_command, _media_info_from_payload)
from proxy_cache import register_proxy
from proxy_media import validate_proxy_media

PARAMETERS = {'version':3,'bounds':[1920,1080],'fps_limit':30,'codec':'h264',
              'depth':8,'gop_seconds':1,'bframes':0,'audio':'aac-128k-stereo-48k',
              'encoders':{'nvenc':'p1-cq27-zero-latency','amf':'speed-cqp27-async1',
                          'qsv':'veryfast-quality27-async1','cpu':'ultrafast-crf26-threads2'}}
BACKGROUND_FLAGS = CREATE_NO_WINDOW | (0x00004000 if os.name=='nt' else 0)


class ProxyEncodeError(RuntimeError):
    pass


def _probe_media_cancel(ffprobe,source,*,cancel_event=None):
    """Own the ffprobe process so cancellation also stops slow input probing."""
    _cancelled(cancel_event)
    process=subprocess.Popen(_media_probe_command(ffprobe,source),stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=BACKGROUND_FLAGS)
    deadline=time.monotonic()+30
    try:
        while True:
            _cancelled(cancel_event)
            if time.monotonic()>=deadline:raise RuntimeError('ffprobe 读取媒体超时。')
            try:
                output,error=process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:continue
        _cancelled(cancel_event)
        if process.returncode:raise RuntimeError('ffprobe 无法读取媒体：'+error.decode('utf-8','replace')[-3000:])
        try:payload=json.loads(output)
        except (ValueError,UnicodeError) as error:raise RuntimeError('ffprobe returned invalid JSON') from error
        return _media_info_from_payload(source,payload)
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill();process.communicate(timeout=2)
        if process.stdout:process.stdout.close()
        if process.stderr:process.stderr.close()


def _cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled('已取消 1080p 代理生成；原片与已有代理保持不变。')


def _source_stamp(source):
    path=Path(source).expanduser().resolve()
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size<=0:
        raise ValueError('代理原片必须是非空的普通文件。')
    return {'path':os.path.normcase(str(path)),'size':info.st_size,'mtime_ns':info.st_mtime_ns}


def _cache_key(stamp):
    document={'source':stamp,'parameters':PARAMETERS}
    return hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def _proxy_rate(media):
    fps=media.fps if math.isfinite(media.fps) and media.fps>0 else 30.
    return Fraction(min(30.,fps)).limit_denominator(100000)


def _proxy_modes(caps):
    """Only capability-probed hardware is tried; every route can fall back."""
    from hardware_encoders import available_codecs, preferred_codec
    available=available_codecs(caps)
    first=preferred_codec(caps,'h264')
    order=list(dict.fromkeys([first,'h264_nvenc','h264_amf','h264_qsv']))
    modes=[]
    for codec in order:
        if codec not in available:continue
        if codec=='h264_nvenc':
            if getattr(caps,'cuda_decode',False) and getattr(caps,'scale_cuda',False):modes.append('cuda_nvenc')
            modes.append('cpu_nvenc')
        elif codec=='h264_amf':modes.append('cpu_amf')
        elif codec=='h264_qsv':modes.append('cpu_qsv')
    modes.append('cpu')
    return modes


def _build_command(ffmpeg,source,output,media,mode):
    rate=_proxy_rate(media);rate_text=f'{rate.numerator}/{rate.denominator}'
    gop=max(1,math.floor(float(rate)))
    command=[ffmpeg,'-hide_banner','-y','-nostdin','-loglevel','error','-xerror',
             '-stats_period','0.25','-progress','pipe:1','-nostats','-filter_threads','2']
    if mode=='cuda_nvenc':
        command+=['-hwaccel','cuda','-hwaccel_output_format','cuda']
    else:
        command+=['-threads','2']
    command+=['-i',source,'-map','0:v:0','-map','0:a:0?','-sn','-dn']
    # Evaluate iw/ih after FFmpeg's autorotation; reset_sar also preserves
    # anamorphic display aspect. Portrait media remains portrait and no output
    # dimension exceeds the monitoring bounds.
    prefix=f'setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,'
    scale="w='min(iw,1920)':h='min(ih,1080)':force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1"
    if mode=='cuda_nvenc':
        graph=prefix+'scale_cuda='+scale+':interp_algo=bilinear:format=nv12'
    else:
        graph=prefix+'scale='+scale+':flags=fast_bilinear,format=yuv420p'
        if mode=='cpu_qsv':graph=graph.removesuffix('format=yuv420p')+'format=nv12'
    command+=['-vf',graph,'-fps_mode','cfr']
    if mode.endswith('nvenc'):
        command+=['-c:v','h264_nvenc','-preset','p1','-tune','ull','-rc','vbr',
                  '-cq','27','-b:v','0','-multipass','disabled','-rc-lookahead','0','-zerolatency','1']
    elif mode=='cpu_amf':
        command+=['-c:v','h264_amf','-usage','transcoding','-quality','speed','-rc','cqp',
                  '-qp_i','27','-qp_p','27','-async_depth','1']
    elif mode=='cpu_qsv':
        command+=['-c:v','h264_qsv','-preset','veryfast','-global_quality','27',
                  '-look_ahead','0','-async_depth','1']
    elif mode!='cpu':raise ValueError('未知代理编码路径：'+str(mode))
    else:
        command+=['-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-crf','26','-threads','2']
    command+=['-g',str(gop),'-bf','0','-refs','1','-c:a','aac','-b:a','128k','-ac','2','-ar','48000',
              '-af','aresample=async=1:first_pts=0','-t',f'{media.duration:.9f}',
              '-map_metadata','0','-movflags','+faststart',str(output)]
    return command


def _run_encode(command,duration,*,cancel_event=None,progress=None,phase='生成 1080p 代理'):
    _cancelled(cancel_event)
    environment=os.environ.copy();environment['OMP_NUM_THREADS']='2'
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             env=environment,creationflags=BACKGROUND_FLAGS)
    events=queue.Queue();tail=deque(maxlen=40)
    def read(stream,kind):
        try:
            for raw in iter(stream.readline,b''):
                events.put((kind,raw.decode('utf-8','replace').rstrip()))
        finally:events.put((kind,None))
    readers=[threading.Thread(target=read,args=(stream,kind),daemon=True,name='quick-proxy-'+kind)
             for stream,kind in ((process.stdout,'progress'),(process.stderr,'error'))]
    for reader in readers:reader.start()
    fields={};finished=0;ended=False;frames=0
    try:
        while finished<2:
            _cancelled(cancel_event)
            try:kind,line=events.get(timeout=.1)
            except queue.Empty:continue
            if line is None:finished+=1;continue
            if kind=='error':
                if line:tail.append(line)
                continue
            parsed=parse_progress_line(line)
            if parsed is None:continue
            key,value=parsed;fields[key]=value
            if key=='frame':
                try:frames=max(frames,int(value))
                except ValueError:pass
            if key=='progress':
                ended=value=='end'
                if progress:progress(min(98.,max(0.,progress_seconds(fields)/duration*98.)),phase)
        code=process.wait();_cancelled(cancel_event)
        if code or not ended or frames<=0:
            detail='\n'.join(tail)[-3000:]
            raise ProxyEncodeError(f'1080p 代理编码失败（{code}）：{detail or "未收到完整视频的结束记录"}')
        return frames
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=2)
        for reader in readers:reader.join(timeout=1)
        if process.stdout:process.stdout.close()
        if process.stderr:process.stderr.close()


def _validate(ffprobe,source,proxy,media,*,cancel_event=None):
    result=validate_proxy_media(ffprobe,source,str(proxy),media.duration,media.fps,
        probe=lambda binary,path:_probe_media_cancel(binary,path,cancel_event=cancel_event))
    if (result.video_codec!='h264' or result.bit_depth!=8 or result.width>1920 or result.height>1080
            or result.width%2 or result.height%2 or not 0<result.fps<=30.001):
        raise ValueError('代理未满足 1080p / H.264 / 8-bit / 最多 30fps 规格。')
    expected_rate=float(_proxy_rate(media))
    if abs(result.fps-expected_rate)>.01:
        raise ValueError('代理帧率与所选监看规格不一致。')
    if abs(result.duration-media.duration)>max(1/expected_rate,.04)+.02:
        raise ValueError('代理没有覆盖完整原片时间轴。')
    if media.has_audio and not result.has_audio:
        raise ValueError('代理缺少原片音轨。')
    return result


def prepare_1080p_proxy(source,ffmpeg,ffprobe,caps,cancel_event=None,progress=None):
    """Build or reuse one complete proxy; invoke from a background worker.

    Progress is actual FFmpeg output time (0..98), then validation/publication.
    Source identity and parameter version prevent same-name collisions. Only a
    successfully ended, validated encode is atomically published and indexed.
    """
    _cancelled(cancel_event)
    if not ffmpeg or not ffprobe:raise ValueError('FFmpeg / ffprobe 尚未就绪。')
    stamp=_source_stamp(source);source=stamp['path']
    if progress:progress(0.,'读取原片，准备 1080p 代理')
    _cancelled(cancel_event)
    media=_probe_media_cancel(ffprobe,source,cancel_event=cancel_event)
    _cancelled(cancel_event)
    if not math.isfinite(media.duration) or media.duration<=0 or media.width<=0 or media.height<=0:
        raise ValueError('原片没有有效的视频尺寸或时长。')
    folder=Path(cache_dir('proxies/1080p'));folder.mkdir(parents=True,exist_ok=True)
    destination=folder/(_cache_key(stamp)+'.mp4')
    if destination.is_file():
        try:_validate(ffprobe,source,destination,media,cancel_event=cancel_event)
        except ExportCancelled:raise
        except (ValueError,RuntimeError,OSError):pass
        else:
            _cancelled(cancel_event)
            if _source_stamp(source)!=stamp:raise ValueError('原片在读取期间发生变化，请重新生成代理。')
            register_proxy(source,str(destination))
            if progress:progress(100.,'复用已验证的 1080p 代理')
            return str(destination)
    modes=_proxy_modes(caps)
    temporary=None
    try:
        descriptor,name=tempfile.mkstemp(prefix='.'+destination.stem[:16]+'.',suffix='.pending.mp4',dir=folder)
        os.close(descriptor);temporary=Path(name)
        errors=[]
        for mode in modes:
            _cancelled(cancel_event)
            phase={'cuda_nvenc':'生成 1080p 代理 · CUDA / NVENC',
                   'cpu_nvenc':'生成 1080p 代理 · NVENC 兼容解码',
                   'cpu_amf':'生成 1080p 代理 · AMD AMF 硬件编码',
                   'cpu_qsv':'生成 1080p 代理 · Intel QSV 硬件编码',
                   'cpu':'生成 1080p 代理 · CPU 兼容模式（2 线程）'}[mode]
            if progress:progress(0.,phase)
            try:
                _run_encode(_build_command(ffmpeg,source,temporary,media,mode),media.duration,
                            cancel_event=cancel_event,progress=progress,phase=phase)
                _cancelled(cancel_event)
                if progress:progress(99.,'校验完整时长与代理规格')
                _cancelled(cancel_event)
                _validate(ffprobe,source,temporary,media,cancel_event=cancel_event)
                break
            except (ProxyEncodeError,ValueError) as error:
                errors.append(str(error))
                if mode==modes[-1]:raise RuntimeError('\n'.join(errors)[-5000:]) from error
        _cancelled(cancel_event)
        if _source_stamp(source)!=stamp:raise ValueError('原片在生成期间发生变化，未发布代理，请重试。')
        publish_completed(temporary,destination,cancel_event=cancel_event)
        temporary=None
        if _source_stamp(source)!=stamp:raise ValueError('原片已变化，未绑定旧版本代理，请重试。')
        register_proxy(source,str(destination))
        if progress:progress(100.,'1080p 代理已就绪')
        return str(destination)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
