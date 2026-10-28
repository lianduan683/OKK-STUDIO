"""Typed timeline selection, intersecting marquee, and undoable group gestures."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import tkinter as tk

from timeline_track_move import track_move_targets, validate_track_move, apply_track_move
from editor_core import format_signed_time, format_time
from timeline_edits import (delete_source_clips, move_source_clips, source_move_delta,
                            remove_program_boundaries, program_boundaries_for_ranges, finalize_source_edit)


class TimelineSelectionMixin:
    def _init_timeline_selection(self):
        self._timeline_selection = set()
        self._selection_project = self.project
        self._selection_gesture = None
        self._selection_pan_after = None

    def _bind_timeline_selection(self):
        self.timeline.bind('<Delete>', self._delete_timeline_selection)
        self.timeline.bind('<BackSpace>', self._delete_timeline_selection)
        self.timeline.bind('<Control-a>', self._select_all_timeline)
        self.timeline.bind('<Control-A>', self._select_all_timeline)
        self.timeline.bind('<FocusOut>', lambda _e: self._finish_selection_gesture(cancel=True), add='+')
        self.timeline.bind('<Unmap>', lambda _e: self._finish_selection_gesture(cancel=True), add='+')

    def _capture_timeline_range(self, project):
        try:
            start, end, _fps = self._read_range()
            if (project.duration <= 0 or start >= self.project.duration
                    or (end is not None and end > self.project.duration+1/self.project.fps)): return
            project.range_start, project.range_end = start, end
        except ValueError:
            pass

    def _remember_timeline_change(self):
        self._remember()
        self._capture_timeline_range(self.undo_stack[-1])

    def _restore_timeline_range_fields(self):
        self._loading_fields = True
        try:
            self.start_var.set(format_time(self.project.range_start))
            self.end_var.set(format_time(self.project.range_end) if self.project.range_end is not None else '')
        finally:
            self._loading_fields = False

    def _timeline_item_keys(self):
        result = {}
        for name, kind in (('timeline_clip_items', 'clip'), ('timeline_subtitle_items', 'subtitle'),
                           ('timeline_transition_items', 'transition'), ('timeline_program_items', 'program')):
            result.update((item, (kind, identifier)) for item, identifier in getattr(self, name, {}).items())
        result.update((item, ('audio', 'master')) for item in getattr(self, 'timeline_audio_items', ()))
        return result

    def _timeline_hit_key(self, event):
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        mapping = self._timeline_item_keys()
        return next((mapping[item] for item in reversed(self.timeline.find_overlapping(x, y, x, y))
                     if item in mapping), None)

    def _marquee_keys(self, left, top, right, bottom):
        # Geometry comes from the model, so auto-scrolling a selection keeps
        # clips already inside the box even after their Canvas items are culled.
        keys = set()
        scale, origin = self._timeline_scale(), self._timeline_origin()
        def tx(value): return self._timeline_left+(value-origin)*scale
        def touches(x1, y1, x2, y2):
            return x2 >= left and x1 <= right and y2 >= top and y1 <= bottom
        for clip in self.project.clips:
            bounds = self._timeline_track_bounds.get(clip.camera)
            if bounds and touches(tx(clip.start), bounds[0]+self._px(1),
                                  tx(clip.end), bounds[1]-self._px(1)):
                keys.add(('clip', clip.id))
        program_top, program_bottom = self._timeline_program_bounds
        if bottom >= program_top and top <= program_bottom:
            cached = vars(self).get('_timeline_span_cache')
            if cached:
                for span in cached[1]:
                    if touches(tx(span.start), program_top+self._px(4),
                               tx(span.end), program_bottom-self._px(3)):
                        keys.add(('program', (span.start, span.end)))
            for item in self.project.transitions:
                x1, x2 = tx(item.time-item.duration/2), tx(item.time+item.duration/2)
                if x2-x1 < self._px(10): x1, x2 = tx(item.time)-self._px(5), tx(item.time)+self._px(5)
                if touches(x1, program_top+self._px(2), x2, program_bottom-self._px(2)):
                    keys.add(('transition', item.time))
        subtitle = getattr(self, '_timeline_subtitle_bounds', None)
        if subtitle:
            offset, duration = self.project.subtitles.offset, self._timeline_view_duration()
            for cue in self.project.subtitles.cues:
                start, end = max(0., cue.start+offset), min(duration, cue.end+offset)
                if start < end and touches(tx(start), subtitle[0]+self._px(2),
                                           tx(end), subtitle[1]-self._px(2)):
                    keys.add(('subtitle', cue.id))
        audio = getattr(self, '_timeline_audio_bounds', None)
        if audio and self.project.master_audio:
            end = min(self._timeline_view_duration(), self.project.master_duration or self._timeline_view_duration())
            if end > 0 and touches(tx(0.), audio[0]+self._px(3), tx(end), audio[1]-self._px(3)):
                keys.add(('audio', 'master'))
        return keys

    def _set_timeline_selection(self, keys, *, redraw=True):
        keys = set(keys)
        changed = keys != self._timeline_selection or self._selection_project is not self.project
        self._selection_project = self.project
        self._timeline_selection = keys
        ranges = [key[1] for key in keys if key[0] == 'program']
        self.selected_program_range = ((min(r[0] for r in ranges), max(r[1] for r in ranges)) if ranges else None)
        if changed:
            ids = [clip.id for clip in self.project.clips if ('clip', clip.id) in keys and self.media_tree.exists(clip.id)]
            self.media_tree.selection_set(ids)
        if redraw: self._draw_timeline_selection()

    def _draw_timeline_selection(self):
        canvas = self.timeline
        canvas.delete('timeline-selected')
        if self._selection_project is not self.project and self._selection_gesture is None:
            self._timeline_selection.clear()
            self._selection_project = self.project
        for item, key in self._timeline_item_keys().items():
            if key not in self._timeline_selection or canvas.type(item) != 'rectangle': continue
            bounds = canvas.coords(item)
            canvas.create_rectangle(*bounds, outline='#7acbff', width=max(2, self._px(2)),
                                    tags=('timeline-selected',))
        canvas.delete('timeline-marquee')
        drag = self._selection_gesture
        if drag and drag['kind'] == 'marquee' and drag['moved']:
            canvas.create_rectangle(drag['x'], drag['y'], drag['last_x'], drag['last_y'],
                outline='#7acbff', fill='#58a6d6', stipple='gray75', width=max(1, self._px(1)),
                tags=('timeline-marquee',))
        canvas.tag_raise('head')

    def _select_all_timeline(self, _event=None):
        if not self._available(): return 'break'
        # The programme overview duplicates source clips. Select editable media
        # and subtitles, keeping the continuous master audio explicitly chosen.
        keys = {('clip', clip.id) for clip in self.project.clips}
        keys.update(('subtitle', cue.id) for cue in self.project.subtitles.cues)
        self._set_timeline_selection(keys)
        self.status.set(f'已选中 {len(keys)} 个素材/字幕；可成组拖动或 Delete 删除。')
        return 'break'

    def _selection_press(self, event):
        y = self.timeline.canvasy(event.y)
        if y < self._timeline_program_bounds[0]: return False
        if self._stack_track_at(event) is not None: return False
        self.timeline.focus_set()
        key = self._timeline_hit_key(event)
        additive = bool(getattr(event, 'state', 0) & (0x1 | 0x4))
        if key and key[0] in {'program', 'transition'} and not additive:
            self._set_timeline_selection({key}, redraw=False)
            return False  # Retain programme cut/transition inspector gestures.
        self._stop_transport_hold()
        self.pause()
        if key:
            selected = set(self._timeline_selection)
            if additive:
                if key in selected: selected.remove(key)
                else: selected.add(key)
            elif key not in selected:
                selected = {key}
            self._set_timeline_selection(selected)
            if key not in selected: return True
            if key[0] == 'audio':
                self.status.set('已选中连续主音轨；右键或 Delete 取消绑定，Ctrl+Z 恢复。')
                return True
            if key[0] in {'program', 'transition'}: return True
            clips = {clip.id: clip.start for clip in self.project.clips if ('clip', clip.id) in selected}
            cues = {cue.id: (cue.start, cue.end) for cue in self.project.subtitles.cues
                    if ('subtitle', cue.id) in selected}
            self._capture_timeline_geometry()
            self.seek(self._timeline_origin()+(self.timeline.canvasx(event.x)-self._timeline_left)/self._timeline_scale())
            self._selection_gesture = dict(kind='move', project=self.project,
                x=self.timeline.canvasx(event.x), y=y, moved=False, changed=False, delta=0.,
                clips=clips, cues=cues,
                clip_cameras={clip.id: clip.camera for clip in self.project.clips if clip.id in clips},
                anchor_camera=next((clip.camera for clip in self.project.clips if key == ('clip', clip.id)), None),
                track_order=list(self._timeline_camera_order()), track_bounds=dict(self._timeline_track_bounds),
                track_changed=False, stack_enabled=self.project.video_stack_enabled,
                empty_tracks=list(self.project.timeline_empty_tracks),
                stack_order=list(self.project.video_stack_order), stabilizations=[(item, item.start, item.end)
                    for item in self.project.stabilizations if item.source_id in clips],
                undo=list(self.undo_stack), redo=list(self.redo_stack), dirty=self.dirty,
                pointer=(event.x, event.y), selection=set(selected))
            self.timeline.configure(cursor='fleur')
            self.status.set(f'选中 {len(selected)} 项 · 拖动一起移动 · Ctrl/Shift 点选增减 · Esc 取消')
            return True
        # Empty-space dragging starts a marquee; a partial rectangle overlap
        # counts, including clips much longer than the visible viewport.
        if not additive: self._set_timeline_selection(set())
        self._selection_gesture = dict(kind='marquee', project=self.project,
            x=self.timeline.canvasx(event.x), y=y, last_x=self.timeline.canvasx(event.x), last_y=y,
            moved=False, base=set(self._timeline_selection) if additive else set(), pointer=(event.x, event.y))
        return True

    def _selection_motion(self, event):
        drag = self._selection_gesture
        if not drag: return False
        if drag['project'] is not self.project:
            self._finish_selection_gesture(cancel=True)
            return True
        drag['pointer'] = (event.x, event.y)
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        if not drag['moved'] and max(abs(x-drag['x']), abs(y-drag['y'])) < self._px(4): return True
        drag['moved'] = True
        if drag['kind'] == 'marquee':
            drag['last_x'], drag['last_y'] = x, y
            left, right = sorted((drag['x'], x)); top, bottom = sorted((drag['y'], y))
            keys = set(drag['base'])
            keys.update(self._marquee_keys(left, top, right, bottom))
            self._set_timeline_selection(keys)
            self.status.set(f'框选 {len(keys)} 项 · 碰到片段即可选中 · Delete 删除')
        else:
            fps = self.project.fps
            delta = math.floor((x-drag['x'])/self._timeline_scale()*fps+.5)/fps
            if drag['cues']:
                minimum = math.ceil(-min(start for start, _ in drag['cues'].values())*fps-1e-8)/fps
                delta = max(delta, minimum)
            targets = track_move_targets(self.project, drag, y)
            current_cameras = {clip.id: clip.camera for clip in self.project.clips if clip.id in drag['clips']}
            if abs(delta-drag['delta']) < .25/fps and current_cameras == targets:
                self._schedule_selection_pan()
                return True
            increment = delta-drag['delta']
            try:
                validate_track_move(self.project, drag, targets, delta)
                if drag['clips']: source_move_delta(self.project, drag['clips'], increment, target_cameras=targets)
            except ValueError as error:
                self.status.set(str(error)); return True
            if not drag['changed']:
                self._remember_timeline_change(); drag['baseline'] = self.undo_stack[-1]; drag['changed'] = True
            if drag['clips']: move_source_clips(self.project, drag['clips'], increment, target_cameras=targets)
            apply_track_move(self.project, drag, targets)
            drag['track_changed'] = targets != drag['clip_cameras']
            for cue in self.project.subtitles.cues:
                if cue.id in drag['cues']:
                    start, end = drag['cues'][cue.id]; cue.start, cue.end = start+delta, end+delta
            for item, start, end in drag['stabilizations']:
                item.start, item.end = max(0., start+delta), end+delta
            drag['delta'] = delta
            self._queue_timeline_redraw()
            self.status.set(f'成组移动 {format_signed_time(delta)} · Esc 取消 · 松开提交')
        self._schedule_selection_pan()
        return True

    def _schedule_selection_pan(self):
        drag = self._selection_gesture
        if not drag or self._selection_pan_after is not None: return
        x, y = drag['pointer']; margin = self._px(22)
        if x < margin or x > self.timeline.winfo_width()-margin or y < margin or y > self.timeline.winfo_height()-margin:
            self._selection_pan_after = self.after(30, self._selection_auto_pan)

    def _selection_auto_pan(self):
        self._selection_pan_after = None
        drag = self._selection_gesture
        if not drag or not self.alive: return
        x, y = drag['pointer']; margin = self._px(22)
        dx = -1 if x < margin else 1 if x > self.timeline.winfo_width()-margin else 0
        dy = -1 if y < margin else 1 if y > self.timeline.winfo_height()-margin else 0
        if dx: self.timeline.xview_scroll(dx, 'units')
        if dy: self.timeline.yview_scroll(dy, 'units')
        if dx or dy:
            self.draw_timeline()
            from types import SimpleNamespace
            self._selection_motion(SimpleNamespace(x=x, y=y))

    def _finish_selection_gesture(self, *, cancel=False):
        drag = self._selection_gesture
        if drag is None: return False
        self._selection_gesture = None
        if self._selection_pan_after is not None:
            self.after_cancel(self._selection_pan_after); self._selection_pan_after = None
        self._cancel_timeline_redraw()
        self._timeline_drag_geometry = None
        self.timeline.configure(cursor='')
        self.timeline.delete('timeline-marquee')
        if drag['kind'] == 'move' and drag['changed']:
            reverted = cancel or (abs(drag['delta']) < .25/self.project.fps and not drag['track_changed'])
            error = None
            if not reverted:
                try:
                    self._capture_timeline_range(self.project)
                    finalize_source_edit(self.project)
                except ValueError as exc: reverted, error = True, str(exc)
            if reverted:
                self.project = drag['baseline']
                self.undo_stack, self.redo_stack, self.dirty = drag['undo'], drag['redo'], drag['dirty']
                self.project_label.configure(text=(Path(self.project_path).name if self.project_path else '未保存工程')
                    + (' *' if self.dirty else ''))
            self._selection_project = self.project
            self._restore_timeline_range_fields()
            self._refresh()
            self._set_timeline_selection(drag['selection'])
            self.seek(self.current)
            self.status.set(error or ('已取消移动。' if reverted else '已移动所选素材/字幕；Ctrl+Z 撤销。'))
        else:
            if cancel and drag['kind'] == 'marquee': self._set_timeline_selection(drag['base'])
            self._draw_timeline_selection()
        return True

    def _delete_timeline_selection(self, _event=None):
        if not self._available(): return 'break'
        if self._selection_gesture: self._finish_selection_gesture(cancel=True)
        keys = set(self._timeline_selection)
        if not keys: return 'break'
        candidate = copy.deepcopy(self.project)
        self._capture_timeline_range(candidate)
        count = 0
        try:
            ranges = [key[1] for key in keys if key[0] == 'program']
            points = program_boundaries_for_ranges(self.project, ranges)
            if points: count += remove_program_boundaries(candidate, points)
            count += delete_source_clips(candidate, [key[1] for key in keys if key[0] == 'clip'])
            ids = {key[1] for key in keys if key[0] == 'subtitle'}
            before = len(candidate.subtitles.cues)
            candidate.subtitles.cues = [cue for cue in candidate.subtitles.cues if cue.id not in ids]
            count += before-len(candidate.subtitles.cues)
            times = {key[1] for key in keys if key[0] == 'transition'}
            before = len(candidate.transitions)
            candidate.transitions = [item for item in candidate.transitions if item.time not in times]
            count += before-len(candidate.transitions)
            if ('audio', 'master') in keys and candidate.master_audio:
                candidate.master_audio, candidate.master_duration = '', 0.
                count += 1
            if count: finalize_source_edit(candidate)
        except ValueError as error:
            self.status.set(str(error)); return 'break'
        if count:
            self.pause(); self._remember_timeline_change(); self.project = candidate
            self.media_tree.selection_set(())
            self._set_timeline_selection(set(), redraw=False)
            self._restore_timeline_range_fields()
            self._refresh(); self.seek(self.current)
            self.status.set(f'已删除 {count} 项时间线内容；原文件保留，Ctrl+Z 撤销。')
        return 'break'

    def _rekey_timeline_boundary(self, drag, reverted):
        if not drag: return
        old, new = drag['original'], drag['original'] if reverted else drag['current']
        fps = self.project.fps
        def moved(value):
            return new if math.floor(value*fps+.5) == math.floor(old*fps+.5) else value
        keys = set()
        for kind, identifier in self._timeline_selection:
            if kind == 'program': identifier = tuple(moved(point) for point in identifier)
            elif kind == 'transition': identifier = moved(identifier)
            keys.add((kind, identifier))
        self._selection_project = self.project
        self._set_timeline_selection(keys, redraw=False)

    def _selection_context(self, event):
        key = self._timeline_hit_key(event)
        if key and key[0] == 'program': return False
        if key is None and not self._timeline_selection:
            top, bottom = self._timeline_program_bounds
            if top <= self.timeline.canvasy(event.y) <= bottom: return False
            return self._show_timeline_insert_menu(event)
        self.timeline.focus_set(); self.pause()
        if key is not None and key not in self._timeline_selection: self._set_timeline_selection({key})
        menu = tk.Menu(self, tearoff=False, bg='#292b2e', fg='#dedfe3', activebackground='#3c5263')
        source_count = sum(key[0] == 'clip' for key in self._timeline_selection)
        menu.add_command(label='合并选中素材（首尾接齐）', command=self._merge_timeline_selection,
                         state='normal' if source_count >= 2 else 'disabled')
        label = '取消绑定主音轨' if self._timeline_selection == {('audio', 'master')} else '删除所选内容'
        menu.add_command(label=label, accelerator='Delete', command=self._delete_timeline_selection)
        menu.add_command(label='全选素材与字幕', accelerator='Ctrl+A', command=self._select_all_timeline)
        menu.add_separator()
        menu.add_command(label='撤销', accelerator='Ctrl+Z', command=self.undo,
                         state='normal' if self.undo_stack else 'disabled')
        self._append_timeline_insert_menu(menu, event)
        self.timeline_context_menu = menu
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return True
