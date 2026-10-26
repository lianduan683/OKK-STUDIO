"""Small, repeatable display-signal analysis for selected-cut colour presets.

This is deliberately image-statistics based assistance. It neither infers a
camera's Log transfer function from its model name nor detects people's faces.
Raw/proxy/stabilized pixels are sampled before saved colour grades, so using a
preset again does not analyse and boost its own previous result.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import math
from pathlib import Path
import subprocess
import time

from creative_core import ColorGrade, validate_grade, grade_filter
from exporter_core import CREATE_NO_WINDOW, ExportCancelled, probe_media
from reference_color import white_balance_plan, reference_match_summary


PRESET_LABELS = {
    'natural': '自然微亮',
    'portrait_natural': '人物自然美白',
    'portrait_clear': '人物通透美白',
}
NORMAL_MIDTONE = .45
MAX_SAMPLES = 7


@dataclass(frozen=True, slots=True)
class AutoColorAnalysis:
    samples_requested: int
    samples_used: int
    sample_times: tuple[float, ...]
    pixel_count: int
    usable_pixels: int
    luma_p10: float
    luma_p50: float
    luma_p90: float
    luma_p98: float
    luma_mean: float
    neutral_fraction: float
    skin_fraction: float
    exposure_correction_ev: float
    temperature: float
    tint: float
    histogram: tuple[int, ...]
    usable: bool
    warnings: tuple[str, ...] = ()
    reference_luma: float = 0.
    neutral_rgb: tuple[float, ...] = ()
    neutral_count: int = 0
    bright_neutral_rgb: tuple[float, ...] = ()
    bright_neutral_fraction: float = 0.
    bright_neutral_count: int = 0
    skin_rgb: tuple[float, ...] = ()
    skin_count: int = 0
    rgb_peak_p98: float = 0.
    rgb_peak_p995: float = 0.
    rgb_bins: tuple[tuple[float, float, float, int], ...] = ()

    @property
    def recommended_exposure_ev(self):
        return _exposure_plan(self, .25)[0] if self.usable else 0.

    @property
    def summary(self):
        if not self.usable:
            return '未找到可靠亮度参考，保持当前调色；请避开全黑、全白或空白画面。'
        exposure, gamma, protected = _exposure_plan(self, .25)
        text = (f'已分析 {self.samples_used} 帧 · 正常曝光估计 {self.exposure_correction_ev:+.2f} EV'
                f' · 微亮偏好 +0.25 EV · 建议曝光 {exposure:+.2f} EV')
        if protected: text += ' · 已限制高光溢出'
        if gamma > 1.01: text += ' · 温和提亮中间调'
        if self.neutral_fraction < .015: text += ' · 无可靠中性参考，保留白平衡'
        return text


def _finite(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label}必须在 {low:g} 到 {high:g} 之间')
    return float(value)


def _percentile(histogram, fraction):
    total = sum(histogram)
    if not total: return 0.
    threshold = max(1, math.ceil(total * fraction))
    accumulated = 0
    for index, count in enumerate(histogram):
        accumulated += count
        if accumulated >= threshold: return index / 255.
    return 1.


def _exposure_plan(analysis, bias_ev):
    """Estimate normal exposure, add the user's bias, then protect highlights."""
    desired = max(-2.25, min(2.25, analysis.exposure_correction_ev + bias_ev))
    exposure = desired
    protected = False
    highlight=max(analysis.luma_p98,analysis.rgb_peak_p98,analysis.rgb_peak_p995)
    if exposure > 0 and highlight > .02:
        # Ignore only tiny specular highlights, but don't wash out white clothing,
        # windows or large bright regions. Curves after the exposure clamp
        # cannot recover those pixels, so limit gain before it happens.
        headroom = math.log2(.985 / highlight)
        if exposure > max(0., headroom):
            exposure = max(0., headroom)
            protected = True
    gamma = 1.
    reference = analysis.reference_luma or analysis.luma_p50
    if protected and desired > exposure and reference > .02:
        current_mid = max(.02, min(.95, reference * 2.**exposure))
        wanted_mid = max(.05, min(.75, reference * 2.**desired))
        if wanted_mid > current_mid:
            gamma = max(1., min(1.25, math.log(current_mid) / math.log(wanted_mid)))
    return exposure, gamma, protected


