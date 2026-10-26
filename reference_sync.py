"""Align cameras to another camera's anchored audio without moving its track.

Each reference recording retains its existing position on the project clock.
The established middle-window waveform matcher is reused for each source;
there is no temporary stitched soundtrack or change to programme master audio.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
import time

from editor_core import AlignmentResult,align_audio_segments
from exporter_core import CREATE_NO_WINDOW,ExportCancelled


def _get(item,name,default=None):
    return item.get(name,default) if isinstance(item,dict) else getattr(item,name,default)


def _cancel(event):
    if event is not None and event.is_set(): raise ExportCancelled('机位参考同步已取消')


def _has_audio(ffprobe,path,cancel_event=None):
    command=[ffprobe,'-v','error','-select_streams','a','-show_entries','stream=index','-of','json',str(path)]
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             creationflags=CREATE_NO_WINDOW)
    try:
        deadline=time.monotonic()+25
        while True:
            _cancel(cancel_event)
            if time.monotonic()>deadline: raise RuntimeError('检查参考机位音轨超时')
            try:
                output,error=process.communicate(timeout=.1);break
            except subprocess.TimeoutExpired: pass
        if process.returncode:
            raise RuntimeError(error.decode('utf-8','replace').strip()[-300:] or '参考文件不可读取')
        try:return bool(json.loads(output).get('streams'))
        except (ValueError,TypeError):raise RuntimeError('无法读取参考机位音轨信息')
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
        for stream in (process.stdout,process.stderr):
            if stream:stream.close()


def _start(clip):
    value=_get(clip,'start',0.)
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
        raise ValueError('参考或目标片段的时间起点无效')
    return float(value)


def _path(clip):
    value=_get(clip,'path','')
    if not isinstance(value,(str,Path)) or not str(value) or '\x00' in str(value):
        raise ValueError('参考或目标片段的原片路径无效')
    return str(value)


def _manual(clip,reason):
    return AlignmentResult(-_start(clip),0.,50,'参考机位 · 待手动：'+str(reason)[:300])


def align_to_camera(ffmpeg,clips,reference_clips,*,ffprobe_path='',mode='fast',
                    cancel_event=None,progress=None,cache_dir=None):
    """Return one global-clock AlignmentResult for every target clip.

    Existing results express camera start as ``-offset_seconds``. A match
    relative to a reference beginning at T therefore becomes offset-T.
    Ambiguous or weak matches preserve the target's existing position and
    carry zero confidence so the normal UI cannot apply them accidentally.
    """
    clips=list(clips);reference_clips=list(reference_clips)
    if mode not in ('fast','full'):raise ValueError('音频同步模式必须是 fast 或 full')
    if not clips:return []
    if not reference_clips:raise ValueError('参考机位没有素材')
    _cancel(cancel_event)
    ffprobe=str(ffprobe_path or '')
    if not ffprobe:
        sibling=Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix.lower()=='.exe' else 'ffprobe')
        ffprobe=str(sibling) if sibling.is_file() else (shutil.which('ffprobe') or '')
    if not ffprobe:raise ValueError('未找到 ffprobe，无法检查参考机位音轨')
    for item in [*clips,*reference_clips]:_path(item);_start(item)
    last_progress=0.
    def report(value,message):
        nonlocal last_progress
        last_progress=max(last_progress,min(100.,float(value)))
        if progress:progress(last_progress,message)
    report(0.,'检查参考机位原始音轨')
    references=[];errors=[]
    for index,item in enumerate(reference_clips):
        _cancel(cancel_event)
        try:
            if _has_audio(ffprobe,_path(item),cancel_event):references.append(item)
            else:errors.append(Path(_path(item)).name+'：没有音轨')
        except ExportCancelled:raise
        except (OSError,ValueError,RuntimeError) as error:errors.append(Path(_path(item)).name+'：'+str(error))
        report(5*(index+1)/len(reference_clips),f'检查参考机位音轨 {index+1}/{len(reference_clips)}')
    if not references:
        raise ValueError('参考机位没有可读取的音轨：'+'；'.join(errors[:3]))
    reference_ids={(_get(item,'camera'),_get(item,'id')) for item in reference_clips if _get(item,'id') is not None}
    anchored={index for index,item in enumerate(clips)
              if (_get(item,'camera'),_get(item,'id')) in reference_ids and _get(item,'id') is not None}
    targets=[index for index in range(len(clips)) if index not in anchored]
    candidates={index:[] for index in targets}
    batches=0
    for reference_index,reference in enumerate(references):
        _cancel(cancel_event)
        first=5+90*reference_index/len(references);span=90/len(references)
        def update(value,message,base=first,width=span):
            report(base+width*min(100.,max(0.,float(value)))/100,
                   f'参考音轨 {reference_index+1}/{len(references)} · {message}')
        if not targets:break
        try:
            results=align_audio_segments(str(ffmpeg),[_path(clips[index]) for index in targets],_path(reference),
                mode=mode,cache_dir=cache_dir,cancel_event=cancel_event,progress=update,continue_on_error=True)
            if len(results)!=len(targets):raise RuntimeError('音频同步结果与目标片段数量不一致')
        except ExportCancelled:raise
        except (OSError,ValueError,RuntimeError) as error:
            errors.append(Path(_path(reference)).name+'：'+str(error));continue
        batches+=1
        for target_index,result in zip(targets,results):
            offset=float(result.offset_seconds)-_start(reference)
            confidence=float(result.confidence)
            if not math.isfinite(offset) or not math.isfinite(confidence):continue
            confidence=min(1.,max(0.,confidence))
            target=clips[target_index]
            target_duration=float(_get(target,'duration',0.) or 0.)
            reference_duration=float(_get(reference,'duration',0.) or 0.)
            proposed_start=-offset
            if target_duration>0 and reference_duration>0:
                overlap=min(proposed_start+target_duration,_start(reference)+reference_duration)-max(proposed_start,_start(reference))
                if overlap<=0:confidence=0.
            label=f'参考机位 {_get(reference,"camera","?")} / {Path(_path(reference)).name} · {result.engine}'
            candidates[target_index].append(AlignmentResult(offset,confidence,result.envelope_rate,label))
        report(first+span,f'参考音轨 {reference_index+1}/{len(references)} 分析完成')
    if targets and not batches:
        raise ValueError('参考机位音轨均无法可靠分析：'+'；'.join(errors[-3:]))
    output=[]
    for index,clip in enumerate(clips):
        if index in anchored:
            output.append(AlignmentResult(-_start(clip),1.,50,'参考机位锚点，保持当前位置'));continue
        matches=sorted(candidates[index],key=lambda item:item.confidence,reverse=True)
        if not matches or matches[0].confidence<.2:
            reason=matches[0].engine if matches else ('；'.join(errors[-2:]) or '未找到可靠重叠波形')
            output.append(_manual(clip,reason));continue
        best=matches[0]
        tolerance=max(.12,3/max(5,best.envelope_rate))
        contradictory=next((item for item in matches[1:] if item.confidence>=.2
            and item.confidence>=best.confidence*.9 and best.confidence-item.confidence<=.08
            and abs(item.offset_seconds-best.offset_seconds)>tolerance),None)
        if contradictory is not None:
            output.append(_manual(clip,'多个参考片段得到相近可信度但时间位置矛盾，可能是重复声音，请检查'))
        else:output.append(best)
    _cancel(cancel_event)
    report(100.,'参考机位同步完成，参考轨位置保持不变')
    return output
