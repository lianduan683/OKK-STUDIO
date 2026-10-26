"""Selected-range picture-motion stabilization through FFmpeg / vid.stab.

This independent implementation uses the public FFmpeg filters, not Resolve
code or camera gyro data. Both passes see the same CFR frames and time range.
Large frames are analysed at an integer-reduced size; local-motion vectors,
feature centres and field sizes are restored to native pixel coordinates
before applying the transform to the original full-resolution picture.
Filter reference: https://www.ffmpeg.org/ffmpeg-filters.html#vidstabdetect
https://www.ffmpeg.org/ffmpeg-filters.html#vidstabtransform

The installed vid.stab filters operate on 8-bit pixels. Ten-bit outputs retain
source colour tags and use a ten-bit encoder, but cannot recover precision lost
inside that filter. HDR is never silently tone mapped.
"""
from __future__ import annotations

from collections import deque
import copy
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading

from exporter_core import CREATE_NO_WINDOW, ExportCancelled, probe_media


ALGORITHM_VERSION='FFmpeg vid.stab native-size CFR range v1'
IMAGE_FIELDS=('image_smoothing','image_shakiness','image_zoom','image_tripod','image_model','image_quality')
_CUDA_DECODERS={'h264','hevc','av1','vp9','mpeg2video'}
_LOCAL_MOTION=re.compile(r'\(LM\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+([^\s()]+)\s+([^\s()]+)\)')


def _number(value,label,low,high):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError(f'{label}必须在 {low:g} 到 {high:g} 之间')
    return float(value)


def _image_parameters(settings):
    smoothing=_number(getattr(settings,'image_smoothing',.5),'画面防抖平滑时间',0,5)
    shakiness=getattr(settings,'image_shakiness',5)
    if type(shakiness) is not int or not 1<=shakiness<=10:raise ValueError('抖动程度必须为 1 到 10 的整数')
    zoom=getattr(settings,'image_zoom',True);tripod=getattr(settings,'image_tripod',False)
    if type(zoom) is not bool or type(tripod) is not bool:raise ValueError('自动裁切和固定镜头必须为布尔值')
    model=getattr(settings,'image_model','similarity')
    if model not in ('similarity','translation'):raise ValueError('画面防抖模式无效')
    quality=getattr(settings,'image_quality','fine')
    if quality not in ('fine','fast'):raise ValueError('画面防抖画质模式无效')
    parameters=dict(image_smoothing=smoothing,image_shakiness=shakiness,image_zoom=zoom,
                    image_tripod=tripod,image_model=model)
    # Existing 5.8 cache signatures did not include an interpolation field.
    # Keep that exact dictionary for fine/legacy; fast must have its own key.
    if quality=='fast':parameters['image_quality']='fast'
    return parameters


def image_cache_signature(source_path,source_start,source_end,settings):
    """Sign only inputs used by this engine; no gyro/lens sidecar dependencies."""
    start=_number(source_start,'原片开始',0,1e12);end=_number(source_end,'原片结束',0,1e12)
    if end<=start:raise ValueError('原片时间范围无效')
    source=Path(source_path).resolve()
    try:
        stamp=source.stat();identity=(str(source),stamp.st_size,stamp.st_mtime_ns)
    except OSError:identity=(str(source),'missing')
    payload={'algorithm':ALGORITHM_VERSION,'source':identity,'range':[round(start,9),round(end,9)],
             'parameters':_image_parameters(settings)}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=True).encode()).hexdigest()


def _cancelled(event):
    if event is not None and event.is_set():raise ExportCancelled('已取消画面防抖')


def _binary(value,label):
    if not value:raise ValueError('未找到'+label+'，无法进行画面防抖。')
    path=Path(value)
    found=str(path.resolve()) if path.is_file() else shutil.which(str(value))
    if not found:raise ValueError('未找到'+label+'，无法进行画面防抖。')
    return found