def _parse_ppm(data):
    """Read the one P6 frame emitted by FFmpeg, without an imaging dependency."""
    index = 0
    tokens = []
    while len(tokens) < 4:
        while index < len(data) and data[index] in b' \t\r\n': index += 1
        if index >= len(data): raise ValueError('无法读取自动调色抽样帧')
        if data[index] == 35:
            stop = data.find(b'\n', index)
            if stop < 0: raise ValueError('抽样帧头无效')
            index = stop + 1
            continue
        stop = index
        while stop < len(data) and data[stop] not in b' \t\r\n': stop += 1
        tokens.append(data[index:stop]); index = stop
    if tokens[0] != b'P6' or tokens[3] != b'255': raise ValueError('抽样图像格式无效')
    width, height = int(tokens[1]), int(tokens[2])
    if not 1 <= width <= 256 or not 1 <= height <= 160: raise ValueError('抽样图像尺寸无效')
    if index >= len(data) or data[index] not in b' \t\r\n': raise ValueError('抽样帧头缺少分隔符')
    if data[index:index+2] == b'\r\n': index += 2
    else: index += 1
    pixels = data[index:index+width*height*3]
    if len(pixels) != width*height*3: raise ValueError('抽样帧数据不完整')
    return pixels


def _sample_pixels(ffmpeg, path, position, cancel_event, sample_filter=''):
    filters=','.join(value for value in (sample_filter,
        'scale=256:160:force_original_aspect_ratio=decrease:force_divisible_by=2,format=rgb24') if value)
    command = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '2',
               '-ss', f'{max(0.,position):.9f}', '-i', str(path), '-map', '0:v:0',
               '-an', '-sn', '-dn', '-frames:v', '1',
               '-vf', filters,
               '-c:v', 'ppm', '-f', 'image2pipe', 'pipe:1']
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic()+25.
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ExportCancelled('已取消自动调色分析')
            if time.monotonic() > deadline: raise RuntimeError('读取自动调色抽样帧超时')
            try:
                output, error = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired: pass
        if process.returncode or not output:
            raise RuntimeError('无法抽取画面：'+error.decode('utf-8','replace')[-500:])
        if len(output)>1024*1024: raise RuntimeError('自动调色抽样输出超出预期')
        return _parse_ppm(output)
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill();process.communicate(timeout=2)


