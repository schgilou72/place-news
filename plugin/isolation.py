"""Pad-to-pad isolation model (clearance / creepage) for the placer.

The rule set (net-class clearances + custom rules) gives, for every pair of
pads on different nets, the minimum copper-to-copper distance KiCad's DRC
will demand. Pairs whose requirement is no larger than ``threshold`` are
ignored: non-overlapping footprint boxes already keep pads that far apart.

Pads are reduced to "profiles" — only the attributes the rules actually look
at — so the requirement table stays small (typically a few net classes).

Geometry: each pad is its axis-aligned copper box. The gap between two pads
is the distance between the boxes, which is exact for rectangles and slightly
conservative for round pads. Requiring a straight-line gap >= creepage is
conservative too: KiCad's creepage path can only be longer (slots).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .rules import PadInfo, RuleSet

# Gaps within 1 µm of the requirement count as met (offsets are rounded to
# whole nanometres when footprints are rotated).
ISO_EPSILON = 1000


@dataclass
class IsoPad:
    """A pad taking part in isolation checks (geometry at footprint 0°)."""
    pad_index: int
    net_code: int          # 0 = no net (still isolated from everything)
    profile: int
    ox: int                # copper box centre offset from footprint origin (nm)
    oy: int
    hx: int                # copper box half-extents (nm)
    hy: int


@dataclass
class IsolationModel:
    threshold: int
    profiles: List[PadInfo] = field(default_factory=list)
    req: Dict[int, Dict[int, int]] = field(default_factory=dict)   # profile -> profile -> nm
    # Between two pads without net: KiCad tests clearance but not creepage
    # (both are "net 0"), so a separate, smaller table applies.
    req0: Dict[int, Dict[int, int]] = field(default_factory=dict)
    fp_pads: Dict[int, Dict[int, List[IsoPad]]] = field(default_factory=dict)  # fp -> profile -> pads
    fp_reach: Dict[int, int] = field(default_factory=dict)        # fp -> max requirement
    max_req: int = 0
    rules: Optional[RuleSet] = None

    @property
    def active(self) -> bool:
        return bool(self.fp_pads) and self.max_req > 0

    def pair_reach(self, i: int, j: int) -> int:
        """Largest requirement between any pad of footprint i and any of j."""
        pi = self.fp_pads.get(i)
        pj = self.fp_pads.get(j)
        if not pi or not pj:
            return 0
        best = 0
        for a in pi:
            row = self.req.get(a)
            if not row:
                continue
            for b in pj:
                r = row.get(b, 0)
                if r > best:
                    best = r
        return best


def _profile_key(info: PadInfo, attrs: FrozenSet[str], canon=None) -> PadInfo:
    """Keep only the attributes the rules use (plus net classes / clearance).
    `canon` maps a net name to a representative with the same rule outcome."""
    blank = {}
    if 'net_name' not in attrs:
        blank['net_name'] = ''
    elif canon is not None and info.net_name:
        rep = canon(info.net_name)
        if rep != info.net_name:
            blank['net_name'] = rep
    if 'fp_ref' not in attrs:
        blank['fp_ref'] = ''
    if 'fp_lib_id' not in attrs:
        blank['fp_lib_id'] = ''
    if 'component_classes' not in attrs:
        blank['component_classes'] = ()
    if 'sheet' not in attrs:
        blank['sheet'] = ''
    if 'groups' not in attrs:
        blank['groups'] = ()
    if 'pad_type' not in attrs and info.pad_type != 'npth':
        blank['pad_type'] = 'smd'
    if 'layers' not in attrs:
        blank['layers'] = ('F.Cu',)
    return replace(info, **blank) if blank else info


def build_isolation_model(
    pads: Sequence[Tuple[int, int, int, PadInfo, int, int, int, int]],
    rules: RuleSet,
    threshold: int,
) -> IsolationModel:
    """Build the model.

    pads: (fp_index, pad_index, net_code, info, ox, oy, hx, hy) for every
    copper pad of the board.
    """
    model = IsolationModel(threshold=threshold, rules=rules)
    attrs = rules.attrs
    canon = rules.net_canonicalizer()
    key_to_id: Dict[PadInfo, int] = {}
    pad_profiles: List[int] = []
    netless: set = set()            # profiles used by pads without net
    for (_fi, _pi, nc, info, _ox, _oy, _hx, _hy) in pads:
        key = _profile_key(info, attrs, canon)
        pid = key_to_id.get(key)
        if pid is None:
            pid = len(model.profiles)
            key_to_id[key] = pid
            model.profiles.append(key)
        pad_profiles.append(pid)
        if nc == 0:
            netless.add(pid)

    n = len(model.profiles)
    for a in range(n):
        for b in range(a, n):
            pa, pb = model.profiles[a], model.profiles[b]
            r = rules.requirement(pa, pb)
            if r > threshold:
                model.req.setdefault(a, {})[b] = r
                model.req.setdefault(b, {})[a] = r
                if r > model.max_req:
                    model.max_req = r
                if a in netless and b in netless:
                    no_net = (0 if 'npth' in (pa.pad_type, pb.pad_type)
                              else rules.clearance(pa, pb))
                    if no_net > threshold:
                        model.req0.setdefault(a, {})[b] = no_net
                        model.req0.setdefault(b, {})[a] = no_net

    if not model.req:
        return model

    for (fi, pi, nc, _info, ox, oy, hx, hy), pid in zip(pads, pad_profiles):
        if pid not in model.req:
            continue
        model.fp_pads.setdefault(fi, {}).setdefault(pid, []).append(
            IsoPad(pad_index=pi, net_code=nc, profile=pid, ox=ox, oy=oy, hx=hx, hy=hy))

    for fi, by_prof in model.fp_pads.items():
        model.fp_reach[fi] = max(max(model.req[p].values()) for p in by_prof)
    return model


def _pad_box(fp, p: IsoPad) -> Tuple[float, float, float, float]:
    """World centre and half-extents of a pad's copper box."""
    c = fp._cos_a
    s = fp._sin_a
    cx = fp.x + p.ox * c - p.oy * s
    cy = fp.y + p.ox * s + p.oy * c
    ac = abs(c)
    as_ = abs(s)
    return cx, cy, p.hx * ac + p.hy * as_, p.hx * as_ + p.hy * ac


