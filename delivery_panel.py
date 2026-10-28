"""Compact delivery controls shared with the existing exporter settings."""
from __future__ import annotations

from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, ttk

from editor_core import (format_time, normalize_audio_bitrate,
                         normalize_audio_sample_rate, normalize_video_bitrate)
from exporter_core import ExportSettings
from hardware_encoders import (CODEC_LABELS, available_codecs, codec_from_label,
                               codec_label, encoder_vendor)

PANEL = '#292b2e'
INPUT = '#18191b'
INK = '#dedfe3'
MUTED = '#969ba3'
ACCENT = '#58a6d6'
FONT = ('Microsoft YaHei UI', 10)
CONTAINERS = {'MP4': '.mp4', 'MOV': '.mov', 'MKV': '.mkv'}
AUDIO_FORMATS = {
    'AAC': 'aac', '复制主音频': 'copy', '无音频': 'none',
    'PCM 16-bit': 'pcm_s16le', 'PCM 24-bit': 'pcm_s24le',
    'PCM 32-bit': 'pcm_s32le', 'PCM Float32': 'pcm_f32le', 'FLAC': 'flac',
}
CODECS = list(dict.fromkeys(['HEVC / H.265 (NVENC)', 'H.264 (NVENC)', 'AV1 (NVENC)',
                            *CODEC_LABELS.values()]))
PRESETS = ['最快 P1', '极速 P2', '均衡 P4', '高质量 P6', '最高质量 P7']
CPU_PRESETS = {'p1': 'ultrafast', 'p2': 'veryfast', 'p4': 'medium',
               'p6': 'slow', 'p7': 'slower'}


def _ensure(owner, name, kind, default, master):
    if name not in vars(owner):
        setattr(owner, name, kind(master=master, value=default))
    return vars(owner)[name]


def init_delivery_variables(workspace):
    """Use the exporter's live variables, including in standalone Tk tests."""
    ws, app = workspace, workspace.app
    shared = {
        'codec_var': (tk.StringVar, CODECS[0]),
        'preset_var': (tk.StringVar, PRESETS[1]),
        'resolution_var': (tk.StringVar, '保持原分辨率'),
        'depth_var': (tk.StringVar, '自动'),
        'quality_var': (tk.IntVar, 20),
        'audio_var': (tk.StringVar, 'AAC 320 kbps'),
        'maxrate_var': (tk.StringVar, ''),
        'decode_var': (tk.StringVar, 'NVIDIA CUDA（推荐）'),
        'split_var': (tk.StringVar, '驱动自动选择'),
        'parallel_var': (tk.IntVar, 1),
    }
    for name, (kind, default) in shared.items():
        _ensure(app, name, kind, default, ws)
    initial_audio = app.audio_var.get()
    initial_format = '复制主音频' if initial_audio == '复制原音频' else (
        '无音频' if initial_audio == '无音频' else 'AAC')
    initial_suffix = Path(ws.output_var.get()).suffix.lower()
    initial_container = next((key for key, value in CONTAINERS.items()
                              if value == initial_suffix), 'MP4')
    defaults = {
        'delivery_container_var': initial_container,
        'delivery_audio_format_var': initial_format,
        'delivery_audio_bitrate_var': '192k' if '192' in initial_audio else '320k',
        'delivery_audio_sample_rate_var': '48000',
        'delivery_gop_var': '120',
        'delivery_bframes_var': '2',
        'delivery_tune_var': 'hq',
        'delivery_multipass_var': '自动（随预设）',
        'delivery_gpuindex_var': '0',
    }
    for name, default in defaults.items():
        _ensure(ws, name, tk.StringVar, default, ws)
    _ensure(ws,'delivery_auto_parallel_var',tk.BooleanVar,True,ws)
    if '_delivery_path_traces' not in vars(ws):
        ws._delivery_path_updating = False

        def container_changed(*_):
            if ws._delivery_path_updating:
                return
            suffix = CONTAINERS.get(ws.delivery_container_var.get())
            path = ws.output_var.get().strip()
            if suffix and path and Path(path).suffix.lower() != suffix:
                ws._delivery_path_updating = True
                try:
                    ws.output_var.set(str(Path(path).with_suffix(suffix)))
                finally:
                    ws._delivery_path_updating = False

        def output_changed(*_):
            if ws._delivery_path_updating:
                return
            suffix = Path(ws.output_var.get().strip()).suffix.lower()
            name = next((key for key, value in CONTAINERS.items() if value == suffix), None)
            if name and name != ws.delivery_container_var.get():
                ws._delivery_path_updating = True
                try:
                    ws.delivery_container_var.set(name)
                finally:
                    ws._delivery_path_updating = False

        ws._delivery_path_traces = (
            ws.delivery_container_var.trace_add('write', container_changed),
            ws.output_var.trace_add('write', output_changed),
        )