def analyze_rgb_samples(samples, *, sample_times=(), samples_requested=None, warnings=()):
    """Pure statistics entry point, also useful for deterministic verification."""
    histogram = [0]*256
    useful = [0]*256
    count = neutral_count = skin_count = 0
    neutral_sums = [0.,0.,0.]
    cohort_histograms={name:[[0]*256 for _ in range(3)] for name in ('neutral','bright_neutral','skin')}
    cohort_counts={name:0 for name in cohort_histograms}
    peak_histogram=[0]*256
    rgb_bins={}
    frame_medians = []
    used = 0
    for pixels in samples:
        if len(pixels)%3: raise ValueError('RGB 抽样帧字节数量无效')
        if not pixels: continue
        frame_hist = [0]*256
        used += 1
        for index in range(0,len(pixels),3):
            raw=(pixels[index],pixels[index+1],pixels[index+2])
            red,green,blue = (value/255. for value in raw)
            luma = .2126*red+.7152*green+.0722*blue
            slot = min(255,max(0,round(luma*255)))
            histogram[slot]+=1;frame_hist[slot]+=1;count+=1
            hi,lo=max(red,green,blue),min(red,green,blue)
            peak_histogram[max(raw)]+=1
            key=tuple(value>>4 for value in raw)
            bucket=rgb_bins.setdefault(key,[0,0,0,0]);bucket[0]+=1
            for channel,value in enumerate(raw):bucket[channel+1]+=value
            saturation=(hi-lo)/hi if hi else 0.
            if .02 <= luma <= .98: useful[slot]+=1
            if .12 <= luma <= .85 and saturation <= .22:
                neutral_count+=1
                for channel,value in enumerate((red,green,blue)): neutral_sums[channel]+=value
                cohort_counts['neutral']+=1
                for channel,value in enumerate(raw):cohort_histograms['neutral'][channel][value]+=1
            if .72 <= luma <= .975 and saturation <= .26 and hi<.995:
                cohort_counts['bright_neutral']+=1
                # A pale face or dyed hair should not outvote the small white
                # clothing areas in a close-up. Weight the least chromatic
                # bright samples, rather than their image coverage alone.
                weight=max(1,round((1-saturation/.27)**4*128))
                for channel,value in enumerate(raw):cohort_histograms['bright_neutral'][channel][value]+=weight
            # A diagnostic colour-range proportion, never a face/skin identity
            # claim. The actual skin treatment has its own smooth RGB mask.
            cb=.5-.168736*red-.331264*green+.5*blue
            cr=.5+.5*red-.418688*green-.081312*blue
            warm_ratio=(green-blue)/(red-blue) if red-blue>.005 else -10.
            if (.16 <= luma <= .88 and .035 <= hi-lo <= .38 and red-green>.015
                    and red-blue>.005 and .32<=cb<=.54 and .52<=cr<=.70
                    and warm_ratio<.63):
                skin_count+=1
                cohort_counts['skin']+=1
                for channel,value in enumerate(raw):cohort_histograms['skin'][channel][value]+=1
        frame_medians.append(_percentile(frame_hist,.5))
    if not count: raise ValueError('所选范围没有可供分析的视频画面')
    notes=list(warnings)
    median = _percentile(useful,.5)
    p10,p50,p90,p98=(_percentile(histogram,p) for p in (.10,.5,.90,.98))
    useful_count=sum(useful)
    usable = useful_count >= max(16,count*.01) and not (p50>.95 and p98-p10<.025)
    correction = max(-2.,min(2.,math.log2(NORMAL_MIDTONE/max(.02,median)))) if usable else 0.
    temperature=tint=0.
    neutral_fraction=neutral_count/count
    if neutral_count >= 128 and neutral_fraction >= .015:
        means=[value/neutral_count for value in neutral_sums]
        reference=max(.1,sum(means)/3.)
        temperature=max(-.20,min(.20,(means[2]-means[0])/reference*.5))
        tint=max(-.12,min(.12,((means[0]+means[2])/2.-means[1])/reference*.5))
    else: notes.append('缺少可靠中性参考，未自动改变白平衡。')
    if len(frame_medians)>1 and max(frame_medians)-min(frame_medians)>.30:
        notes.append('片段内光线变化较大，可在变化处 Cut 后分段自动调色。')
    if used < 3: notes.append('可用抽样少于 3 帧，建议回放检查结果。')
    if not usable: notes.append('全黑、近白或空白画面缺少可靠曝光参考，保持当前调色。')
    if correction>=1.95: notes.append('暗部提升已保守限幅，深暗画面可能仍需手动调整并检查噪点。')
    result = AutoColorAnalysis(samples_requested or used,used,tuple(sample_times),count,useful_count,
        p10,p50,p90,p98,sum(i*n for i,n in enumerate(histogram))/(255.*count),neutral_fraction,
        skin_count/count,correction,temperature,tint,tuple(histogram),usable,tuple(dict.fromkeys(notes)),median)
    cohort_rgb={name:tuple(_percentile(channel,.5) for channel in histograms)
                if cohort_counts[name] else () for name,histograms in cohort_histograms.items()}
    result=replace(result,neutral_rgb=cohort_rgb['neutral'],neutral_count=cohort_counts['neutral'],
        bright_neutral_rgb=cohort_rgb['bright_neutral'],bright_neutral_fraction=cohort_counts['bright_neutral']/count,
        bright_neutral_count=cohort_counts['bright_neutral'],skin_rgb=cohort_rgb['skin'],skin_count=cohort_counts['skin'],
        rgb_peak_p98=_percentile(peak_histogram,.98),
        rgb_peak_p995=_percentile(peak_histogram,.995),
        rgb_bins=tuple((bucket[1]/(255*bucket[0]),bucket[2]/(255*bucket[0]),bucket[3]/(255*bucket[0]),bucket[0])
                       for bucket in rgb_bins.values()))
    if usable and _exposure_plan(result,.25)[2]:
        result=replace(result,warnings=(*result.warnings,'高光保护限制了正曝光增益，并温和提亮中间调。'))
    return result


