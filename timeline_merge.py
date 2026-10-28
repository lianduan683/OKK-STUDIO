"""Join source recordings without re-encoding or modifying original files.

Planning/model publication are fast and contain no I/O. Rendering belongs on a
worker thread; it checks complete streams before making a persistent new file.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from exporter_core import CREATE_NO_WINDOW, ExportCancelled
from creative_core import ColorGrade
from multicam_core import CameraClip
from timeline_edits import finalize_source_edit


@dataclass(frozen=True)
class SourceMergePlan:
    sources: tuple[CameraClip, ...]

    @property
    def source_ids(self): return tuple(clip.id for clip in self.sources)
    @property
    def camera(self): return self.sources[0].camera
    @property
    def start(self): return self.sources[0].start
    @property
    def duration(self): return math.fsum(clip.duration for clip in self.sources)
    @property
    def shifts(self):
        position=self.start; result=[]
        for clip in self.sources:
            result.append(position-clip.start);position+=clip.duration
        return tuple(result)


@dataclass(frozen=True)
class SourceMergeResult:
    path: str
    duration: float
    source_durations: tuple[float, ...]


def _check_processed(project, identifiers):
    if any(item.source_id in identifiers for item in project.stabilizations):
        raise ValueError('所选素材已应用防抖，请先移除防抖后再合并分段，避免缓存对应到错误画面。')
    if any(item.source_id in identifiers for item in project.library_overlays):
        raise ValueError('所选素材已应用叠加特效，请先移除该素材特效后再合并分段。')


def _check_camera_grades(project, sources):
    def effective(camera):
        grade=project.camera_grades.get(camera,ColorGrade())
        return grade if grade.enabled else ColorGrade()
    reference=effective(sources[0].camera)
    if any(effective(clip.camera)!=reference for clip in sources[1:]):
        raise ValueError('所选素材所在机位使用了不同的机位调色，请先统一机位调色再合并，避免效果丢失。')


def plan_source_merge(project, selected_ids):
    if isinstance(selected_ids,(str,bytes)):raise ValueError('请选择多个素材 ID。')
    identifiers=set(selected_ids)
    # Python's stable sort retains import order when batch-imported camera
    # parts all start at zero. UUID lexical order is not recording order.
    sources=sorted((clip for clip in project.clips if clip.id in identifiers),key=lambda clip:clip.start)
    if len(sources)<2:raise ValueError('请按住 Ctrl 选择至少两个源素材片段。')
    if len(sources)!=len(identifiers):raise ValueError('部分所选素材已不存在，请重新选择。')
    if any(not math.isfinite(clip.duration) or clip.duration<=0 for clip in sources):
        raise ValueError('所选素材的时长无效，请重新导入后再合并。')
    _check_processed(project,identifiers)
    _check_camera_grades(project,sources)
    return SourceMergePlan(tuple(copy.deepcopy(sources)))


def apply_source_merge(project, plan, result):
    current={clip.id:clip for clip in project.clips}
    if any(current.get(source.id)!=source for source in plan.sources):
        raise ValueError('合并期间所选素材已发生变化，原时间线已保留；请重新选择后合并。')
    _check_processed(project,set(plan.source_ids))
    _check_camera_grades(project,plan.sources)
    if (not isinstance(result.path,str) or not result.path or not math.isfinite(result.duration)
            or result.duration<=0 or len(result.source_durations)!=len(plan.sources)):
        raise ValueError('合并结果的路径或时长无效。')
    if any(not math.isfinite(value) or value<=0 for value in result.source_durations):
        raise ValueError('合并结果包含无效分段时长。')
    if abs(result.duration-math.fsum(result.source_durations))>max(2/project.fps,.1):
        raise ValueError('合并结果与分段总时长不一致，未替换时间线。')
    if os.path.normcase(os.path.abspath(result.path)) in {
            os.path.normcase(os.path.abspath(source.path)) for source in plan.sources}:
        raise ValueError('合并结果必须是新文件，不能替换源素材。')
    candidate=copy.deepcopy(project)
    merged=replace(plan.sources[0],path=result.path,duration=result.duration,proxy_path='')
    selected=set(plan.source_ids)
    candidate.clips=[merged if clip.id==merged.id else clip for clip in candidate.clips
                     if clip.id==merged.id or clip.id not in selected]
    vanished={source.camera for source in plan.sources}-set(candidate.cameras)
    for cut in candidate.cuts:
        if cut.camera in vanished:cut.camera=merged.camera
    if candidate.alignment_reference_camera in vanished:candidate.alignment_reference_camera=merged.camera
    if candidate.color_reference_camera in vanished:candidate.color_reference_camera=merged.camera
    try:finalize_source_edit(candidate)
    except ValueError as error:
        raise ValueError('分段接齐后现有工程范围或效果超出了素材，请先调整相关范围再合并：'+str(error)) from error
    return candidate,next(clip for clip in candidate.clips if clip.id==merged.id)


def _cancelled(event):
    if event is not None and event.is_set():raise ExportCancelled('已取消合并，原素材和时间线保持不变。')


def _run(command, cancel_event, *, timeout=None, progress=None, duration=0.):
    _cancelled(cancel_event)
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,creationflags=CREATE_NO_WINDOW)
    began=time.monotonic();last_report=0.
    try:
        while True:
            _cancelled(cancel_event)
            try:output,error=process.communicate(timeout=.1);break
            except subprocess.TimeoutExpired as pending:
                if timeout is not None and time.monotonic()-began>timeout:
                    raise RuntimeError('读取媒体信息超时，请检查文件是否可正常访问。')
                if progress and duration>0 and time.monotonic()-last_report>.15:
                    text=(pending.output or b'')[-4096:].decode('utf-8',errors='replace')
                    matches=re.findall(r'out_time_us=(\d+)',text)
                    if matches:progress(10+85*min(1.,int(matches[-1])/1_000_000/duration),'正在无损合并分段')
                    last_report=time.monotonic()
        _cancelled(cancel_event)
        if process.returncode:
            raise RuntimeError(error.decode('utf-8',errors='replace')[-3000:] or '媒体合并进程失败。')
        return output,error.decode('utf-8',errors='replace')
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.communicate()


def _probe(ffprobe, path, cancel_event):
    output,_=_run([str(ffprobe),'-v','error','-show_streams','-show_format',
        '-show_data_hash','sha256','-of','json',str(path)],cancel_event,timeout=30)
    try:payload=json.loads(output)
    except (ValueError,TypeError) as error:raise RuntimeError('无法读取素材流信息：'+str(path)) from error
    streams=payload.get('streams',[])
    videos=[stream for stream in streams if stream.get('codec_type')=='video'
            and not stream.get('disposition',{}).get('attached_pic')]
    if len(videos)!=1:raise ValueError('无损分段合并要求每个素材只有一路主视频：'+str(path))
    audio=[stream for stream in streams if stream.get('codec_type')=='audio']
    # Extra data, timecode and thumbnail tracks do not participate in picture/audio.
    return payload,videos[0],audio


def _fraction(value):
    try:return Fraction(str(value))
    except (ValueError,ZeroDivisionError,TypeError):return Fraction(0)


def _duration(payload, video):
    try:value=float(payload['format']['duration'])
    except (ValueError,TypeError,KeyError):value=float(video.get('duration',0))
    if not math.isfinite(value) or value<=0:raise ValueError('素材没有可用的完整时长。')
    return value


def _stream_duration(stream):
    ticks=stream.get('duration_ts');base=_fraction(stream.get('time_base'))
    if ticks is not None and base>0:
        try:return float(int(ticks)*base)
        except (ValueError,TypeError):pass
    try:return float(stream.get('duration',0))
    except (TypeError,ValueError):return 0.


def _video_signature(stream):
    fields=('codec_name','profile','level','width','height','pix_fmt','field_order',
        'sample_aspect_ratio','color_range','color_space','color_transfer','color_primaries','extradata_hash')
    return {key:stream.get(key) for key in fields} | {
        key:_fraction(stream.get(key)) for key in ('r_frame_rate','avg_frame_rate','time_base')}


def _audio_signature(stream):
    fields=('codec_name','profile','sample_fmt','sample_rate','channels','channel_layout','extradata_hash')
    return {key:stream.get(key) for key in fields} | {'time_base':_fraction(stream.get('time_base'))}


FIELD_LABELS={'codec_name':'编码格式','profile':'编码规格','level':'编码级别','width':'画面宽度',
    'height':'画面高度','pix_fmt':'像素格式','field_order':'扫描方式','sample_aspect_ratio':'像素比例',
    'r_frame_rate':'帧率','avg_frame_rate':'平均帧率','time_base':'时间基','sample_rate':'采样率',
    'sample_fmt':'音频采样格式','channels':'声道数','channel_layout':'声道布局','extradata_hash':'编码参数',
    'color_range':'色彩范围','color_space':'色彩空间','color_transfer':'色彩传递','color_primaries':'色彩基色'}


def _compare(expected, actual, name, stream_label):
    for key,value in expected.items():
        if actual.get(key)!=value:
            raise ValueError(f'「{name}」的{stream_label}{FIELD_LABELS.get(key,key)}与第一段不同，无法直接无损合并；请先转为相同格式。')


def _validate_sources(plan, ffprobe, cancel_event, progress):
    metadata=[];durations=[];first_video=None;first_audio=None
    for index,clip in enumerate(plan.sources):
        _cancelled(cancel_event)
        path=Path(clip.path)
        if not path.is_file():raise ValueError('找不到源素材：'+clip.path)
        if any(character in clip.path for character in '\x00\r\n'):raise ValueError('素材路径包含无效换行字符。')
        before=path.stat();payload,video,audio=_probe(ffprobe,path,cancel_event)
        fps=_fraction(video.get('avg_frame_rate')) or _fraction(video.get('r_frame_rate'))
        if fps<=0:raise ValueError('无法确定素材帧率：'+path.name)
        tolerance=max(1/float(fps),.025)+1e-5
        duration=_duration(payload,video)
        if abs(duration-clip.duration)>tolerance:
            raise ValueError(f'「{path.name}」的时间线时长与原文件不同，请重新导入完整分段后再无损合并。')
        video_duration=_stream_duration(video)
        if video_duration and abs(duration-video_duration)>tolerance:
            raise ValueError(f'「{path.name}」的视频与文件时长不一致，请先修复该素材的时间戳。')
        try:video_start=float(video.get('start_time',0))
        except (ValueError,TypeError):video_start=0.
        for stream in audio:
            try:audio_start=float(stream.get('start_time',0))
            except (ValueError,TypeError):audio_start=0.
            audio_duration=_stream_duration(stream)
            if abs(audio_start-video_start)>tolerance or (audio_duration and abs(audio_duration-duration)>tolerance):
                raise ValueError(f'「{path.name}」的音画起点或时长不一致，请先修复后再合并，避免拼接后音画偏移。')
        if index==0:first_video=_video_signature(video);first_audio=[_audio_signature(stream) for stream in audio]
        else:
            _compare(first_video,_video_signature(video),path.name,'视频')
            if len(audio)!=len(first_audio):
                raise ValueError(f'「{path.name}」的音轨数量与第一段不同，不能混合带声音和无声音的分段。')
            for reference,stream in zip(first_audio,audio):_compare(reference,_audio_signature(stream),path.name,'音频')
        metadata.append((path,before.st_size,before.st_mtime_ns,video,audio))
        durations.append(duration)
        if progress:progress(10*(index+1)/len(plan.sources),'正在检查分段格式和音画时间戳')
    return metadata,tuple(durations)


def render_source_merge(plan, ffmpeg, ffprobe, output_dir, *, cancel_event=None, progress=None):
    if len(plan.sources)<2:raise ValueError('至少需要两个源素材。')
    metadata,durations=_validate_sources(plan,ffprobe,cancel_event,progress)
    total=math.fsum(durations)
    output_dir=Path(output_dir).resolve();output_dir.mkdir(parents=True,exist_ok=True)
    video,audio=metadata[0][3:]
    suffix='.mp4' if video.get('codec_name') in ('h264','hevc','av1','mpeg4') and all(
        stream.get('codec_name') in ('aac','mp3','ac3','alac') for stream in audio) else '.mkv'
    basename='合并_'+uuid.uuid4().hex[:12]
    destination=output_dir/(basename+suffix)
    pending=output_dir/(basename+'.pending'+suffix)
    try:
        with tempfile.TemporaryDirectory(prefix='okk_source_join_') as temporary:
            manifest=Path(temporary)/'sources.ffconcat'
            lines=['ffconcat version 1.0']
            for (path,*_),duration in zip(metadata,durations):
                escaped=str(path.resolve()).replace('\\','/').replace("'","'\\''")
                lines.extend(("file '"+escaped+"'",f'duration {duration:.12f}'))
            manifest.write_text('\n'.join(lines)+'\n',encoding='utf-8')
            command=[str(ffmpeg),'-hide_banner','-nostdin','-v','warning','-n','-stats_period','0.2',
                '-f','concat','-safe','0','-i',str(manifest),'-map','0:v:0','-map','0:a?',
                '-c','copy','-map_metadata','0','-progress','pipe:1','-nostats']
            if suffix=='.mp4':command+=['-movflags','+faststart']
            command.append(str(pending))
            _,warnings=_run(command,cancel_event,progress=progress,duration=total)
            if re.search(r'non.?monoton|invalid.*(?:dts|pts)|timestamp.*discontinu',warnings,re.I):
                raise ValueError('分段时间戳不连续，无法安全无损合并；请先转为相同格式后重试。')
        if progress:progress(96.,'正在核验合并文件')
        output,merged_video,merged_audio=_probe(ffprobe,pending,cancel_event)
        actual=_duration(output,merged_video)
        fps=float(_fraction(video.get('avg_frame_rate')) or _fraction(video.get('r_frame_rate')))
        tolerance=max(1/fps,.025)*2
        if abs(actual-total)>tolerance or len(merged_audio)!=len(audio):
            raise ValueError('合并结果的时长或音轨数量不匹配，未替换原时间线。')
        for path,size,modified,_,_ in metadata:
            stamp=path.stat()
            if (stamp.st_size,stamp.st_mtime_ns)!=(size,modified):
                raise ValueError('合并期间源文件发生变化，未替换原时间线：'+path.name)
        _cancelled(cancel_event)
        os.replace(pending,destination)
        if progress:progress(100.,'分段已无损合并')
        return SourceMergeResult(str(destination),actual,durations)
    finally:pending.unlink(missing_ok=True)
