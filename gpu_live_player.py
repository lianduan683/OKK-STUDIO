"""Pure source/grade plans for two-texture, master-clock GPU transitions."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from creative_core import grade_is_identity, validate_transitions
from creative_render import _continuous_clip
from program_edits import grade_at
from subtitle_core import subtitle_at


def _frame(time, fps): return math.floor(float(time)*fps+.5)


@dataclass(frozen=True)
class LiveSide:
    camera: int
    source_id: str
    path: str
    asset_key: tuple
    source_epoch: float
    grade_anchor: float

    def native_time(self, programme_time): return float(programme_time)-self.source_epoch


@dataclass(frozen=True)
class LiveTransitionPlan:
    kind: str
    start_frame: int
    cut_frame: int
    end_frame: int
    fps: float
    outgoing: LiveSide
    incoming: LiveSide

    @property
    def start(self): return self.start_frame/self.fps
    @property
    def end(self): return self.end_frame/self.fps
    @property
    def token(self):
        return (self.kind, self.start_frame, self.cut_frame, self.end_frame,
                self.outgoing.asset_key, self.incoming.asset_key,
                self.outgoing.source_epoch, self.incoming.source_epoch)

    def contains(self, time): return self.start_frame <= _frame(time, self.fps) < self.end_frame
    def progress(self, time):
        return max(0., min(1., (_frame(time, self.fps)-self.start_frame)/(self.end_frame-self.start_frame)))


def compile_live_transitions(project, *, use_proxies=True):
    """Resolve the same rounded frame windows and shot handles as export."""
    validate_transitions(project.transitions, project)
    fps = float(project.fps)
    result = []
    previous_end = -1
    for effect in sorted(project.transitions, key=lambda item: item.time):
        if getattr(effect, 'mask_path', ''):
            raise ValueError('资源遮罩转场使用后台准备的精确预览。')
        count = max(1, _frame(effect.duration, fps))
        cut = _frame(effect.time, fps)
        first, last = cut-count//2, cut-count//2+count
        if first < previous_end or first < 0 or last > _frame(project.duration, fps):
            raise ValueError('GPU 转场区间在工程帧率下重叠或超出工程。')
        previous_end = last
        boundaries = [item.time for item in project.cuts]+list(project.edit_points)
        if any(first < _frame(time, fps) < last and _frame(time, fps) != cut for time in boundaries):
            raise ValueError('GPU 转场区间包含其他切点，请缩短转场。')
        def side(anchor):
            camera = project.camera_at(anchor)
            clip = _continuous_clip(project, camera, first, last)
            found = project.media_at(camera, anchor, use_proxies=use_proxies)
            if found is None: raise ValueError('GPU 转场机位在所选范围没有视频画面。')
            path, native, key = found
            epoch = anchor-float(native)
            if first/fps-epoch < -1e-6:
                raise ValueError('GPU 转场的防抖或代理素材缺少入口余量，请重新准备或缩短转场。')
            if key and key[0] == 'stabilized':
                treated = project.stabilization_at(camera, anchor, clip.id)
                if treated is not None and last/fps > clip.start+treated.source_end+1e-6:
                    raise ValueError('GPU 转场的防抖素材缺少出口余量，请重新准备或缩短转场。')
            return LiveSide(camera, clip.id, str(path), tuple(key), epoch, anchor)
        result.append(LiveTransitionPlan(effect.kind, first, cut, last, fps,
                                        side((cut-1)/fps), side(cut/fps)))
    return tuple(result)


def side_grades(project, side, draft=None):
    camera_grade = project.camera_grades.get(side.camera, project.camera_grades.get(str(side.camera)))
    local_grade = grade_at(project, side.grade_anchor)
    global_grade = project.global_grade
    if draft:
        target, grade = draft
        if target == ('camera', side.camera): camera_grade = grade
        elif target == ('global',): global_grade = grade
        elif target[0] == 'selection' and target[1] <= side.grade_anchor < target[2]: local_grade = grade
    return camera_grade, local_grade, global_grade


def missing_gpu_features(project, capabilities):
    """A missing native feature must select the explicit old effect path."""
    missing = []
    grades = [project.global_grade, *project.camera_grades.values(), *(item.grade for item in project.clip_grades)]
    if any(grade and grade.enabled and grade.lut_path for grade in grades) and not capabilities.get('lut'):
        missing.append('LUT')
    track = project.subtitles
    if track.enabled and track.cues and not capabilities.get('subtitles'):
        missing.append('字幕')
    if not capabilities.get('grade_uniforms') and any(grade and grade.enabled and not grade_is_identity(grade) for grade in grades):
        missing.append('调色')
    supported = set(capabilities.get('transitions', ()))
    if any(getattr(effect, 'mask_path', '') for effect in project.transitions):
        missing.append('资源遮罩转场')
    missing.extend(effect.kind for effect in project.transitions if effect.kind not in supported)
    return tuple(dict.fromkeys(missing))


def subtitle_payload(project, time):
    from dataclasses import asdict
    cues = subtitle_at(project.subtitles, time)
    if not cues: return None
    return {'cues': [{'id': cue.id, 'text': cue.text} for cue in cues],
            'style': asdict(project.subtitles.style), 'programme_time': float(time)}


class GpuLivePlayerMixin:
    """Worker-owned optional texture path; thumbnails/audio retain old owners."""
    def _init_gpu_live(self):
        self._gpu_live_enabled = False
        self._gpu_session = None
        self._gpu_plans = ()
        self._gpu_context = None
        self._gpu_pause_states = {}
        self._gpu_last_phase = None
        self._gpu_failure_reason = ''
        self._gpu_software_fallback = False

    def _gpu_pause(self, slot, paused, *, force=False):
        key = id(slot.controller)
        if force or self._gpu_pause_states.get(key) != paused:
            if paused: self._set_picture_speed(slot, 1.)
            slot.controller.command(['set_property', 'pause', bool(paused)])
            self._gpu_pause_states[key] = bool(paused)

    def _gpu_stop(self):
        # Keep the owner reachable until native shutdown has succeeded.  A
        # failover replaces texture slots, so those slots cannot own retries.
        session = self._gpu_session
        self._gpu_context = None
        self._gpu_plans = ()
        self._gpu_live_enabled = False
        self._gpu_pause_states.clear()
        if session is not None:
            for slot in (self._program, self._program_standby):
                if slot is not None and getattr(slot.controller, 'release_only', False):
                    try:slot.controller.stop()
                    except Exception:
                        # Closing the complete session below may recover a
                        # failed per-texture release; always attempt both.
                        pass
            session.close()
            self._gpu_session = None

    def _gpu_failover(self, camera, error):
        """Keep the master clock running, and stop retrying a failed backend."""
        from multicam_player import _Slot
        if not self._gpu_live_enabled or self._closing.is_set(): return
        label = f'机位 {camera}' if camera else '主画面'
        reason = f'{label} GPU 实时预览失败，已切换兼容预览（允许软件解码）：{error}'
        self._gpu_failure_reason = reason
        self._gpu_software_fallback = True
        self._position = self._clock() if self._playing else self._position
        self._anchor = time.monotonic()
        try:
            self._gpu_stop()
        except Exception as closing_error:
            self._error('关闭 GPU 预览资源失败', closing_error)
        self._program = _Slot(self.program_window)
        self._program_standby = _Slot(self.program_standby_window) if self.program_standby_window is not None else None
        self._awaiting_program_swap = None
        self._position = self._clock() if self._playing else self._position
        self._anchor = time.monotonic()
        self._error('GPU 实时预览已切换兼容模式', RuntimeError(reason))
        self._emit('gpu_live_state', {'enabled': False, 'reason': reason})

    def _new_program_controller(self, window):
        from multicam_player import MpvController
        if self._gpu_software_fallback:
            return MpvController(self.executable, window, allow_software_decode=True)
        return MpvController(self.executable, window)

    def _configure_gpu_live(self, requested):
        from gpu_live_controller import GpuLiveSession, TextureController
        from multicam_player import _Slot
        reason = ''
        if not requested:
            # Only an explicit off -> on clears a runtime decoder failure.
            self._gpu_failure_reason = ''
        elif self._gpu_failure_reason:
            self._emit('gpu_live_state', {'enabled': False, 'reason': self._gpu_failure_reason})
            return
        if requested:
            try:
                if self._gpu_session is None:
                    self._gpu_session = GpuLiveSession(self.gpu_output_window,
                        runtime=self._gpu_live_runtime, session_factory=self._gpu_session_factory)
                    self._gpu_session.start()
                missing = missing_gpu_features(self._project, self._gpu_session.capabilities)
                if (self._grade_draft and self._grade_draft[1] is not None
                        and self._grade_draft[1].enabled and self._grade_draft[1].lut_path
                        and not self._gpu_session.capabilities.get('lut')):
                    missing = (*missing, 'LUT 调色预览')
                if missing: raise RuntimeError('原生 GPU 预览尚不支持：'+ '、'.join(missing)+'；使用原效果预览路径。')
                plans = compile_live_transitions(self._project, use_proxies=self._use_proxies)
                if not self._gpu_live_enabled:
                    for slot in (self._program, self._program_standby):
                        if slot is not None and slot.controller is not None: self._dispose(slot.controller)
                    self._program = _Slot(self.program_window,
                        controller=TextureController(self._gpu_session, 'a', self.program_window))
                    spare_window = self.program_standby_window or self.program_window
                    self._program_standby = _Slot(spare_window,
                        controller=TextureController(self._gpu_session, 'b', spare_window))
                    self._gpu_live_enabled = True
                    self._gpu_software_fallback = False
                    self._awaiting_program_swap = None
                if self._gpu_context and self._gpu_context['plan'].token not in {plan.token for plan in plans}:
                    self._gpu_cancel_context()
                self._gpu_plans = plans
                self._emit('gpu_live_state', {'enabled': True, 'reason': '', 'capabilities': self._gpu_session.capabilities})
                return
            except Exception as error:
                reason = str(error)
                self._error('GPU 实时预览不可用，使用原效果预览路径', error)
        was_enabled = self._gpu_live_enabled
        self._gpu_stop()
        if was_enabled:
            self._program = _Slot(self.program_window)
            self._program_standby = _Slot(self.program_standby_window) if self.program_standby_window is not None else None
        self._emit('gpu_live_state', {'enabled': False, 'reason': reason})

    def _gpu_side(self, camera, position, spec=None):
        spec = spec or self._clip(camera, position)
        if spec is None: return None
        key, path, native_time = spec
        clip = self._project.clip_at(camera, position)
        return LiveSide(camera, clip.id if clip is not None else '', path, tuple(key), position-native_time, position)

    def _gpu_ensure_side(self, slot, side, position, *, force=False, grades=None):
        if self._gpu_navigation_pending(): return False
        if slot.failed_key == side.asset_key: return False
        try:
            new = slot.key != side.asset_key
            controller = slot.controller
            controller.start()
            if new or force or not slot.active:
                self._gpu_pause(slot, True, force=True)
                self._set_picture_speed(slot, 1.)
                controller.command(['set_property', 'mute', True])
                if new: self._load_picture(controller, side.path)
                position = self._clock() if self._playing and not force else self._position
                target = max(0., side.native_time(position))
                # A normal cut may already have a decoded staging frame.
                # Preserve that frame while still verifying render readiness.
                ready = self._prepare_picture_frame(slot, target, force=force, newly_loaded=new)
                if self._gpu_navigation_pending(): return False
                slot.key, slot.failed_key, slot.active = side.asset_key, None, True
                latest = self._clock() if self._playing and not force else self._position
                if self._playing and side.native_time(latest) >= 0:
                    self._catch_up_picture(slot, side.native_time(latest)-ready)
                position = latest
            elif self._playing and side.native_time(position) >= 0:
                self._correct_picture_drift(slot, side.native_time(position))
            controller.set_grades(grades if grades is not None else side_grades(self._project, side, self._grade_draft))
            self._gpu_pause(slot, not (self._playing and side.native_time(position) >= 0))
            return True
        except Exception as error:
            if self._gpu_navigation_pending(): return False
            slot.failed_key = side.asset_key
            self._gpu_failover(side.camera, error)
            return False

    def _gpu_navigation_pending(self):
        pending = getattr(self, '_navigation_pending', None)
        return self._closing.is_set() or (callable(pending) and pending())

    def _gpu_compose(self, outgoing, incoming=None, *, plan=None, position=None):
        if self._gpu_navigation_pending(): return
        position = self._position if position is None else position
        ready = plan is not None and (not self._playing or plan.incoming.native_time(position) >= 0)
        active = bool(ready)
        rate = plan.fps/(plan.end_frame-plan.start_frame) if ready and self._playing else 0.
        delay = max(0., plan.start-position) if ready and self._playing else 0.
        return self._gpu_session.compose(front_slot=outgoing.controller.slot,
            other_slot=incoming.controller.slot if incoming is not None else None,
            kind=plan.kind if plan is not None else 'fade',
            progress=plan.progress(position) if plan is not None else 0., active=active,
            subtitle=subtitle_payload(self._project, position), progress_rate=rate, start_delay=delay)

    def _gpu_cancel_context(self):
        context, self._gpu_context = self._gpu_context, None
        if context is None: return
        for slot in (context['a'], context['b']):
            if slot is not self._program and slot.controller is not None: self._gpu_pause(slot, True)
        if self._program.active: self._gpu_compose(self._program)
        self._emit('live_transition', {'phase': 'cancel', 'kind': context['plan'].kind})

    def _gpu_begin_context(self, plan):
        a, b = self._program, self._program_standby
        if a.key == plan.incoming.asset_key and a.key != plan.outgoing.asset_key:
            a, b = b, a
        elif b.key == plan.outgoing.asset_key and a.key != plan.outgoing.asset_key:
            a, b = b, a
        self._gpu_context = {'plan': plan, 'a': a, 'b': b}
        return self._gpu_context

    def _gpu_finish_context(self, position):
        context = self._gpu_context
        if context is None or not context['b'].active: return
        outgoing, incoming = context['a'], context['b']
        self._gpu_compose(incoming, position=position)
        self._program, self._program_standby = incoming, outgoing
        self._gpu_pause(outgoing, True)
        self._gpu_context = None
        self._emit('live_transition', {'phase': 'finish', 'kind': context['plan'].kind,
                                      'time': position, 'progress': 1.})

    def _gpu_sync_single(self, position, *, force=False):
        camera = self._camera_at(position)
        spec = self._program_spec(camera, position)
        if spec is None:
            self._blank(self._program)
            self._gpu_session.compose(front_slot=self._program.controller.slot,
                active=False, blank=True, subtitle=subtitle_payload(self._project, position))
            return
        side = self._gpu_side(camera, position, spec)
        current = self._program
        target = current if current.key in (None, side.asset_key) else self._program_standby
        grades = () if self._is_baked_key(side.asset_key) else self._program_grade_chain(camera, position)
        if not self._gpu_ensure_side(target, side, position, force=force, grades=grades): return
        if self._gpu_navigation_pending(): return
        position = self._clock() if self._playing and not force else self._position
        self._gpu_compose(target, position=position)
        if target is not current:
            self._program, self._program_standby = target, current
            self._gpu_pause(current, True)

    def _gpu_sync_program(self, *, force=False, drift=False):
        if self._gpu_navigation_pending(): return
        # The audio seek acknowledgement precedes the decoder clock update.
        # A forced navigation must keep its requested time for both textures.
        position = self._clock() if self._playing and not force else self._position
        rendered = self._render_preview
        overrides = (self._comparison_enabled or self._audition is not None or
                     bool(rendered and rendered[1] <= position < rendered[2]))
        if overrides and self._gpu_context: self._gpu_cancel_context()
        active = None if overrides else next((plan for plan in self._gpu_plans if plan.contains(position)), None)
        context = self._gpu_context
        if context is not None and (position >= context['plan'].end or
                position < context['plan'].start-self.PREWARM_SECONDS):
            if position >= context['plan'].end and not force:
                self._gpu_finish_context(position)
            else:
                self._gpu_cancel_context()
            context = self._gpu_context
        if active is not None and (context is None or context['plan'].token != active.token):
            if context is not None: self._gpu_cancel_context()
            context = self._gpu_begin_context(active)
        single_synced = None
        if context is None:
            self._gpu_sync_single(position, force=force)
            if not self._gpu_live_enabled: return
            single_synced = self._program
            if not overrides:
                upcoming = next((plan for plan in self._gpu_plans if position < plan.start <= position+self.PREWARM_SECONDS), None)
                if upcoming is not None and self._program.key == upcoming.outgoing.asset_key:
                    context = self._gpu_begin_context(upcoming)
                elif not force:
                    self._prewarm_program(position)
        if context is None: return
        plan, a, b = context['plan'], context['a'], context['b']
        in_window = plan.contains(position)
        grades_a = side_grades(self._project, plan.outgoing, self._grade_draft) if in_window else self._program_grade_chain(plan.outgoing.camera, position)
        ready_a = self._gpu_ensure_side(a, plan.outgoing, position,
                                      force=force and a is not single_synced, grades=grades_a)
        if not ready_a or self._gpu_navigation_pending(): return
        ready_b = self._gpu_ensure_side(b, plan.incoming, position, force=force)
        if not ready_b or self._gpu_navigation_pending(): return
        position = self._clock() if self._playing and not force else self._position
        if position >= plan.end:
            self._gpu_finish_context(position)
            return
        self._program, self._program_standby = a, b
        self._gpu_compose(a, b, plan=plan, position=position)
        phase = 'mixing' if plan.contains(position) else 'armed'
        self._emit('live_transition', {'phase': phase, 'kind': plan.kind,
                                      'progress': plan.progress(position), 'time': position})

    def _gpu_refresh_grades(self):
        if not self._gpu_live_enabled: return
        context = self._gpu_context
        if context is not None:
            plan = context['plan']
            for label, side in (('a', plan.outgoing), ('b', plan.incoming)):
                slot = context[label]
                if slot.active:
                    grades = side_grades(self._project, side, self._grade_draft)
                    if label == 'a' and not plan.contains(self._position):
                        grades = self._program_grade_chain(side.camera, self._position)
                    slot.controller.set_grades(grades)
        elif self._program.active:
            grades = () if self._is_baked_key(self._program.key) else self._program_grade_chain(self._camera_at(self._position), self._position)
            self._program.controller.set_grades(grades)
