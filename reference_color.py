"""Conservative camera colour matching from neutral and skin-colour cohorts.

Only metadata-free image statistics are used.  Bright low-chroma pixels are
preferred over whole-frame averages, which would confuse differently framed
purple stage backgrounds with a camera white-balance error.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class WhiteBalancePlan:
    gains: tuple[float, float, float] = (1., 1., 1.)
    source: str = ''
    used_reference: bool = False
    confidence: float = 0.
    warnings: tuple[str, ...] = ()

    @property
    def summary(self):
        if self.used_reference:
            return '已参考其他机位的'+self.source+'校正偏色'
        if self.source:
            return '已按本片段的'+self.source+'校正白平衡'
        return '缺少可靠中性色，以肤色色域去黄为主，保留全景白平衡'


def _anchor(analysis, name):
    rgb = getattr(analysis, name+'_rgb', ())
    fraction = getattr(analysis, name+'_fraction', 0.)
    count = getattr(analysis, name+'_count', 0)
    if (len(rgb) == 3 and count >= 32 and fraction >= .003
            and all(math.isfinite(value) and .025 < value < .999 for value in rgb)):
        return tuple(rgb)
    return None


def white_balance_plan(analysis, reference_analysis=None, *, strength=1.) -> WhiteBalancePlan:
    """Return bounded attenuation gains; exposure can then reuse the headroom.

    Normalising the largest gain to one avoids blowing the blue highlights
    when correcting a yellow camera.  The analysis layer compensates for the
    resulting luminance reduction, before its ordinary highlight protection.
    """
    if not isinstance(strength, (int,float)) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('白平衡强度必须在 0 到 1 之间')
    candidates = (('bright_neutral','亮部中性色',1.), ('neutral','中性色',.82),
                  ('skin','肤色色域',.48))
    notes=[]
    target=desired=None
    source='';confidence=0.;used_reference=False
    if reference_analysis is not None:
        minimum=min(3,max(1,getattr(reference_analysis,'samples_requested',1)))
        if getattr(reference_analysis,'usable',False) and getattr(reference_analysis,'samples_used',0)>=minimum:
            for name,label,weight in candidates:
                first,second=_anchor(analysis,name),_anchor(reference_analysis,name)
                if first is not None and second is not None:
                    target,desired,source,confidence=first,second,label,weight
                    used_reference=True;break
        if not used_reference:
            notes.append('参考机位缺少足够的共同中性或肤色样本，已回退本片段自动校正。')
    if target is None:
        for name,label,weight in candidates[:2]:
            first=_anchor(analysis,name)
            if first is not None:
                target=first;desired=(1.,1.,1.);source=label;confidence=weight*.9;break
    if target is None:
        return WhiteBalancePlan(warnings=tuple(notes))
    first_mean=sum(target)/3.;second_mean=sum(desired)/3.
    ratios=[(second/second_mean)/(first/first_mean) for first,second in zip(target,desired)]
    logs=[math.log(value) for value in ratios]
    spread=max(logs)-min(logs)
    # A skin-only match is less certain than an illuminated white dress.
    limit=math.log(1.42 if source=='亮部中性色' else 1.32 if source=='中性色' else 1.20)
    scale=min(1.,limit/spread) if spread>1e-12 else 1.
    amount=(1. if used_reference else .9)*strength
    if source=='肤色色域': amount*=.78
    maximum=max(logs)
    gains=tuple(max(.5,min(1.,math.exp((value-maximum)*scale*amount))) for value in logs)
    return WhiteBalancePlan(gains,source,used_reference,confidence,tuple(notes))


def reference_match_summary(analysis, reference_analysis=None):
    plan=white_balance_plan(analysis,reference_analysis)
    text=plan.summary
    if plan.warnings:text+='；'+'；'.join(plan.warnings)
    return text