def _integer(variable, title, minimum, maximum):
    try:
        # IntVar.get() truncates a floating point Tcl value. Read its raw value
        # so typing "20.5" cannot silently become CQ 20 or two worker tasks.
        raw = str(variable._tk.globalgetvar(str(variable))).strip()
        if not re.fullmatch(r'[+-]?\d+', raw):
            raise ValueError
        value = int(raw)
    except (ValueError, TypeError, tk.TclError):
        raise ValueError(f'{title}必须是 {minimum}–{maximum} 之间的整数。') from None
    if not minimum <= value <= maximum:
        raise ValueError(f'{title}必须在 {minimum}–{maximum} 之间。')
    return value


def _bitrate(variable, title, *, optional=False, audio=False):
    value = str(variable.get()).strip()
    if not value and optional:
        return ''
    normalized = normalize_audio_bitrate(value) if audio else normalize_video_bitrate(value)
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([kKmMgG]?)', normalized)
    if not match or float(match.group(1)) <= 0:
        raise ValueError(f'{title}必须大于 0，例如 320k 或 80M。')
    return normalized


def make_delivery_settings(workspace):
    """Return validated settings; no selected option is silently discarded."""
    ws, app = workspace, workspace.app
    init_delivery_variables(ws)
    quality = _integer(app.quality_var, 'CQ / CRF', 0, 51)
    factory = vars(app).get('_settings')
    method = getattr(type(app), '_settings', None)
    settings = factory() if callable(factory) else method(app) if callable(method) else ExportSettings()
    # A standalone workspace has no ExporterApp._settings method. Keep the same
    # mapping there so tests and integrations still use the visible controls.
    if not callable(factory) and not callable(method):
        settings.codec = codec_from_label(app.codec_var.get())
        settings.preset = app.preset_var.get().split()[-1].lower()
        settings.resolution = {'保持原分辨率': 'original', '1080p': '1080p',
                               '1440p': '1440p', '4K': '4k', '8K': '8k'}[app.resolution_var.get()]
        settings.bit_depth = app.depth_var.get().replace('自动', 'auto')
        settings.decode_mode = 'cuda' if (settings.codec.endswith('_nvenc') and
            app.decode_var.get().startswith('NVIDIA')) else 'cpu'
        split_text = app.split_var.get()
        match = re.search(r'强制\s*(\d+)\s*路', split_text)
        settings.split_encode = match.group(1) if match else (
            'disabled' if split_text.startswith('单路') or split_text == '关闭' else 'auto')
    if ws.export_mode.get().startswith('CPU'):
        settings.codec = 'libx264'
        settings.decode_mode = 'cpu'
        settings.preset = CPU_PRESETS.get(settings.preset, settings.preset)
    settings.quality = quality
    settings.video_bitrate = _bitrate(ws.bitrate_var, '目标码率', optional=True)
    settings.maxrate = _bitrate(app.maxrate_var, '最大码率', optional=True)
    settings.gop = _integer(ws.delivery_gop_var, '关键帧间隔', 1, 10000)
    settings.bframes = (0 if encoder_vendor(settings.codec) == 'amd' else
                       _integer(ws.delivery_bframes_var, 'B 帧', 0, 4))
    settings.gpu_index = (_integer(ws.delivery_gpuindex_var, 'GPU 索引', 0, 31)
                          if settings.codec.endswith('_nvenc') else 0)
    settings.parallel_workers = _integer(app.parallel_var, '并行任务数', 1, 4)
    settings.auto_parallel = bool(ws.delivery_auto_parallel_var.get())
    settings.tune = ws.delivery_tune_var.get() if settings.codec.endswith('_nvenc') else 'hq'
    if settings.codec.endswith('_nvenc') and settings.tune not in {'hq', 'll', 'ull', 'lossless'}:
        raise ValueError('请选择有效的 NVENC 调优方式。')
    multipass = ws.delivery_multipass_var.get() if settings.codec.endswith('_nvenc') else 'disabled'
    if multipass == '自动（随预设）':
        settings.multipass = 'disabled' if settings.preset in {'p1', 'p2'} else (
            'qres' if settings.preset == 'p4' else 'fullres')
    elif multipass in {'disabled', 'qres', 'fullres'}:
        settings.multipass = multipass
    else:
        raise ValueError('请选择有效的多重分析模式。')
    if settings.codec.startswith('h264_') and settings.bit_depth == '10-bit':
        raise ValueError('H.264 硬件编码仅支持 8-bit；10-bit 请使用可用的 HEVC / AV1 或 CPU H.264。')
    caps = vars(app).get('caps')
    if (caps is not None and getattr(caps, 'hardware_probe_complete', False)
            and settings.codec in getattr(caps, 'hardware_codecs', set())
            and settings.bit_depth == '10-bit'
            and settings.codec not in getattr(caps, 'ten_bit_codecs', set())):
        raise ValueError('当前显卡或驱动未通过此编码器的 10-bit 检测，请改用 8-bit 或其他编码器。')
    if not settings.codec.endswith('_nvenc'):
        settings.decode_mode = 'cpu'
        settings.split_encode = 'disabled'
        settings.multipass = 'disabled'
        settings.gpu_index = 0
    if settings.decode_mode!='cuda' or not settings.codec.endswith('_nvenc'):settings.auto_parallel=False
    suffix = CONTAINERS.get(ws.delivery_container_var.get())
    if suffix is None:
        raise ValueError('请选择 MP4、MOV 或 MKV 容器。')
    audio = AUDIO_FORMATS.get(ws.delivery_audio_format_var.get())
    if audio is None:
        raise ValueError('请选择有效的音频格式。')
    if audio.startswith('pcm_') and suffix not in {'.mov', '.mkv'}:
        raise ValueError('PCM 无损音频请选择 MOV 或 MKV 容器。')
    if audio == 'flac' and suffix != '.mkv':
        raise ValueError('FLAC 音频请选择 MKV 容器。')
    settings.audio = audio
    settings.audio_bitrate = _bitrate(ws.delivery_audio_bitrate_var, '音频码率', audio=True) if audio == 'aac' else ''
    settings.audio_sample_rate = normalize_audio_sample_rate(ws.delivery_audio_sample_rate_var.get()) if audio not in {'copy', 'none'} else None
    if audio == 'aac' and settings.audio_sample_rate and settings.audio_sample_rate > 96000:
        raise ValueError('AAC 最高使用 96 kHz；更高采样率请选择 PCM 或 FLAC。')
    return settings, suffix