def sample_analyze(project,start,end,ffmpeg,ffprobe='',*,cancel_event=None,progress=None,sample_grade=None):
    start=_finite(start,'分析开始',0,1e12);end=_finite(end,'分析结束',0,1e12)
    fps=_finite(project.fps,'工程帧率',.001,1000)
    if end<=start or end>project.duration+1/fps: raise ValueError('自动调色范围无效')
    if cancel_event is not None and cancel_event.is_set(): raise ExportCancelled('已取消自动调色分析')
    first,last=math.floor(start*fps+.5),math.floor(end*fps+.5)
    if last <= first: raise ValueError('自动调色范围不足一帧')
    total=min(MAX_SAMPLES,max(1,last-first))
    frames=sorted({min(last-1,first+int((index+.5)*(last-first)/total)) for index in range(total)})
    samples=[];times=[];notes=[];seen=set();probed=set()
    sample_filter=''
    if sample_grade is not None:
        validate_grade(sample_grade,check_lut=False)
        if sample_grade.lut_path:
            notes.append('参考机位 LUT 不在当前实时显示中，按实时色轮与曲线校色匹配。')
        sample_filter=grade_filter(replace(sample_grade,lut_path=''))
    for index,frame in enumerate(frames):
        if cancel_event is not None and cancel_event.is_set(): raise ExportCancelled('已取消自动调色分析')
        position=frame/fps;camera=project.camera_at(position)
        if camera is None: continue
        media=project.media_at(camera,position,use_proxies=True)
        if media is None: continue
        path,source_time,_=media
        identity=(str(path),round(source_time*fps))
        if identity in seen: continue
        seen.add(identity)
        if not Path(path).is_file():
            notes.append('部分原片或代理已移动，跳过对应抽样。');continue
        if ffprobe and str(path) not in probed:
            probed.add(str(path))
            try:
                info=probe_media(str(ffprobe),str(path))
                if info.color_transfer in ('smpte2084','arib-std-b67'):
                    notes.append('素材带 HDR 传递函数；此功能不自动转换 Log/HDR，请核对色彩空间。')
            except (OSError,RuntimeError,ValueError): pass
        try:
            pixels=(_sample_pixels(ffmpeg,path,source_time,cancel_event,sample_filter) if sample_filter else
                    _sample_pixels(ffmpeg,path,source_time,cancel_event))
        except ExportCancelled: raise
        except (OSError,RuntimeError,ValueError) as error:
            notes.append(str(error));continue
        samples.append(pixels);times.append(position)
        if progress: progress((index+1)/len(frames)*100,f'自动调色分析 {index+1}/{len(frames)}')
    if not samples: raise ValueError('所选片段没有可读取的画面，无法自动调色。')
    if len(samples)<len(frames): notes.append(f'可用抽样 {len(samples)}/{len(frames)} 帧，已跳过无画面或无法读取的时刻。')
    return analyze_rgb_samples(samples,sample_times=times,samples_requested=len(frames),warnings=notes)


def _balanced_analysis(analysis,gains):
    if not analysis.rgb_bins or all(abs(gain-1)<1e-9 for gain in gains):return analysis
    histogram=[0]*256;useful=[0]*256;peak=[0]*256
    for red,green,blue,count in analysis.rgb_bins:
        values=[value*gain for value,gain in zip((red,green,blue),gains)]
        y=sum(value*weight for value,weight in zip(values,(.2126,.7152,.0722)))
        index=max(0,min(255,round(y*255)))
        histogram[index]+=count;peak[max(0,min(255,round(max(values)*255)))]+=count
        if .02<=y<=.98:useful[index]+=count
    median=_percentile(useful,.5)
    return replace(analysis,histogram=tuple(histogram),luma_p10=_percentile(histogram,.1),
        luma_p50=_percentile(histogram,.5),luma_p90=_percentile(histogram,.9),luma_p98=_percentile(histogram,.98),
        rgb_peak_p98=_percentile(peak,.98),rgb_peak_p995=_percentile(peak,.995),reference_luma=median,
        exposure_correction_ev=max(-2.,min(2.,math.log2(NORMAL_MIDTONE/max(.02,median)))))