def _analysis_factor(width,height):
    """Use exact isotropic integer scales, including even YUV420 dimensions.

    Common UHD/8K frames become 1920x1080. Odd/unusual dimensions may retain a
    larger analysis image rather than introducing anisotropic camera motion.
    """
    factor=1
    while max(width//factor,height//factor)>2048 or width*height//(factor*factor)>1920*1200:
        candidate=factor*2
        if width%(candidate*2) or height%(candidate*2) or min(width//candidate,height//candidate)<32:break
        factor=candidate
    return factor


def _restore_motion_coordinates(source,destination,factor,frame_count,*,cancel_event=None):
    """Map vid.stab ASCII LM pixel units to native resolution, not just dx/dy.

    Upstream format: (LM vx vy field_x field_y field_size contrast match).
    The first five values use pixels; contrast and average matching error do
    not. Scaling centres as well as displacements preserves the rotation fit.
    https://github.com/georgmartius/vid.stab/blob/master/docs/trf-format.md
    """
    if type(factor) is not int or factor<1:raise ValueError('画面运动坐标缩放比例无效')
    tracked=frames=0
    with Path(source).open(encoding='ascii',errors='strict') as input_stream,Path(destination).open('w',encoding='ascii',newline='\n') as output_stream:
        for line in input_stream:
            _cancelled(cancel_event)
            frame=re.match(r'^Frame\s+(\d+)\s+\(List\s+(\d+)',line)
            if frame:
                frames+=1
                if int(frame.group(1))!=frames:raise RuntimeError('画面运动记录的帧顺序不匹配。')
                expected=int(frame.group(2));found=0
                def restore(match):
                    nonlocal found
                    found+=1
                    values=[int(match.group(index))*factor for index in range(1,6)]
                    if any(value<-32768 or value>32767 for value in values):raise ValueError('画面运动坐标超出 vid.stab 支持范围。')
                    if not all(math.isfinite(float(match.group(index))) for index in (6,7)):
                        raise ValueError('画面运动测量包含无效数值。')
                    return '(LM '+' '.join(str(value) for value in values)+' '+match.group(6)+' '+match.group(7)+')'
                line=_LOCAL_MOTION.sub(restore,line)
                if found!=expected:raise RuntimeError('画面运动记录不完整，已停止应用。')
                if found:tracked+=1
            output_stream.write(line)
    if frames!=frame_count:raise RuntimeError('画面运动记录帧数不匹配，已停止应用。')
    return tracked


def _run(command,folder,*,cancel_event=None,progress=None,log=None,frame_count=1):
    _cancelled(cancel_event)
    environment=os.environ.copy()
    environment['OMP_NUM_THREADS']=str(min(8,os.cpu_count() or 2))
    process=subprocess.Popen(command,cwd=folder,env=environment,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=CREATE_NO_WINDOW)
    lines=queue.Queue();tail=deque(maxlen=60)
    def reader(stream,kind):
        try:
            for raw in iter(stream.readline,b''):lines.put((kind,raw.decode('utf-8','replace').rstrip()))
        finally:lines.put((kind,None))
    threads=[threading.Thread(target=reader,args=(stream,kind),name='image-stabilize-'+kind,daemon=True)
             for stream,kind in ((process.stdout,'progress'),(process.stderr,'log'))]
    for thread in threads:thread.start()
    frames=0;finished=0
    try:
        while finished<2:
            _cancelled(cancel_event)
            try:kind,line=lines.get(timeout=.1)
            except queue.Empty:
                if process.poll() is not None and not any(thread.is_alive() for thread in threads) and lines.empty():break
                continue
            if line is None:finished+=1;continue
            if not line:continue
            if kind=='log':
                tail.append(line)
                if log and ('error' in line.lower() or 'warning' in line.lower()):log(line)
                continue
            key,separator,value=line.partition('=')
            if key=='frame' and separator:
                try:frames=max(frames,int(value))
                except ValueError:pass
            elif key=='progress' and separator:
                if progress:progress(min(1.,frames/max(1,frame_count)))
            elif key not in ('fps','stream_0_0_q','bitrate','total_size','out_time_us','out_time_ms','out_time',
                              'dup_frames','drop_frames','speed'):
                tail.append(line)
                if log and ('error' in line.lower() or 'warning' in line.lower()):log(line)
        returncode=process.wait()
        _cancelled(cancel_event)
        if returncode:raise RuntimeError(f'FFmpeg 画面防抖失败（{returncode}）：\n'+'\n'.join(tail)[-4500:])
        return frames
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        for thread in threads:thread.join(timeout=2)
        if process.stdout:process.stdout.close()
        if process.stderr:process.stderr.close()


def _colour_options(media):
    result=[]
    for flag,name in (('-color_primaries','color_primaries'),('-color_trc','color_transfer'),
                      ('-colorspace','color_space'),('-color_range','color_range')):
        value=getattr(media,name,None)
        # RGB source matrices are not valid matrix tags for our YUV cache.
        if name=='color_space' and value in ('gbr','rgb'):value='bt709'
        if name=='color_range' and media.color_space in ('gbr','rgb'):value='tv'
        if value and value not in ('unknown','unspecified','reserved'):result += [flag,str(value)]
    return result


def _encoded_frame_count(ffprobe,path):
    command=[ffprobe,'-v','error','-select_streams','v:0','-show_entries','stream=nb_frames','-of','json',str(path)]
    result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,timeout=30)
    if result.returncode:raise RuntimeError('无法验证画面防抖缓存帧数。')
    try:return int(json.loads(result.stdout)['streams'][0]['nb_frames'])
    except (ValueError,TypeError,KeyError,IndexError,json.JSONDecodeError):
        raise RuntimeError('画面防抖缓存没有可验证的精确帧数。') from None


def run_image_stabilization(source_path,source_start,source_end,settings,output_path,*,
                            ffmpeg_path,ffprobe_path,cancel_event=None,progress=None,log=None):
    # Lazy imports keep gyroflow_core's engine dispatcher free of import cycles.
    from gyroflow_core import StabilizationResult,GyroflowProbe
    settings=copy.deepcopy(settings);parameters=_image_parameters(settings)
    _cancelled(cancel_event)
    source=Path(source_path).resolve(strict=True);output=Path(output_path).resolve()
    protected={source}
    for name in ('gyro_path','lens_path','project_path'):
        value=getattr(settings,name,'')
        if value:protected.add(Path(value).resolve())
    if output in protected:raise ValueError('画面防抖输出不能覆盖原片、陀螺仪、镜头配置或工程。')
    ffmpeg=_binary(ffmpeg_path,'ffmpeg.exe');ffprobe=_binary(ffprobe_path,'ffprobe.exe')
    media=probe_media(ffprobe,str(source))
    fps=_number(media.fps,'原片帧率',.01,1000)
    if media.width<16 or media.height<16:raise ValueError('原片画幅太小，无法可靠分析画面运动。')
    requested_start=_number(source_start,'原片开始',0,media.duration)
    requested_end=_number(source_end,'原片结束',0,media.duration+1/fps)
    if requested_end<=requested_start:raise ValueError('画面防抖时间范围无效')
    first=math.floor(requested_start*fps+1e-7)
    last=min(math.floor(media.duration*fps+.5),math.ceil(requested_end*fps-1e-7))
    count=last-first
    if count<2:raise ValueError('画面防抖至少需要两帧画面。')
    start=first/fps;end=last/fps;expected=count/fps
    signature=image_cache_signature(source,start,end,settings)
    output.parent.mkdir(parents=True,exist_ok=True)
    ratio=str(Fraction(fps).limit_denominator(100000))
    smoothing=min(1000,max(0,round(parameters['image_smoothing']*fps)))
    shakiness=parameters['image_shakiness'];tripod=parameters['image_tripod']
    detect=(f'vidstabdetect=result=analysis.trf:shakiness={shakiness}:accuracy={max(9,shakiness)}'
            f':stepsize=6:mincontrast=0.15:fileformat=ascii:tripod={1 if tripod else 0}')
    interpolation='bilinear' if parameters.get('image_quality')=='fast' else 'bicubic'
    transform=(f'vidstabtransform=input=motion.trf:smoothing={smoothing}:optalgo=gauss'
               f':optzoom={1 if parameters["image_zoom"] else 0}:crop=black:interpol={interpolation}'
               f':tripod={1 if tripod else 0}'
               f':maxangle={0 if parameters["image_model"]=="translation" else -1}')
    # Relative transform-file names are interpreted in our ASCII temporary
    # working directory, so Windows drive colons and apostrophes never enter
    # FFmpeg filter syntax. Source/output remain ordinary argument-list paths.
    conversion=('scale=iw:ih:out_color_matrix=bt709:out_range=tv,'
                if media.color_space in ('gbr','rgb') else '')
    timing=f'fps={ratio}:round=near,trim=end_frame={count},setpts=PTS-STARTPTS,'
    preparation=timing+conversion+'format=yuv420p'
    factor=_analysis_factor(media.width,media.height)
    analysis_width,analysis_height=media.width//factor,media.height//factor
    if factor>1:
        matrix=':out_color_matrix=bt709:out_range=tv' if media.color_space in ('gbr','rgb') else ''
        cpu_analysis=(timing+f'scale={analysis_width}:{analysis_height}:flags=bilinear'+matrix+',format=yuv420p')
    else:cpu_analysis=preparation
    cpu_threads=str(min(8,max(2,(os.cpu_count() or 4)//2)))
    common=[ffmpeg,'-hide_banner','-loglevel','warning','-nostdin','-y','-threads',cpu_threads,'-filter_threads',cpu_threads,
            '-ss',f'{start:.12f}','-noautorotate','-i',str(source),'-map','0:v:0','-an','-sn','-dn',
            '-frames:v',str(count),'-progress','pipe:1','-nostats']
    reported=[0.]
    def notify(value,message):
        value=max(reported[0],min(100.,float(value)));reported[0]=value
        if progress:progress(value,message)
    if log:
        log(f'画面运动分析 {analysis_width}×{analysis_height}；变换与缓存保留原片 {media.width}×{media.height}。')
        log('运动估计与原尺寸变换使用 CPU；支持时使用 NVIDIA 硬件解码/缩小分析图，并优先 NVENC 编码。')
        log('防抖插值：快速（双线性，细节可能稍柔和）' if interpolation=='bilinear' else '防抖插值：精细（双三次，处理较慢）')
    if media.bit_depth>8 or media.color_transfer in ('smpte2084','arib-std-b67'):
        if log:log('注意：本机 vid.stab 防抖滤镜内部为 8bit；将保留原片色彩标记并使用 10bit 缓存，'
                   '不会自动色调映射，但不能保留滤镜内丢失的 10bit/HDR 精度。此类原片可优先选择 Gyroflow。')
    notify(0.,'画面防抖 · 分析运动（1/2）')
    with tempfile.TemporaryDirectory(prefix='okk_image_stab_',dir=output.parent) as folder:
        analysis_candidates=[];gpu_input=None;hardware_analysis=False
        if factor>1 and media.video_codec in _CUDA_DECODERS:
            position=common.index('-i')
            gpu_input=[*common[:position],'-hwaccel','cuda','-hwaccel_output_format','cuda',*common[position:]]
            gpu_analysis=(timing+f'scale_cuda={analysis_width}:{analysis_height}:format=yuv420p:interp_algo=bilinear:passthrough=0,'
                          'hwdownload,format=yuv420p')
            analysis_candidates.append(('NVIDIA 硬件解码与缩放',gpu_input,gpu_analysis))
        analysis_candidates.append(('CPU 解码与分析图缩放',common,cpu_analysis))
        for index,(decoder,inputs,filters) in enumerate(analysis_candidates):
            _cancelled(cancel_event)
            if log:log('画面防抖分析：'+decoder)
            detect_command=[*inputs,'-vf',filters+','+detect,'-f','null','NUL' if os.name=='nt' else os.devnull]
            try:
                detected=_run(detect_command,folder,cancel_event=cancel_event,
                    progress=lambda part:notify(part*43.,'画面防抖 · 分析运动（1/2）'),log=log,frame_count=count)
                if detected!=count:raise RuntimeError('画面运动分析帧数不完整。')
                hardware_analysis=decoder.startswith('NVIDIA')
                break
            except ExportCancelled:raise
            except (OSError,RuntimeError) as error:
                if index==len(analysis_candidates)-1:raise
                if log:log('硬件分析未完成，改用 CPU 解码与小图分析。原因：'+str(error)[-700:])
                (Path(folder)/'analysis.trf').unlink(missing_ok=True)
        analysis_motion=Path(folder)/'analysis.trf'
        if not analysis_motion.is_file() or analysis_motion.stat().st_size<20:
            raise RuntimeError('画面运动分析没有生成完整帧数的变换数据，已停止应用。')
        motion=Path(folder)/'motion.trf'
        tracked=_restore_motion_coordinates(analysis_motion,motion,factor,count,cancel_event=cancel_event)
        notify(44.,'画面防抖 · 已恢复原尺寸运动坐标')
        if not tracked:raise ValueError('画面缺少可跟踪的纹理，无法可靠计算防抖；请选择有清晰细节的片段。')
        if tracked<count*.5 and log:log('部分帧缺少可靠画面特征，已平滑可用运动；请预览检查遮挡、模糊与灯光变化。')
        _cancelled(cancel_event)
        pending=Path(folder)/'stabilized.mp4'
        ten_bit=media.bit_depth>8
        colour=_colour_options(media)
        nvenc=['-c:v','hevc_nvenc','-preset','p3','-rc','vbr','-cq','16','-b:v','0',
               '-bf','0','-pix_fmt','p010le' if ten_bit else 'yuv420p','-tag:v','hvc1']
        candidates=[]
        if hardware_analysis:
            # This is an identity-size format conversion, not a small-image
            # upscale. CPU vid.stab still receives every native source pixel.
            native_download=(timing+'scale_cuda=iw:ih:format=yuv420p:interp_algo=nearest:passthrough=0,'
                             'hwdownload,format=yuv420p')
            candidates.append(('NVIDIA 硬件解码 + NVENC',gpu_input,native_download,nvenc))
        candidates.append(('CPU 解码 + NVIDIA NVENC',common,preparation,nvenc))
        if ten_bit:
            candidates.append(('CPU x265',common,preparation,['-c:v','libx265','-preset','fast','-crf','16','-pix_fmt','yuv420p10le',
                '-x265-params','pools=4:frame-threads=2:log-level=error','-tag:v','hvc1']))
        else:candidates.append(('CPU x264',common,preparation,['-c:v','libx264','-preset','fast','-crf','16','-pix_fmt','yuv420p']))
        last_error=None
        for index,(encoder,inputs,filters,options) in enumerate(candidates):
            _cancelled(cancel_event)
            if log:log('画面防抖缓存编码：'+encoder)
            notify(45.,'画面防抖 · 平滑与编码（2/2，'+encoder+'）')
            command=[*inputs,'-vf',filters+','+transform,'-r',ratio,'-fps_mode','cfr',
                     '-map_metadata','0',*options,*colour,'-movflags','+faststart',str(pending)]
            try:
                rendered_frames=_run(command,folder,cancel_event=cancel_event,
                    progress=lambda part:notify(45+part*52.,'画面防抖 · 平滑与编码（2/2）'),log=log,frame_count=count)
                if rendered_frames!=count:raise RuntimeError(f'编码后的画面防抖缓存帧数不完整：应为 {count}，实际 {rendered_frames}。')
                break
            except ExportCancelled:raise
            except (RuntimeError,OSError) as error:
                last_error=error
                if index==len(candidates)-1:raise
                if log:log('硬件处理未完成，保留运动分析并切换到下一种解码/编码方式。原因：'+str(error)[-700:])
                if pending.exists():pending.unlink()
        else:raise RuntimeError(str(last_error or '画面防抖缓存编码失败。'))
        notify(98.,'画面防抖 · 验证帧数与缓存')
        _cancelled(cancel_event)
        rendered=probe_media(ffprobe,str(pending))
        if (rendered.width,rendered.height)!=(media.width,media.height):
            raise RuntimeError('画面防抖改变了缓存画幅，已停止应用。')
        if abs(rendered.fps-fps)>max(.001,fps*.0001):raise RuntimeError('画面防抖缓存帧率不匹配，已停止应用。')
        if abs(rendered.duration-expected)>max(.002,.1/fps):raise RuntimeError('画面防抖缓存时长不匹配，已停止应用。')
        if _encoded_frame_count(ffprobe,pending)!=count:raise RuntimeError('画面防抖缓存帧数不匹配，已停止应用。')
        if ten_bit and rendered.bit_depth<10:raise RuntimeError('未生成预期的 10bit 编码缓存，已停止应用。')
        if image_cache_signature(source,start,end,settings)!=signature:raise RuntimeError('原片在防抖计算期间发生变化，请重试。')
        _cancelled(cancel_event)
        os.replace(pending,output)
    probe=GyroflowProbe(source_name='画面运动 · FFmpeg vid.stab',motion_samples=tracked,
        duration=media.duration,fps=fps,width=media.width,height=media.height)
    notify(100.,'所选片段画面防抖完成')
    return StabilizationResult(str(output),signature,start,end,rendered.duration,rendered.fps,
                               rendered.width,rendered.height,probe)