def build_delivery_panel(workspace, delivery_frame):
    """Four scrollable pages and a fixed, single primary export action."""
    ws, app = workspace, workspace.app
    init_delivery_variables(ws)
    px = ws._px
    ws.delivery_panel = delivery_frame
    style = ttk.Style(ws)
    style.configure('Delivery.TNotebook', background=PANEL, borderwidth=0)
    style.configure('Delivery.TNotebook.Tab', font=FONT, padding=(px(7), px(6)))
    ws.delivery_tabs = ttk.Notebook(delivery_frame, style='Delivery.TNotebook')
    ws.delivery_tabs.pack(fill='both', expand=True)
    ws.delivery_canvases = []
    controls = {}

    def scroll_page(title):
        outer = tk.Frame(ws.delivery_tabs, bg=PANEL)
        ws.delivery_tabs.add(outer, text=title)
        scrollbar = ttk.Scrollbar(outer, orient='vertical')
        scrollbar.pack(side='right', fill='y')
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0,
                           yscrollcommand=scrollbar.set, width=1, height=1)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.configure(command=canvas.yview)
        inner = tk.Frame(canvas, bg=PANEL)
        window = canvas.create_window(0, 0, anchor='nw', window=inner)
        inner.bind('<Configure>', lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        ws.delivery_canvases.append(canvas)
        # Scope wheel handling to this page, including its child controls.
        wheel_tag = f'DeliveryWheel_{str(canvas)}'
        ws.bind_class(wheel_tag, '<MouseWheel>',
                      lambda event: (canvas.yview_scroll(-int(event.delta / 120), 'units'), 'break')[-1])

        def tag_children(widget):
            widget.bindtags((wheel_tag, *widget.bindtags()))
            for child in widget.winfo_children():
                tag_children(child)

        inner.after_idle(lambda: tag_children(inner) if inner.winfo_exists() else None)
        return inner

    def section(parent, title):
        tk.Label(parent, text=title, bg=PANEL, fg=INK, anchor='w',
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(
                     fill='x', padx=px(11), pady=(px(14), px(5)))

    def note(parent, text='', *, variable=None):
        label = tk.Label(parent, text=text, textvariable=variable, bg=PANEL, fg=MUTED,
                         font=FONT, justify='left', anchor='w', wraplength=px(245))
        label.pack(fill='x', padx=px(11), pady=(px(3), px(6)))
        parent.bind('<Configure>', lambda event: label.configure(wraplength=max(px(80), event.width-px(22))), add='+')
        return label

    def field(parent, title, variable, values=None, *, editable=False, key=None):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill='x', padx=px(11), pady=px(5))
        tk.Label(frame, text=title, bg=PANEL, fg=MUTED, font=FONT,
                 anchor='w').pack(fill='x', pady=(0, px(3)))
        if values is not None:
            widget = ttk.Combobox(frame, textvariable=variable, values=values, width=12,
                                  state='normal' if editable else 'readonly', font=FONT)
        else:
            widget = tk.Entry(frame, textvariable=variable, bg=INPUT, fg=INK,
                              disabledbackground=PANEL, disabledforeground=MUTED,
                              insertbackground=INK, relief='flat', font=FONT,
                              highlightthickness=1, highlightbackground='#3a3c40', highlightcolor=ACCENT)
        widget.pack(fill='x', ipady=px(3) if values is None else 0)
        if key:
            controls[key] = (widget, 'normal' if editable or values is None else 'readonly')
        return widget

    output = scroll_page('成片')
    section(output, '成片范围')
    field(output, '起点', ws.start_var)
    field(output, '终点（留空为完整时间线）', ws.end_var)
    line = tk.Frame(output, bg=PANEL)
    line.pack(fill='x', padx=px(9), pady=px(3))
    ttk.Button(line, text='设为起点', command=lambda: ws.start_var.set(format_time(ws.current)),
               style='Compact.TButton').pack(side='left', fill='x', expand=True, padx=px(2))
    ttk.Button(line, text='设为终点', command=lambda: ws.end_var.set(format_time(ws.current)),
               style='Compact.TButton').pack(side='left', fill='x', expand=True, padx=px(2))
    field(output, '帧率', ws.fps_var, ['23.976', '24', '25', '29.97', '30', '50', '59.94', '60'], editable=True)
    section(output, '输出文件')
    field(output, '容器', ws.delivery_container_var, list(CONTAINERS))
    field(output, '保存位置', ws.output_var)

    def choose_output():
        suffix = CONTAINERS.get(ws.delivery_container_var.get(), '.mp4')
        current = ws.output_var.get().strip()
        options = dict(parent=ws, title='保存多机位成片', defaultextension=suffix,
                       filetypes=[(ws.delivery_container_var.get(), f'*{suffix}')])
        if current:
            options.update(initialdir=str(Path(current).parent), initialfile=Path(current).name)
        path = filedialog.asksaveasfilename(**options)
        if path:
            ws.output_var.set(str(Path(path).with_suffix(suffix)))

    ttk.Button(output, text='选择保存位置…', command=choose_output,
               style='Compact.TButton').pack(fill='x', padx=px(11), pady=px(5))
    note(output, '切换容器时自动更新文件后缀。导出使用原始素材和连续主音轨。')

    video = scroll_page('视频')
    section(video, '编码与质量')
    field(video, '编码设备', ws.export_mode,
          ['自动（沿用统一编码设置）', 'CPU H.264（兼容模式）'])
    field(video, '视频编码', app.codec_var, CODECS, key='codec')
    field(video, '速度 / 画质预设', app.preset_var, PRESETS)
    field(video, '输出分辨率', app.resolution_var, ['保持原分辨率', '1080p', '1440p', '4K', '8K'])
    field(video, '输出位深', app.depth_var, ['自动', '8-bit', '10-bit'])
    quality_title = tk.StringVar(master=ws, value='质量 CQ（0–51，越低越清晰）')
    quality_control = field(video, '', app.quality_var, key='quality')
    quality_control.master.winfo_children()[0].configure(textvariable=quality_title)
    field(video, '目标码率（留空使用 CQ / CRF）', ws.bitrate_var,
          ['20M', '40M', '60M', '80M', '120M', '200M'], editable=True)
    field(video, '最大码率（选填）', app.maxrate_var)
    video_note = note(video)

    audio = scroll_page('音频')
    section(audio, '连续主音轨')
    field(audio, '音频格式', ws.delivery_audio_format_var, list(AUDIO_FORMATS))
    field(audio, 'AAC 码率（可输入任意数值）', ws.delivery_audio_bitrate_var,
          ['96k', '128k', '192k', '256k', '320k', '512k'], editable=True, key='audio_bitrate')
    field(audio, '采样率（Hz，可手动输入）', ws.delivery_audio_sample_rate_var,
          ['保留原采样率', '44100', '48000', '88200', '96000', '192000'], editable=True, key='audio_rate')
    audio_note = note(audio)

    rtx = scroll_page('GPU')
    section(rtx, 'GPU 与并行')
    field(rtx, '解码方式', app.decode_var,
          ['NVIDIA CUDA（推荐）', 'CPU 解码'], key='decode')
    note(rtx, '按检测结果使用 NVENC、AMD AMF 或 Intel QSV。AMF / QSV 使用兼容解码，预览仍可使用 D3D11 硬件解码。')
    split_control = field(rtx, '多 NVENC 分帧', app.split_var,
                          ['驱动自动选择', '关闭'], key='split')
    auto_parallel=tk.Checkbutton(rtx,text='自动安排并行（推荐）',variable=ws.delivery_auto_parallel_var,
        bg=PANEL,fg=INK,selectcolor=INPUT,activebackground=PANEL,activeforeground=INK,font=FONT,anchor='w')
    auto_parallel.pack(fill='x',padx=px(7),pady=px(5))
    controls['auto_parallel']=(auto_parallel,'normal')
    field(rtx, '手动同时编码任务数', app.parallel_var, ['1', '2', '3', '4'],key='parallel')
    split_note = note(rtx)
    advanced_var = tk.BooleanVar(master=ws, value=False)
    advanced = tk.Frame(rtx, bg=PANEL)

    def toggle_advanced():
        if advanced_var.get():
            advanced.pack(fill='x')
        else:
            advanced.pack_forget()

    tk.Checkbutton(rtx, text='高级编码设置', variable=advanced_var, command=toggle_advanced,
                   bg=PANEL, fg=INK, selectcolor=INPUT, activebackground=PANEL,
                   activeforeground=INK, font=FONT, anchor='w').pack(fill='x', padx=px(7), pady=px(8))
    field(advanced, '关键帧间隔 GOP（帧）', ws.delivery_gop_var)
    field(advanced, 'B 帧（0–4）', ws.delivery_bframes_var, key='bframes')
    field(advanced, 'NVENC 调优', ws.delivery_tune_var, ['hq', 'll', 'ull', 'lossless'], key='tune')
    field(advanced, '多重分析', ws.delivery_multipass_var,
          ['自动（随预设）', 'disabled', 'qres', 'fullres'], key='multipass')
    field(advanced, 'GPU 索引（从 0 开始）', ws.delivery_gpuindex_var, key='gpu')
    note(advanced, 'P1 / P2 优先速度。GOP 和 B 帧按此处设置生效；CPU 使用所选速度对应的 x264 预设。')

    def set_enabled(key, enabled):
        widget, state = controls[key]
        widget.configure(state=state if enabled else 'disabled')

    def refresh(*_):
        forced_cpu = ws.export_mode.get().startswith('CPU')
        try:
            selected_codec = 'libx264' if forced_cpu else codec_from_label(app.codec_var.get())
        except ValueError:
            selected_codec = ''
        vendor = encoder_vendor(selected_codec)
        cpu = vendor == 'cpu'
        nvenc = vendor == 'nvidia'
        caps = vars(app).get('caps')
        if caps is not None:
            choices = [label for label in CODECS if codec_from_label(label) in available_codecs(caps)]
            controls['codec'][0].configure(values=choices)
        set_enabled('codec', not forced_cpu)
        for key in ('decode', 'tune', 'multipass', 'gpu'):
            set_enabled(key, nvenc)
        set_enabled('bframes', vendor != 'amd')
        split_capable = bool(nvenc and caps and selected_codec in getattr(caps, 'split_encode', set())
                             and getattr(getattr(caps, 'gpu_profile', None), 'nvenc_engines', 0) > 1)
        set_enabled('split', split_capable)
        can_auto_parallel = nvenc and app.decode_var.get().startswith('NVIDIA')
        if not can_auto_parallel and ws.delivery_auto_parallel_var.get():
            ws.delivery_auto_parallel_var.set(False)
        if vendor == 'amd' and ws.delivery_bframes_var.get() != '0':
            ws.delivery_bframes_var.set('0')
        set_enabled('auto_parallel', can_auto_parallel)
        set_enabled('parallel', not can_auto_parallel or not ws.delivery_auto_parallel_var.get())
        bitrate_selected = bool(ws.bitrate_var.get().strip())
        set_enabled('quality', not bitrate_selected)
        quality_title.set(f'质量 {"CRF" if cpu else "CQ"}（0–51，越低越清晰）')
        video_note.configure(text=(
            '当前使用目标码率；清空目标码率后使用质量数值。' if bitrate_selected else
            '当前使用恒定质量；文件大小随画面复杂度变化。') +
            ('\nCPU 预设按所选速度映射，10-bit 保留。' if cpu else
             '\nH.264 硬件编码使用 8-bit；10-bit 请选择已验证可用的 HEVC / AV1。'))
        audio_codec = AUDIO_FORMATS.get(ws.delivery_audio_format_var.get(), 'aac')
        set_enabled('audio_bitrate', audio_codec == 'aac')
        set_enabled('audio_rate', audio_codec not in {'copy', 'none'})
        suffix = CONTAINERS.get(ws.delivery_container_var.get(), '.mp4')
        message = {'aac': 'AAC 支持 MP4 / MOV / MKV。裸数字码率按 kbps 处理；采样率最高 96 kHz。',
                   'copy': '保留主音频原编码、码率和采样率。仅支持从 0 开始且无需补静音的精确复制；任意起点裁剪请选择 AAC / PCM / FLAC。原编码需与容器兼容。',
                   'none': '仅导出视频画面。',
                   'flac': 'FLAC 为无损音频，需要 MKV 容器。码率随内容自动决定。'}.get(
                       audio_codec, 'PCM 无损音频需要 MOV / MKV 容器，码率由位深和采样率决定。')
        incompatible = (audio_codec.startswith('pcm_') and suffix == '.mp4') or (audio_codec == 'flac' and suffix != '.mkv')
        audio_note.configure(text=message, fg='#ed8e85' if incompatible else MUTED)
        engines = getattr(getattr(caps, 'gpu_profile', None), 'nvenc_engines', 0)
        choices = ['驱动自动选择', '关闭']
        if split_capable and engines > 1:
            choices = [f'强制 {count} 路 NVENC' for count in range(2, min(engines, 4)+1)] + choices
        current = app.split_var.get()
        if current and current not in choices:
            choices.insert(0, current)
        split_control.configure(values=choices)
        split_note.configure(text='分帧适用于支持此功能的 HEVC / AV1；并行任务会增加显存占用。' if split_capable else
                             '当前编码器不使用多 NVENC 分帧。AMD / Intel 默认单任务，手动并行会增加内存和显存占用。')

    for variable in (ws.export_mode, ws.bitrate_var, ws.delivery_audio_format_var,
                     ws.delivery_container_var, app.split_var,ws.delivery_auto_parallel_var,
                     app.codec_var, app.decode_var):
        variable.trace_add('write', refresh)
    refresh()
    ws.delivery_tabs.bind('<<NotebookTabChanged>>', refresh, add='+')

    footer = tk.Frame(delivery_frame, bg=INPUT)
    # Reserve the primary action before allocating the notebook. At high DPI
    # the notebook's requested size can otherwise consume the footer's space.
    footer.pack(fill='x', side='bottom', before=ws.delivery_tabs)
    ws.delivery_progress_frame = footer
    progress = vars(app).get('progress_var')
    if progress is not None:
        ws.delivery_progressbar = ttk.Progressbar(footer, variable=progress, maximum=100,
                                                 style='TProgressbar')
        ws.delivery_progressbar.pack(fill='x', padx=px(11), pady=(px(9), px(3)))
    ws.delivery_export_button = ttk.Button(footer, text='开始导出', command=ws.export,
                                           style='Accent.TButton')
    ws.delivery_export_button.pack(fill='x', padx=px(11), pady=px(9))