def box_gap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    dx = abs(a[0] - b[0]) - (a[2] + b[2])
    dy = abs(a[1] - b[1]) - (a[3] + b[3])
    if dx <= 0 and dy <= 0:
        return 0.0
    if dx <= 0:
        return dy
    if dy <= 0:
        return dx
    return math.hypot(dx, dy)


def pair_shortfall(iso: IsolationModel, fps, i: int, j: int) -> float:
    """Sum over pad pairs (i, j) of max(0, required - gap), in nm."""
    pi = iso.fp_pads.get(i)
    pj = iso.fp_pads.get(j)
    if not pi or not pj:
        return 0.0
    fi = fps[i]
    fj = fps[j]
    total = 0.0
    for a, pads_a in pi.items():
        row = iso.req.get(a)
        if not row:
            continue
        for b, pads_b in pj.items():
            r = row.get(b)
            if not r:
                continue
            r0 = iso.req0.get(a, {}).get(b, 0)
            boxes_b = [(q.net_code, _pad_box(fj, q)) for q in pads_b]
            for p in pads_a:
                box_a = _pad_box(fi, p)
                for nc_b, box_b in boxes_b:
                    if p.net_code == nc_b:
                        if p.net_code != 0:
                            continue
                        need = r0            # two pads without net
                    else:
                        need = r
                    if not need:
                        continue
                    g = box_gap(box_a, box_b)
                    if g < need - ISO_EPSILON:
                        total += need - g
    return total


@dataclass
class Violation:
    fp_a: int
    pad_a: int
    fp_b: int
    pad_b: int
    gap: float
    required: int


def list_violations(iso: IsolationModel, fps, pairs: Optional[Sequence[Tuple[int, int]]] = None,
                    include_same_fp: bool = True) -> List[Violation]:
    """Every pad pair closer than required (pairs of footprints, plus pairs
    inside one footprint when include_same_fp). Sorted worst first."""
    out: List[Violation] = []
    idx = sorted(iso.fp_pads)
    if pairs is None:
        pairs = [(idx[x], idx[y]) for x in range(len(idx)) for y in range(x, len(idx))]
    for i, j in pairs:
        if i == j and not include_same_fp:
            continue
        pi = iso.fp_pads.get(i)
        pj = iso.fp_pads.get(j)
        if not pi or not pj:
            continue
        for a, pads_a in pi.items():
            row = iso.req.get(a, {})
            for b, pads_b in pj.items():
                r = row.get(b)
                if not r:
                    continue
                r0 = iso.req0.get(a, {}).get(b, 0)
                for p in pads_a:
                    box_a = _pad_box(fps[i], p)
                    for q in pads_b:
                        if i == j and q.pad_index <= p.pad_index:
                            continue
                        if p.net_code == q.net_code:
                            if p.net_code != 0:
                                continue
                            need = r0
                        else:
                            need = r
                        if not need:
                            continue
                        g = box_gap(box_a, _pad_box(fps[j], q))
                        if g < need - ISO_EPSILON:
                            out.append(Violation(i, p.pad_index, j, q.pad_index, g, need))
    out.sort(key=lambda v: v.gap - v.required)
    return out
