"""Vulkan/libplacebo export plans for real GPU scaling and primary grading.

No encoder quality setting is changed here. The caller retains ownership of
frame planning, encoder arguments, cancellation, output validation and atomic
publication. Unsupported effects raise a specific exception for an explicit
CPU fallback; they are never silently omitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re
import subprocess
import tempfile

from cache_paths import cache_dir
from creative_core import ColorGrade,escape_filter_path,transition_filter,grade_is_identity
from exporter_core import CREATE_NO_WINDOW
from realtime_grade import grade_uniforms,shader_source


class GpuExportUnsupported(ValueError):pass


# Hardware enumeration is not a stability guarantee. This packaged FFmpeg
# advertises seven xfade_vulkan effects but real two-input regression tests
# produced Windows access violations, including after a same-size GPU copy.
# Never select those production routes until a future runtime passes them.
VERIFIED_GPU_TRANSITIONS:tuple[str,...]=()


@dataclass(frozen=True,slots=True)
class GpuExportCapabilities:
    ffmpeg_path:str
    libplacebo:bool
    vulkan:bool
    xfade_vulkan:bool
    transition_kinds:tuple[str,...]=()
    reason:str=''
    inherit_device:bool=False
    scale_cuda:bool=False


@dataclass(frozen=True,slots=True)
class GpuFilterPlan:
    ffmpeg_path:str
    global_options:tuple[str,...]
    input_options:tuple[str,...]
    filter_chain:str
    shader_path:str
    download_format:str
    decode_mode:str
    color_options:tuple[str,...]=()


def _text(command):
    result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=20,creationflags=CREATE_NO_WINDOW)
    return (result.stdout+result.stderr).decode('utf-8','replace')


@lru_cache(maxsize=8)
def _capabilities(path,stamp):
    effects=_text([path,'-hide_banner','-filters'])
    hardware=_text([path,'-hide_banner','-hwaccels'])
    libplacebo=bool(re.search(r'\blibplacebo\s',effects));vulkan=bool(re.search(r'^vulkan\s*$',hardware,re.M))
    xfade=bool(re.search(r'\bxfade_vulkan\s',effects));kinds=()
    if xfade:
        help_text=_text([path,'-hide_banner','-h','filter=xfade_vulkan'])
        kinds=tuple(re.findall(r'^\s{5}([a-z][a-z0-9_]*)\s+\d+\s',help_text,re.M))
    missing=[name for name,present in (('libplacebo',libplacebo),('Vulkan',vulkan)) if not present]
    placebo_help=_text([path,'-hide_banner','-h','filter=libplacebo']) if libplacebo else ''
    return GpuExportCapabilities(path,libplacebo,vulkan,xfade,kinds,
        '缺少 GPU 特效组件：'+', '.join(missing) if missing else '', 'inherit_device' in placebo_help,
        bool(re.search(r'\bscale_cuda\s',effects)))


def detect_gpu_export(ffmpeg_path):
    path=Path(ffmpeg_path).resolve(strict=True);stat=path.stat()
    return _capabilities(str(path),(stat.st_size,stat.st_mtime_ns))


def find_gpu_export_ffmpeg(app_dir=None):
    import sys
    root=Path(app_dir or (Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent))
    candidates=(root/'bin'/'gpu_export'/'ffmpeg.exe',root/'native_runtime'/'gpu_export_full'/'ffmpeg.exe',
                root/'native_runtime'/'gpu_export'/'ffmpeg.exe',root/'bin'/'ffmpeg.exe')
    for candidate in candidates:
        if candidate.is_file():
            try:
                caps=detect_gpu_export(candidate)
                if caps.libplacebo and caps.vulkan:return str(candidate.resolve())
            except (OSError,ValueError,subprocess.SubprocessError):pass
    return ''


def write_grade_shader(grades,directory=None):
    grades=tuple(grade or ColorGrade() for grade in grades)
    if any(grade.enabled and grade.lut_path for grade in grades):
        raise GpuExportUnsupported('当前 GPU 导出尚未验证逐层 LUT 的应用顺序，请保留 CPU 特效路径。')
    uniforms,_=grade_uniforms(grades)
    # Export scales before grading, just like the existing CPU chain. PREOUTPUT
    # is target-encoded RGB, after scaling/color conversion and before the
    # output RGB -> YUV representation conversion (OUTPUT is already YUV for
    # an NV12/P010 target). In particular do not grade 8K MAIN just to make
    # a 4K deliverable, or apply the primary controls to linear-light RGB.
    source=shader_source().replace('//!HOOK MAIN\n','//!HOOK PREOUTPUT\n')
    pattern=re.compile(r'//!PARAM (\w+)\n//!TYPE DYNAMIC float\n[^\n]+')
    def replace(match):
        name=match.group(1)
        return f'//!PARAM {name}\n//!TYPE CONSTANT float\n{uniforms[name]:.9g}'
    source,count=pattern.subn(replace,source)
    if count!=len(uniforms):raise GpuExportUnsupported('自动调色 shader 参数版本不匹配，已保留 CPU 特效路径。')
    folder=Path(directory or cache_dir('gpu_export/shaders'));folder.mkdir(parents=True,exist_ok=True)
    destination=folder/(hashlib.sha256(source.encode()).hexdigest()[:32]+'.hook')
    if not destination.is_file():
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',suffix='.pending',dir=folder,delete=False) as stream:
                temporary=Path(stream.name);stream.write(source)
            temporary.replace(destination);temporary=None
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
    return str(destination)


def _signal_options(media):
    options=[];output=[]
    for attribute,name,flag in (('color_primaries','color_primaries','-color_primaries'),
        ('color_transfer','color_trc','-color_trc'),('color_space','colorspace','-colorspace'),('color_range','range','-color_range')):
        value=getattr(media,attribute,None) if media is not None else None
        if value and value not in ('unknown','unspecified','reserved','gbr','rgb'):
            if not re.fullmatch(r'[A-Za-z0-9_-]+',value):raise GpuExportUnsupported('原片色彩标记无效')
            options.append(f'{name}={value}');output += [flag,value]
    return options,tuple(output)


def _dimensions(width,height,pixel_format):
    if type(width) is not int or type(height) is not int or width<2 or height<2 or width%2 or height%2:
        raise ValueError('GPU 导出画幅必须是正偶数')
    if pixel_format not in ('yuv420p','yuv420p10le','nv12','p010le'):
        raise GpuExportUnsupported('此输出像素格式尚未验证 GPU 导出')


def build_gpu_video_plan(grades,*,ffmpeg_path,width,height,pixel_format,rate_text,frames,duration,
                         media=None,prefix_frames=0,decode_mode='cuda',shader_dir=None,download=True,gpu_index=0):
    _dimensions(width,height,pixel_format)
    if type(frames) is not int or frames<1 or type(prefix_frames) is not int or prefix_frames<0:
        raise ValueError('GPU 导出帧范围无效')
    if not math.isfinite(duration) or duration<=0:raise ValueError('GPU 导出时长无效')
    if type(gpu_index) is not int or gpu_index<0:raise ValueError('GPU 导出设备编号无效')
    if not re.fullmatch(r'[0-9]+(?:/[0-9]+|\.[0-9]+)?',str(rate_text)):raise ValueError('GPU 导出帧率无效')
    caps=detect_gpu_export(ffmpeg_path)
    if not caps.libplacebo or not caps.vulkan:raise GpuExportUnsupported(caps.reason)
    if decode_mode not in ('vulkan','cuda','cpu'):raise ValueError('GPU 导出解码路径无效')
    if getattr(media,'color_transfer',None) in ('smpte2084','arib-std-b67','linear','vlog'):
        raise GpuExportUnsupported('HDR/线性原片尚未完成 GPU 导出色彩验证，请保留原导出路径。')
    if media is not None and media.width>0 and media.height>0:
        if (media.width*height!=media.height*width
                or getattr(media,'sample_aspect_ratio','1:1') not in ('1:1','1/1')):
            # The existing CPU pipeline grades the padded canvas. Libplacebo
            # grades content before adding black bars; lifting blacks would
            # therefore change the result. Preserve the original order.
            raise GpuExportUnsupported('此素材画幅需补边或使用非方形像素，保留补边后调色的原处理顺序。')
    grades=tuple(grades)
    shader=write_grade_shader(grades,shader_dir)
    active_grade=any(grade is not None and not grade_is_identity(grade) for grade in grades)
    surface='p010le' if pixel_format in ('yuv420p10le','p010le') else 'nv12'
    # CUDA interop must stay on NVIDIA. CPU/Vulkan plans have no such vendor
    # requirement and can use AMD or Intel's default Vulkan adapter as well.
    device='vulkan=okk_vk:NVIDIA' if decode_mode=='cuda' else 'vulkan=okk_vk'
    global_options=('-init_hw_device',device,'-filter_hw_device','okk_vk')
    if decode_mode=='vulkan':
        input_options=('-hwaccel','vulkan','-hwaccel_device','okk_vk','-hwaccel_output_format','vulkan');upload=''
    elif decode_mode=='cuda':
        input_options=('-hwaccel','cuda','-hwaccel_device',str(gpu_index),'-hwaccel_output_format','cuda')
        source_surface='p010le' if getattr(media,'bit_depth',8)>8 else 'nv12'
        # Resize while frames are still CUDA-resident. Bridging an 8K P010
        # frame to system RAM and Vulkan would otherwise transfer ~100MB in
        # each direction per frame just to deliver 4K. Preserve source depth
        # through the bridge; libplacebo performs the requested output depth
        # conversion after floating-point grading.
        source_width=getattr(media,'width',0);source_height=getattr(media,'height',0)
        prescale=''
        if source_width>0 and source_height>0 and (source_width,source_height)!=(width,height):
            if not caps.scale_cuda:
                raise GpuExportUnsupported('此 FFmpeg 缺少 CUDA 缩放组件，保留原有导出路径。')
            if surface=='p010le':source_surface='p010le'
            prescale=(f'scale_cuda=w={width}:h={height}:interp_algo=bicubic:format={source_surface},')
        upload=prescale+f'hwdownload,format={source_surface},hwupload,'
    else:input_options=();upload='hwupload,'
    signal,color_options=_signal_options(media)
    pipeline_cache=Path(shader).parent/'pipeline';pipeline_cache.mkdir(parents=True,exist_ok=True)
    render=[f'w={width}',f'h={height}',f'format={surface}','fit_mode=contain','fillcolor=black',
            *(['inherit_device=1'] if caps.inherit_device else []),'deband=0','peak_detect=0','apply_filmgrain=0','apply_dolbyvision=0',
            'dithering=none',f'shader_cache={escape_filter_path(str(pipeline_cache/"pl-"))}',
            *([f'custom_shader_path={escape_filter_path(shader)}'] if active_grade else []),*signal]
    trim=(f'trim=start_frame={prefix_frames}:end_frame={prefix_frames+frames}' if prefix_frames else f'trim=end_frame={frames}')
    chain=(f'setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,'+upload+'libplacebo='+':'.join(render)
        +f',setsar=1,tpad=stop_mode=clone:stop_duration={duration+1:.9f},{trim},settb=AVTB,setpts=N/({rate_text}*TB)')
    if download:chain+=f',hwdownload,format={surface}'
    return GpuFilterPlan(caps.ffmpeg_path,global_options,input_options,chain,shader,surface,decode_mode,color_options)


def build_gpu_transition_command(job,project,base,settings,width,height,pixel_format,rate_text,encode_args,
                                 chunk,ass_path=None,*,source_frame_rates=None,media_by_path=None,
                                 decode_mode='vulkan',shader_dir=None):
    """Same frame planning/grade order as creative_render's CPU command.

    The builder is retained for a future verified Vulkan runtime. The current
    production whitelist is empty: all nine effects retain CPU compositing
    because two-input regression exposed runtime access violations. Hardware
    capability alone cannot enable this route. Encoding arguments remain
    supplied by the caller, unchanged.
    """
    from program_edits import grade_at
    from subtitle_core import subtitle_filter
    if getattr(job, 'mask_path', ''):
        raise GpuExportUnsupported('资源遮罩转场使用精确缓存合成。')
    if (job.frames<1 or job.full_frames<1 or job.trim_start_frames<0
            or job.trim_start_frames+job.frames>job.full_frames):
        raise ValueError('转场渲染帧范围无效')
    if not base:raise ValueError('导出命令不能为空')
    caps=detect_gpu_export(base[0]);kind=transition_filter(job.kind)
    if kind not in VERIFIED_GPU_TRANSITIONS:
        raise GpuExportUnsupported(f'转场 {kind} 的 GPU 合成尚未通过稳定性验证，使用原有 CPU 转场。')
    if not caps.xfade_vulkan or kind not in caps.transition_kinds:
        raise GpuExportUnsupported(f'转场 {kind} 尚未支持 GPU 导出，保留此转场的 CPU 路径。')
    rate=Fraction(rate_text);fps=float(project.fps)
    if not math.isfinite(fps) or fps<=0:raise ValueError('转场帧率无效')
    full_duration=job.full_frames/fps;prefix=0
    if job.phase_origin is not None and job.phase_origin<job.full_start:
        offset=math.floor((job.full_start-job.phase_origin)*fps+.5)
        source_rate=(source_frame_rates or {}).get(job.path)
        quantum=(rate/Fraction(source_rate)).numerator if source_rate else None
        prefix=offset%quantum if quantum else offset
    prefix_seconds=prefix/fps;plans=[];graph=[]
    cut=math.floor(job.full_start*fps+.5)+job.full_frames//2
    for index,(camera,path) in enumerate(((job.camera,job.path),(job.other_camera,job.other_path))):
        grade_time=(cut-1 if index==0 else cut)/fps
        grades=(project.camera_grades.get(camera,ColorGrade()),grade_at(project,grade_time),project.global_grade)
        # This packaged FFmpeg's xfade_vulkan crashes after linking when a
        # no-op libplacebo pass is consumed. Real graded passes are validated.
        # Keep an explicit fallback rather than perturbing neutral pixels just
        # to force the renderer to allocate a non-trivial intermediate image.
        if all(grade is None or grade_is_identity(grade) for grade in grades):
            raise GpuExportUnsupported('当前运行库的无调色转场路径不稳定，此转场保留 CPU 合成。')
        plan=build_gpu_video_plan(grades,ffmpeg_path=caps.ffmpeg_path,width=width,height=height,
            pixel_format=pixel_format,rate_text=rate_text,frames=job.full_frames,
            duration=full_duration+prefix_seconds,media=(media_by_path or {}).get(path),
            prefix_frames=prefix,decode_mode=decode_mode,shader_dir=shader_dir,download=False)
        plans.append(plan);graph.append(f'[{index}:v:0]{plan.filter_chain}[camera{index}]')
    command=list(base)+list(plans[0].global_options)+['-filter_complex_threads','1']
    for plan,path,start in ((plans[0],job.path,job.source_start),(plans[1],job.other_path,job.other_source_start)):
        command+=list(plan.input_options)+['-ss',f'{max(0,start-prefix_seconds):.9f}','-i',str(path)]
    mixed=(f'[camera0][camera1]xfade_vulkan=transition={kind}:duration={full_duration:.9f}:offset=0,'
        f'trim=start_frame={job.trim_start_frames}:end_frame={job.trim_start_frames+job.frames},'
        f'setpts=N/({rate_text}*TB),hwdownload,format={plans[0].download_format}')
    if ass_path is not None:
        mixed+=(f',setpts=PTS+{job.start:.12g}/TB,'+subtitle_filter(ass_path)+',setpts=PTS-STARTPTS')
    graph.append(mixed+f',format={pixel_format}[vout]')
    command+=['-filter_complex',';'.join(graph),'-map','[vout]','-an','-sn','-dn',*encode_args,
        '-pix_fmt',pixel_format,'-r',str(rate_text),'-frames:v',str(job.frames),
        *plans[0].color_options,'-map_metadata','-1','-video_track_timescale',str(rate.numerator),str(chunk)]
    return command
