"""Tidy-up after placement: rows and columns.

Parts of the same type (same footprint, same orientation axis) that sit
roughly in a row are put on one line through their centres, and parts in a
row of three or more are evenly spaced; the same for columns. A move is kept
only if no rule gets worse (overlap, isolation, keep-out, placement area,
board edge) and the extra wirelength stays within a small budget.

Pure Python: works on the BoardModel / CostState used by the annealer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .board_model import BoardModel
from .cost_function import CostState

MM = 1_000_000


@dataclass
class AlignReport:
    lines: int = 0            # rows + columns that ended up aligned
    parts_moved: int = 0
    hpwl_before: int = 0
    hpwl_after: int = 0


def type_key(fp) -> Tuple[str, int]:
    """Parts with the same key look alike: same footprint (or reference
    prefix when unknown) and same orientation axis."""
    name = getattr(fp, 'lib_id', '') or ''
    name = name.split(':')[-1] if name else re.match(r'[A-Za-z_#]*', fp.reference).group(0)
    return name, int(round(fp.angle_deg)) % 180


def body_center(fp) -> Tuple[int, int]:
    b = fp.bbox
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2


def _penalties(cs: CostState) -> float:
    return (cs._overlap_penalty + cs._iso_penalty + cs._boundary_penalty
            + cs._keepout_penalty + cs._area_penalty)


class _Mover:
    """Applies single-part moves that keep every rule and fit the budget."""

    def __init__(self, model: BoardModel, cs: CostState, budget: float):
        from .moves import _hits_new_keepout, _hits_polygon_cutout
        self._hits_keepout = _hits_new_keepout
        self._hits_cutout = _hits_polygon_cutout
        self.model = model
        self.cs = cs
        self.budget = budget
        self.moved: Set[int] = set()

    def try_move(self, idx: int, dx: int, dy: int) -> bool:
        if dx == 0 and dy == 0:
            return True
        fp = self.model.footprints[idx]
        old = [(idx, fp.x, fp.y, fp.angle_deg)]
        pen0 = _penalties(self.cs)
        h0 = self.cs.hpwl
        snap = self.cs.snapshot({idx})
        fp.x += dx
        fp.y += dy
        if self._hits_keepout(self.model, [idx], old) or self._hits_cutout(self.model, [idx], old):
            fp.x, fp.y = old[0][1], old[0][2]
            return False
        self.cs.incremental_update({idx})
        extra = self.cs.hpwl - h0
        if _penalties(self.cs) > pen0 + 1.0 or extra > self.budget:
            fp.x, fp.y = old[0][1], old[0][2]
            self.cs.restore(snap)
            return False
        self.budget -= max(0, extra)
        self.moved.add(idx)
        return True


def _clusters(members: List[int], centers: Dict[int, Tuple[int, int]],
              sizes: Dict[int, Tuple[int, int]], axis: int) -> List[List[int]]:
    """Groups of parts roughly on one line. axis 0: rows (same y), 1: columns
    (same x). Two parts are linked when their centres are offset across the
    line by less than about half their size, and close along it."""
    along, across = (0, 1) if axis == 0 else (1, 0)
    parent = {m: m for m in members}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for ia in range(len(members)):
        a = members[ia]
        for b in members[ia + 1:]:
            span_a, span_b = sizes[a][along], sizes[b][along]
            thick = max(sizes[a][across], sizes[b][across])
            snap = max(MM // 2, int(0.6 * thick))
            reach = 3 * max(span_a, span_b) + 3 * MM
            d_across = abs(centers[a][across] - centers[b][across])
            d_along = abs(centers[a][along] - centers[b][along])
            if d_across <= snap and d_along <= reach:
                parent[find(a)] = find(b)
    groups: Dict[int, List[int]] = {}
    for m in members:
        groups.setdefault(find(m), []).append(m)
    return [g for g in groups.values() if len(g) >= 2]


def align_rows(model: BoardModel, cs: CostState, budget_ratio: float = 0.03,
               fixed_anchors: bool = True) -> AlignReport:
    """Align same-type parts in rows / columns and space them evenly."""
    report = AlignReport(hpwl_before=cs.hpwl)
    grouped = set(model.fp_to_group) if model.fp_to_group else set()
    moveable = [i for i in model.moveable_indices if i not in grouped]
    if not moveable:
        report.hpwl_after = cs.hpwl
        return report
    budget = max(budget_ratio * max(cs.hpwl, 1), 2 * MM)
    mover = _Mover(model, cs, budget)

    by_type: Dict[Tuple[str, int], List[int]] = {}
    for i in moveable:
        by_type.setdefault(type_key(model.footprints[i]), []).append(i)
    anchors: Dict[Tuple[str, int], List[int]] = {}
    if fixed_anchors:
        for fp in model.footprints:
            if fp.index not in moveable and fp.locked:
                anchors.setdefault(type_key(fp), []).append(fp.index)

    def sizes_of(idx: int) -> Tuple[int, int]:
        b = model.footprints[idx].bbox
        return b[2] - b[0], b[3] - b[1]

    aligned: Dict[int, Set[int]] = {0: set(), 1: set()}
    lines: List[Tuple[int, List[int]]] = []
    for key, members in by_type.items():
        fixed = anchors.get(key, [])
        for axis in (0, 1):
            pool = members + fixed
            centers = {i: body_center(model.footprints[i]) for i in pool}
            sizes = {i: sizes_of(i) for i in pool}
            for cluster in sorted(_clusters(pool, centers, sizes, axis), key=len, reverse=True):
                movers = [i for i in cluster if i not in fixed]
                if not movers:
                    continue
                across = 1 - axis
                locked_vals = sorted(centers[i][across] for i in cluster if i in fixed)
                vals = locked_vals or sorted(centers[i][across] for i in cluster)
                target = vals[len(vals) // 2]
                done = [i for i in cluster if i in fixed or centers[i][across] == target]
                for i in sorted(movers, key=lambda k: abs(centers[k][across] - target)):
                    if i in done:
                        continue
                    c = body_center(model.footprints[i])
                    delta = target - c[across]
                    if mover.try_move(i, delta if axis == 1 else 0, delta if axis == 0 else 0):
                        done.append(i)
                if len(done) >= 2:
                    aligned[axis].update(done)
                    lines.append((axis, done))

    # Even spacing inside lines of three or more
    for axis, line in lines:
        along = axis
        line = sorted(line, key=lambda k: body_center(model.footprints[k])[along])
        if len(line) < 3:
            continue
        first = body_center(model.footprints[line[0]])[along]
        last = body_center(model.footprints[line[-1]])[along]
        step = (last - first) / (len(line) - 1)
        for k, i in enumerate(line[1:-1], start=1):
            if i not in moveable or i in aligned[1 - axis]:
                continue
            c = body_center(model.footprints[i])[along]
            delta = int(round(first + k * step)) - c
            mover.try_move(i, delta if axis == 0 else 0, delta if axis == 1 else 0)

    report.lines = len(lines)
    report.parts_moved = len(mover.moved)
    report.hpwl_after = cs.hpwl
    return report