def _reference_skin_gamma(analysis,reference_analysis,gains,exposure,gamma,confidence):
    if (reference_analysis is None or not analysis.skin_rgb or not reference_analysis.skin_rgb
            or analysis.skin_count<32 or reference_analysis.skin_count<32):return gamma
    current=[max(.005,min(.985,value*gain*2.**exposure))
             for value,gain in zip(analysis.skin_rgb,gains)]
    y=sum(value*weight for value,weight in zip(current,(.2126,.7152,.0722)))
    desired=min(.76,sum(value*weight for value,weight in zip(reference_analysis.skin_rgb,(.2126,.7152,.0722))))
    current_y=sum(value**(1/gamma)*weight for value,weight in zip(current,(.2126,.7152,.0722)))
    if .08<y<.85 and desired>current_y+.025:
        proposed=math.log(y)/math.log(max(.1,desired))
        return max(gamma,min(1.40,1+(proposed-1)*(.5+.3*confidence)))
    return gamma


def build_auto_grade(analysis,preset='natural',base_grade=None,bias_ev=.25,strength=1,reference_analysis=None):
    if not isinstance(analysis,AutoColorAnalysis): raise ValueError('自动调色分析结果无效')
    if preset not in PRESET_LABELS: raise ValueError('未知自动调色预设')
    bias_ev=_finite(bias_ev,'曝光偏好',-1,1);strength=_finite(strength,'预设强度',0,1)
    base=base_grade or ColorGrade();validate_grade(base,check_lut=False)
    if not analysis.usable: return replace(base)
    plan=white_balance_plan(analysis,reference_analysis,strength=strength)
    balanced=_balanced_analysis(analysis,plan.gains)
    exposure,gamma,_=_exposure_plan(balanced,bias_ev)
    portrait=preset!='natural'
    if portrait and plan.used_reference:
        gamma=_reference_skin_gamma(analysis,reference_analysis,plan.gains,exposure,gamma,plan.confidence)
    target={'exposure':exposure*strength,'temperature':0. if plan.source else analysis.temperature*strength,
            'tint':0. if plan.source else analysis.tint*strength,'gamma':1+(gamma-1)*strength,
            'contrast':1+((.97 if preset=='portrait_natural' else .94 if preset=='portrait_clear' else 1.01)-1)*strength,
            'saturation':1+((.99 if preset=='portrait_natural' else .98 if preset=='portrait_clear' else 1.025)-1)*strength,
            'shadows':(.02 if portrait else 0.)*strength,
            'highlights':(-.03 if portrait else -.01)*strength,'enabled':True}
    available={item.name for item in fields(ColorGrade)}
    if {'skin_lighten','skin_saturation'}<=available:
        target.update(skin_lighten=(.35 if preset=='portrait_natural' else .55 if preset=='portrait_clear' else 0.)*strength,
                      skin_saturation=(.14 if preset=='portrait_natural' else .24 if preset=='portrait_clear' else 0.)*strength)
    elif portrait: raise ValueError('此版本尚未加载人物肤色调色引擎')
    if {'wb_red','wb_green','wb_blue'}<=available:
        target.update(zip(('wb_red','wb_green','wb_blue'),plan.gains))
    if 'skin_deyellow' in available:
        amount=(.30 if plan.used_reference else .40) if preset=='portrait_natural' else (
               .55 if plan.used_reference else .75) if preset=='portrait_clear' else 0.
        target['skin_deyellow']=amount*strength
    grade=replace(base,**target)
    validate_grade(grade,check_lut=False)
    return grade
