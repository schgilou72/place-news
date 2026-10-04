"""Tidy-up after placement: rows and columns, and an untangled ratsnest.

Parts of the same type (same footprint, same orientation axis) that sit
roughly in a row are put on one line through their centres, and parts in a
row of three or more are evenly spaced; the same for columns.

Untangling (a cost reduction): every crossing of two ratsnest lines tends
to cost a via. Identical parts swap places and two-pad parts turn round
(180°) when that removes crossings; rows, columns and orientation axes are
kept as they are.

A move is kept only if no rule gets worse (overlap, isolation, keep-out,
placement area, board edge) and the extra wirelength stays within a small
budget.

Pure Python: works on the BoardModel / CostState used by the annealer.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .board_model import BoardModel
from .cost_function import CostState

MM = 1_000_000


@dataclass
class AlignReport:
    lines: int = 0            # rows + columns that ended up aligned
    parts_moved: int = 0
    hpwl_before: int = 0
    hpwl_after: int = 0
    rotated: int = 0          # parts turned like the others of their type


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
    """Applies part moves that keep every rule and fit the budget."""

    def __init__(self, model: BoardModel, cs: CostState, budget: float):
        from .moves import _hits_new_keepout, _hits_polygon_cutout
        self._hits_keepout = _hits_new_keepout
        self._hits_cutout = _hits_polygon_cutout
        self.model = model
        self.cs = cs
        self.budget = budget
        self.moved: Set[int] = set()

    def _attempt(self, idxs: List[int], change: Callable[[], None],
                 accept: Optional[Callable[[], bool]] = None,
                 on_undo: Optional[Callable[[], None]] = None) -> bool:
        """Apply change() to the parts idxs; keep it only if accept() agrees,
        no rule gets worse and the extra wirelength fits the budget."""
        fps = self.model.footprints
        old = [(i, fps[i].x, fps[i].y, fps[i].angle_deg) for i in idxs]
        moved = set(idxs)
        pen0 = _penalties(self.cs)
        h0 = self.cs.hpwl
        snap = self.cs.snapshot(moved)
        change()

        def undo(cost_updated: bool) -> bool:
            for i, x, y, a in old:
                fp = fps[i]
                fp.x, fp.y = x, y
                if fp.angle_deg != a:
                    fp.set_angle(a)
            if cost_updated:
                self.cs.restore(snap)
            if on_undo is not None:
                on_undo()
            return False

        if accept is not None and not accept():
            return undo(False)
        if self._hits_keepout(self.model, list(idxs), old) or self._hits_cutout(self.model, list(idxs), old):
            return undo(False)
        self.cs.incremental_update(moved)
        extra = self.cs.hpwl - h0
        if _penalties(self.cs) > pen0 + 1.0 or extra > self.budget:
            return undo(True)
        self.budget -= max(0, extra)
        self.moved.update(moved)
        return True

    def try_rotate(self, idx: int, angle: float, accept=None, on_undo=None) -> bool:
        """Turn a part about its body centre (the centre stays in place)."""
        fp = self.model.footprints[idx]

        def change():
            c0 = body_center(fp)
            fp.set_angle(angle % 360.0)
            c1 = body_center(fp)
            fp.x += c0[0] - c1[0]
            fp.y += c0[1] - c1[1]
        return self._attempt([idx], change, accept, on_undo)

    def try_move(self, idx: int, dx: int, dy: int, accept=None, on_undo=None) -> bool:
        if dx == 0 and dy == 0:
            return True
        fp = self.model.footprints[idx]

        def change():
            fp.x += dx
            fp.y += dy
        return self._attempt([idx], change, accept, on_undo)

    def try_swap(self, i: int, j: int, accept=None, on_undo=None) -> bool:
        """Exchange the places of two parts (body centre to body centre)."""
        fi, fj = self.model.footprints[i], self.model.footprints[j]

        def change():
            ci, cj = body_center(fi), body_center(fj)
            fi.x += cj[0] - ci[0]
            fi.y += cj[1] - ci[1]
            fj.x += ci[0] - cj[0]
            fj.y += ci[1] - cj[1]
        return self._attempt([i, j], change, accept, on_undo)


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


def harmonize_orientation(model: BoardModel, cs: CostState, budget_ratio: float = 0.02) -> int:
    """Turn parts so that every part of one footprint type lies along the
    same axis as most of its siblings (0/180 or 90/270). Same safeguards as
    the row tidy-up. Returns the number of parts turned."""
    grouped = set(model.fp_to_group) if model.fp_to_group else set()
    moveable = [i for i in model.moveable_indices if i not in grouped]
    if not moveable:
        return 0
    mover = _Mover(model, cs, max(budget_ratio * max(cs.hpwl, 1), MM))
    by_name: Dict[str, List[int]] = {}
    for fp in model.footprints:
        by_name.setdefault(type_key(fp)[0], []).append(fp.index)
    turned = 0
    for name, members in by_name.items():
        if len(members) < 2:
            continue
        axes: Dict[int, List[int]] = {}
        for i in members:
            axes.setdefault(int(round(model.footprints[i].angle_deg)) % 180, []).append(i)
        if len(axes) < 2:
            continue
        major = max(axes, key=lambda a: (len(axes[a]), -a))
        for axis, idxs in axes.items():
            if axis == major:
                continue
            for i in idxs:
                if i not in moveable:
                    continue
                a = model.footprints[i].angle_deg
                for target in (a + (major - axis), a + (major - axis) + 180):
                    if mover.try_rotate(i, target):
                        turned += 1
                        break
    return turned


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


# ---------------------------------------------------------------------------
# Untangling the ratsnest (fewer crossings -> fewer vias)
# ---------------------------------------------------------------------------

Edge = Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int, int, int]]
SWAP_PARTNERS = 6          # each part tries to swap with its 6 nearest identical parts


@dataclass
class UntangleReport:
    crossings_before: int = 0
    crossings_after: int = 0
    swaps: int = 0            # identical parts that exchanged places
    flips: int = 0            # two-pad parts turned round
    hpwl_before: int = 0
    hpwl_after: int = 0


class Ratsnest:
    """Ratsnest of the signal nets (power nets are left out, as for the
    wirelength: they usually go to a plane or a pour): the minimum spanning
    tree of each net's pads, and the crossings between nets. Lines are kept
    in a grid of cells so that a move only looks at its neighbourhood."""

    def __init__(self, model: BoardModel):
        self.model = model
        self.nets = [nc for nc, net in model.nets.items()
                     if not net.is_excluded and len(net.pad_refs) >= 2]
        self.fp_nets: Dict[int, Set[int]] = {}
        for nc in self.nets:
            for fi, _pi in model.nets[nc].pad_refs:
                self.fp_nets.setdefault(fi, set()).add(nc)
        span = max(model.outline_xmax - model.outline_xmin,
                   model.outline_ymax - model.outline_ymin, MM)
        self.cell = max(span // 40, MM)
        self.grid: Dict[Tuple[int, int], Set[Tuple[int, int]]] = {}
        self.edges: Dict[int, List[Edge]] = {}
        for nc in self.nets:
            self.edges[nc] = self._mst(nc)
            self._index(nc)

    # -- geometry --------------------------------------------------------------
    def _points(self, nc: int) -> List[Tuple[int, int]]:
        pts = []
        fps = self.model.footprints
        for fi, pi in self.model.nets[nc].pad_refs:
            fp = fps[fi]
            pad = fp.pads[pi]
            pts.append((fp.x + int(pad.offset_x * fp._cos_a - pad.offset_y * fp._sin_a),
                        fp.y + int(pad.offset_x * fp._sin_a + pad.offset_y * fp._cos_a)))
        return list(dict.fromkeys(pts))

    def _mst(self, nc: int) -> List[Edge]:
        pts = self._points(nc)
        n = len(pts)
        edges: List[Edge] = []
        if n < 2:
            return edges
        in_tree = [False] * n
        best = [math.inf] * n
        link = [-1] * n
        best[0] = 0.0
        for _ in range(n):
            u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i])
            in_tree[u] = True
            if link[u] >= 0:
                a, b = pts[link[u]], pts[u]
                edges.append((a, b, (min(a[0], b[0]), min(a[1], b[1]),
                                     max(a[0], b[0]), max(a[1], b[1]))))
            for v in range(n):
                if not in_tree[v]:
                    d = math.hypot(pts[u][0] - pts[v][0], pts[u][1] - pts[v][1])
                    if d < best[v]:
                        best[v], link[v] = d, u
        return edges

    def _cells(self, box: Tuple[int, int, int, int]):
        c = self.cell
        for cx in range(box[0] // c, box[2] // c + 1):
            for cy in range(box[1] // c, box[3] // c + 1):
                yield cx, cy

    def _index(self, nc: int) -> None:
        for k, (_a, _b, box) in enumerate(self.edges[nc]):
            for cell in self._cells(box):
                self.grid.setdefault(cell, set()).add((nc, k))

    def _unindex(self, nc: int) -> None:
        for k, (_a, _b, box) in enumerate(self.edges.get(nc, ())):
            for cell in self._cells(box):
                bucket = self.grid.get(cell)
                if bucket is not None:
                    bucket.discard((nc, k))
                    if not bucket:
                        del self.grid[cell]

    def refresh(self, nets: Iterable[int]) -> None:
        for nc in nets:
            self._unindex(nc)
            self.edges[nc] = self._mst(nc)
            self._index(nc)

    # -- crossings -------------------------------------------------------------
    def _crossing_edges(self, nc: int, edge: Edge, skip=None):
        """(other net, edge index) of the lines of other nets crossing `edge`
        (proper crossings: touching ends do not count)."""
        (ax1, ay1), (ax2, ay2), ba = edge
        dax, day = ax2 - ax1, ay2 - ay1
        seen: Set[Tuple[int, int]] = set()
        edges = self.edges
        for cell in self._cells(ba):
            for key in self.grid.get(cell, ()):
                if key in seen or key[0] == nc or (skip is not None and skip(key[0])):
                    continue
                seen.add(key)
                (bx1, by1), (bx2, by2), bb = edges[key[0]][key[1]]
                if ba[2] < bb[0] or bb[2] < ba[0] or ba[3] < bb[1] or bb[3] < ba[1]:
                    continue
                d1 = dax * (by1 - ay1) - day * (bx1 - ax1)
                d2 = dax * (by2 - ay1) - day * (bx2 - ax1)
                if d1 * d2 >= 0:
                    continue
                dbx, dby = bx2 - bx1, by2 - by1
                d3 = dbx * (ay1 - by1) - dby * (ax1 - bx1)
                d4 = dbx * (ay2 - by1) - dby * (ax2 - bx1)
                if d3 * d4 < 0:
                    yield key

    def crossings(self, nets: Optional[Iterable[int]] = None) -> int:
        """Crossings that involve at least one ratsnest line of these nets
        (all nets when None), each crossing counted once."""
        chosen = set(self.nets if nets is None else nets) & set(self.edges)
        total = 0
        for nc in chosen:
            def skip(oc, nc=nc):
                return oc in chosen and oc < nc
            for edge in self.edges[nc]:
                total += sum(1 for _ in self._crossing_edges(nc, edge, skip))
        return total

    def crossing_nets(self) -> Set[int]:
        """Nets that cross at least one other net."""
        hot: Set[int] = set()
        for nc, edges in self.edges.items():
            for edge in edges:
                for oc, _k in self._crossing_edges(nc, edge):
                    hot.update((nc, oc))
        return hot


def untangle(model: BoardModel, cs: CostState, budget_ratio: float = 0.02,
             max_passes: int = 4) -> UntangleReport:
    """Fewer ratsnest crossings: identical parts (same footprint, same axis,
    same side) swap places, two-pad parts turn round. Only moves that remove
    crossings are tried; the usual safeguards apply."""
    report = UntangleReport(hpwl_before=cs.hpwl, hpwl_after=cs.hpwl)
    grouped = set(model.fp_to_group) if model.fp_to_group else set()
    moveable = [i for i in model.moveable_indices if i not in grouped]
    rn = Ratsnest(model)
    report.crossings_before = report.crossings_after = rn.crossings()
    if not moveable or report.crossings_before == 0:
        return report
    mover = _Mover(model, cs, max(budget_ratio * max(cs.hpwl, 1), MM))

    by_type: Dict[Tuple, List[int]] = {}
    for i in moveable:
        fp = model.footprints[i]
        by_type.setdefault(type_key(fp) + (fp.side,), []).append(i)
    two_pads = []
    for i in moveable:
        nets = [p.net_code for p in model.footprints[i].pads if p.net_code]
        if len(model.footprints[i].pads) == 2 and len(set(nets)) == 2 and rn.fp_nets.get(i):
            two_pads.append(i)

    def attempt(nets: Set[int], before: int, do) -> bool:
        def accept() -> bool:
            rn.refresh(nets)
            return rn.crossings(nets) < before
        return do(accept, lambda: rn.refresh(nets))

    # Swap partners: the nearest identical parts (far swaps stretch the wires).
    partners: Dict[int, List[int]] = {}
    for members in by_type.values():
        if len(members) < 2:
            continue
        centres = {i: body_center(model.footprints[i]) for i in members}
        for i in members:
            near = sorted((j for j in members if j != i),
                          key=lambda j: abs(centres[i][0] - centres[j][0])
                          + abs(centres[i][1] - centres[j][1]))
            partners[i] = near[:SWAP_PARTNERS]
    pairs = sorted({(min(i, j), max(i, j)) for i, near in partners.items() for j in near})

    for _ in range(max_passes):
        improved = False
        hot_nets = rn.crossing_nets()
        if not hot_nets:
            break
        hot = {fi for fi, nets in rn.fp_nets.items() if nets & hot_nets}
        for i, j in pairs:
            if i not in hot and j not in hot:
                continue
            ni, nj = rn.fp_nets.get(i, set()), rn.fp_nets.get(j, set())
            if ni == nj:
                continue
            nets = ni | nj
            before = rn.crossings(nets)
            if before == 0:
                continue
            if attempt(nets, before, lambda acc, und, i=i, j=j: mover.try_swap(i, j, acc, und)):
                report.swaps += 1
                improved = True
        for i in two_pads:
            if i not in hot:
                continue
            nets = set(rn.fp_nets[i])
            before = rn.crossings(nets)
            if before == 0:
                continue
            angle = model.footprints[i].angle_deg + 180.0
            if attempt(nets, before,
                       lambda acc, und, i=i, angle=angle: mover.try_rotate(i, angle, acc, und)):
                report.flips += 1
                improved = True
        if not improved:
            break
    report.crossings_after = rn.crossings()
    report.hpwl_after = cs.hpwl
    return report
