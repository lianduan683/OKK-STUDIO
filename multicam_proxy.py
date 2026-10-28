"""Non-blocking, serial 1080p proxy preparation for multicamera editing."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from exporter_core import ExportCancelled


def _identity(path):
    return os.path.normcase(os.path.abspath(path))


def _stamp(path):
    try:
        info = Path(path).stat()
        return info.st_size, info.st_mtime_ns
    except OSError:
        return None


class MulticamProxyMixin:
    def _init_quick_proxy(self):
        self._quick_proxy_token = 0
        self._quick_proxy_cancel = threading.Event()
        self._quick_proxy_running = False
        self._quick_proxy_thread = None
        self.quick_proxy_status = tk.StringVar(self, value='')
        self._proxy_link_token = 0
        self._proxy_link_cancel = threading.Event()
        self._proxy_link_running = False

    def _build_quick_proxy_controls(self, parent):
        self.quick_proxy_button = ttk.Button(parent, text='一键 1080p 代理',
            style='Compact.TButton', command=self.start_quick_proxy)
        self.quick_proxy_button.pack(side='left', padx=(self._px(2), self._px(4)))
        self.proxy_link_button = ttk.Menubutton(parent, text='链接已有代理', style='Compact.TMenubutton')
        menu = tk.Menu(self.proxy_link_button, tearoff=False)
        menu.add_command(label='给所选素材链接代理文件…', command=self.link_existing_proxy)
        menu.add_command(label='从文件夹批量匹配…', command=self.link_proxy_folder)
        menu.add_separator()
        menu.add_command(label='取消链接校验', command=self.cancel_proxy_link)
        self.proxy_link_button.configure(menu=menu)
        self.proxy_link_button.pack(side='left', padx=self._px(2))
        tk.Checkbutton(parent, text='代理预览', variable=self.proxy_var,
            command=self._configure_player, bg='#292b2e', fg='#73aa9c', selectcolor='#18191b',
            activebackground='#292b2e', activeforeground='#dedfe3', borderwidth=0,
            font=('Microsoft YaHei UI', 9)).pack(side='left')
        self.quick_proxy_cancel_button = ttk.Button(parent, text='取消', width=4,
            style='Compact.TButton', command=self.cancel_quick_proxy)
        self.quick_proxy_label = tk.Label(parent, textvariable=self.quick_proxy_status,
            bg='#292b2e', fg='#969ba3', font=('Microsoft YaHei UI', 8), anchor='w')
        self.quick_proxy_label.pack(side='left', padx=self._px(4))

    def _set_quick_proxy_running(self, running):
        self._quick_proxy_running = running
        button = vars(self).get('quick_proxy_button')
        cancel = vars(self).get('quick_proxy_cancel_button')
        if button is not None:
            button.configure(state='disabled' if running else 'normal')
        if cancel is not None:
            if running:
                cancel.configure(state='normal')
                cancel.pack(side='right', padx=self._px(2))
            else:
                cancel.pack_forget()

    def start_quick_proxy(self):
        if self._quick_proxy_running:
            return False
        if not self.project.clips:
            self.status.set('请先导入机位素材，再生成 1080p 代理。')
            return False
        if not self.app.caps.ffmpeg_path or not self.app.caps.ffprobe_path:
            self.status.set('FFmpeg 尚未就绪，请稍后生成代理。')
            return False
        # Current-camera material is queued first; repeated files encode once.
        camera = self.project.camera_at(self.current)
        clips = sorted(self.project.clips, key=lambda c: (
            c.camera != camera, not (c.start <= self.current < c.start+c.duration), c.camera, c.start))
        sources = {}
        for clip in clips:
            key = _identity(clip.path)
            if key not in sources:
                sources[key] = {'source': clip.path, 'stamp': _stamp(clip.path), 'clips': []}
            sources[key]['clips'].append((clip.id, clip.proxy_path))
        requests = list(sources.values())
        self._quick_proxy_token += 1
        token = self._quick_proxy_token
        cancel = threading.Event()
        self._quick_proxy_cancel = cancel
        self._set_quick_proxy_running(True)
        self.proxy_var.set(True)
        self._configure_player()
        self.quick_proxy_status.set(f'0/{len(requests)}')
        self.status.set(f'后台逐个准备 {len(requests)} 份 1080p 代理，完成后自动接入；可以继续剪辑。')
        ffmpeg, ffprobe, caps = self.app.caps.ffmpeg_path, self.app.caps.ffprobe_path, self.app.caps
        jobs = self.jobs

        def worker():
            successful, failures = 0, []
            try:
                from quick_proxy import prepare_1080p_proxy
                for index, request in enumerate(requests):
                    if cancel.is_set():
                        break
                    # At most one transcode competes with the visible preview decoders.
                    last_report = [0.]
                    def progress(percent, phase):
                        now = time.monotonic()
                        if now-last_report[0] >= .2 or percent >= 100:
                            last_report[0] = now
                            jobs.put(('quick_proxy_progress', (token, index, len(requests), float(percent))))
                    try:
                        path = prepare_1080p_proxy(request['source'], ffmpeg, ffprobe, caps,
                                                  cancel_event=cancel, progress=progress)
                        if cancel.is_set():
                            break
                        successful += 1
                        jobs.put(('quick_proxy_ready', (token, request, str(path))))
                    except ExportCancelled:
                        break
                    except Exception as exc:
                        failures.append((request['source'], str(exc)))
                        jobs.put(('quick_proxy_error', (token, request['source'], str(exc))))
            except Exception as exc:
                failures.append(('', str(exc)))
                jobs.put(('quick_proxy_error', (token, '', str(exc))))
            finally:
                jobs.put(('quick_proxy_done', (token, successful, len(requests), failures, cancel.is_set())))

        # Keep Python alive long enough to reap a cancelled FFmpeg on exit.
        self._quick_proxy_thread = threading.Thread(target=worker, name='multicam-1080p-proxies', daemon=False)
        self._quick_proxy_thread.start()
        return True

    def cancel_quick_proxy(self):
        if self._quick_proxy_running:
            self._quick_proxy_cancel.set()
            self.quick_proxy_status.set('正在取消…')
            self.quick_proxy_cancel_button.configure(state='disabled')
            self.status.set('正在停止代理任务；已完成并挂载的代理会保留。')

    def _reset_quick_proxy(self):
        self._quick_proxy_cancel.set()
        self._quick_proxy_token += 1
        self._set_quick_proxy_running(False)
        self.quick_proxy_status.set('')
        self._reset_proxy_link()

    def _close_quick_proxy(self):
        self._quick_proxy_cancel.set()
        self._quick_proxy_token += 1
        self._reset_proxy_link()

    def _reset_proxy_link(self):
        event = vars(self).get('_proxy_link_cancel')
        if event is not None:
            event.set()
        self._proxy_link_token = vars(self).get('_proxy_link_token', 0) + 1
        self._proxy_link_running = False

    def cancel_proxy_link(self):
        event = vars(self).get('_proxy_link_cancel')
        if event is not None and vars(self).get('_proxy_link_running', False):
            event.set()
            self.status.set('正在停止代理链接校验；当前代理保持不变。')

    def link_existing_proxy(self):
        if vars(self).get('_proxy_link_running', False):
            self.status.set('正在检查已有代理，请等待完成或在链接菜单中取消。')
            return False
        clip = self._selected_clip()
        if clip is None:
            self.status.set('请先在素材列表选择一份原片，再链接它的完整代理文件。')
            return False
        path = filedialog.askopenfilename(parent=self, title='链接同一完整原片的代理（未裁切、未变速）',
            filetypes=[('视频代理', '*.mp4 *.mov *.mkv *.mxf *.avi *.mts *.m2ts *.webm'), ('所有文件', '*.*')])
        return self._start_proxy_link([(clip.path, path)]) if path else False

    def link_proxy_folder(self):
        if vars(self).get('_proxy_link_running', False):
            self.status.set('正在检查已有代理，请等待完成或在链接菜单中取消。')
            return False
        if not self.project.clips:
            self.status.set('请先导入机位素材，再链接已有代理。')
            return False
        folder = filedialog.askdirectory(parent=self, title='选择代理文件夹（含子文件夹，按原片名匹配）')
        return self._start_proxy_link(folder=folder) if folder else False

    def _start_proxy_link(self, pairs=None, *, folder=None):
        if vars(self).get('_proxy_link_running', False):
            return False
        ffprobe = self.app.caps.ffprobe_path
        if not ffprobe:
            self.status.set('ffprobe 尚未就绪，暂时无法校验已有代理。')
            return False
        requests = {}
        for clip in self.project.clips:
            key = _identity(clip.path)
            if key not in requests:
                requests[key] = {'source': clip.path, 'stamp': _stamp(clip.path), 'clips': []}
            requests[key]['clips'].append((clip.id, clip.proxy_path))
        self._proxy_link_token = vars(self).get('_proxy_link_token', 0) + 1
        token = self._proxy_link_token
        cancel = self._proxy_link_cancel = threading.Event()
        self._proxy_link_running = True
        # Respect an explicit later switch-off while validation runs.
        self.proxy_var.set(True)
        if self.player:
            self.player.submit('proxy_update', bindings=[], use_proxies=True)
        self.status.set('后台检查已有代理的视频与完整时长，可以继续剪辑…')
        jobs = self.jobs

        def worker():
            ready, issues = [], []
            try:
                from external_proxy_link import match_proxy_folder, validate_existing_proxy
                selected = pairs
                if folder:
                    selected, issues = match_proxy_folder(
                        [item['source'] for item in requests.values()], folder, cancel)
                for source, path in selected or ():
                    if cancel.is_set():
                        raise ExportCancelled('已取消链接代理。')
                    request = requests.get(_identity(source))
                    if request is None:
                        continue
                    try:
                        before = _stamp(path)
                        validate_existing_proxy(ffprobe, source, path, cancel)
                        if before is None or _stamp(path) != before or _stamp(source) != request['stamp']:
                            raise ValueError('文件在校验期间发生变化，请重新链接。')
                        ready.append((request, path, before))
                    except ExportCancelled:
                        raise
                    except Exception as error:
                        issues.append((source, str(error)))
            except ExportCancelled:
                pass
            except Exception as error:
                issues.append((folder or '', str(error)))
            finally:
                jobs.put(('quick_proxy_link_done', (token, ready, issues, cancel.is_set())))

        self._proxy_link_thread = threading.Thread(target=worker, name='link-existing-proxies', daemon=False)
        self._proxy_link_thread.start()
        return True

    def _finish_proxy_link(self, payload):
        token, ready, issues, cancelled = payload
        if token != vars(self).get('_proxy_link_token', 0):
            return True
        self._proxy_link_running = False
        if cancelled or self._proxy_link_cancel.is_set():
            self.status.set('已取消代理链接校验，当前代理保持不变。')
            return True
        bindings, changes, linked = [], [], []
        for request, path, stamp in ready:
            if _stamp(path) != stamp or _stamp(request['source']) != request['stamp']:
                issues.append((request['source'], '文件已变化，未挂载。'))
                continue
            initial = dict(request['clips'])
            for clip in self.project.clips:
                if (clip.id in initial and _identity(clip.path) == _identity(request['source'])
                        and clip.proxy_path in (initial[clip.id], path)):
                    changes.append((clip, path))
                    bindings.append({'id': clip.id, 'source': clip.path, 'proxy': path})
            if any(item['source'] == request['source'] for item in bindings):
                linked.append((request['source'], path))
        if changes:
            self._remember()
            for clip, path in changes:
                clip.proxy_path = path
            from proxy_cache import register_proxy
            for source, path in linked:
                register_proxy(source, path)
            self._mark_dirty()
            if self.player:
                self.player.submit('proxy_update', bindings=bindings, use_proxies=self.proxy_var.get())
        for source, error in issues:
            self.app._log(f'链接代理：{source}\n{error}')
        self.status.set(f'已链接 {len(linked)} 份已有代理；导出使用原片。' +
            (f' {len(issues)} 份未匹配或校验失败，详见任务日志，可逐个手动链接。' if issues else ''))
        return True

    def _handle_quick_proxy_job(self, kind, payload):
        if not kind.startswith('quick_proxy_'):
            return False
        if kind == 'quick_proxy_link_done':
            return self._finish_proxy_link(payload)
        if payload[0] != self._quick_proxy_token:
            return True
        if kind == 'quick_proxy_progress':
            if not self._quick_proxy_cancel.is_set():
                _, index, total, percent = payload
                self.quick_proxy_status.set(f'{index}/{total} · {percent:.0f}%')
        elif kind == 'quick_proxy_ready':
            if self._quick_proxy_cancel.is_set():
                return True
            _, request, path = payload
            if not Path(path).is_file() or _stamp(request['source']) != request['stamp']:
                return True
            initial = dict(request['clips'])
            bindings = []
            for clip in self.project.clips:
                if (clip.id in initial and _identity(clip.path) == _identity(request['source'])
                        and clip.proxy_path in (initial[clip.id], path)):
                    clip.proxy_path = path
                    bindings.append({'id': clip.id, 'source': clip.path, 'proxy': path})
            if bindings:
                self._mark_dirty()
                if self.player:
                    self.player.submit('proxy_update', bindings=bindings, use_proxies=self.proxy_var.get())
        elif kind == 'quick_proxy_error':
            _, source, error = payload
            self.app._log(f'1080p 代理失败：{source}\n{error}')
        elif kind == 'quick_proxy_done':
            _, completed, total, failures, cancelled = payload
            self._set_quick_proxy_running(False)
            self.quick_proxy_status.set(('已取消 ' if cancelled else '1080p ') + f'{completed}/{total}')
            self.status.set((f'已取消代理任务，保留 {completed} 份已完成代理。' if cancelled else
                f'1080p 代理完成 {completed}/{total}；预览使用代理，导出使用原片。') +
                (f' {len(failures)} 份失败，详见任务日志，可再次点击重试。' if failures else ''))
        return True
