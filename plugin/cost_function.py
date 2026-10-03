"""Cost function: HPWL + overlap + isolation + boundary + keep-out + placement areas.

Supports incremental updates: when footprints move, only affected pairs/items
are recomputed, reducing per-move cost from O(n²) to O(n).

Overlap detection uses flat bbox arrays plus a sorted xmin list for O(log n + k)
neighbor scans, where k is the number of footprints whose left edge is left of
the query's right edge. The bisect narrows the scan vs the previous O(n) flat scan.

Isolation (clearance / creepage between pads of different nets, from the net
classes and the custom rules) is a pairwise term like overlap and is always at
full weight. Boundary, keep-out and placement-area terms are scaled by the
temperature-dependent penalty_scale.
"""
from __future__ import annotations

import bisect
import math
import time
from typing import Dict, List, Optional, Set, Tuple
from .board_model import BoardModel, Net, KeepOut, PlacementArea, polygon_is_rectangle
from .isolation import pair_shortfall


OVERLAP_WEIGHT = 50.0       # per-pair, multiplied by overlap distance (nm)
BOUNDARY_WEIGHT = 50.0      # per-footprint, multiplied by distance outside rect bbox (nm)
POLYGON_BOUNDARY_WEIGHT = 500.0  # polygon cutout violations (nm); 10× BOUNDARY_WEIGHT
                            # so that at high T (penalty_scale_min=0.1) the effective
                            # weight equals OVERLAP_WEIGHT — preventing SA from trading
                            # cutout placement for overlap relief even at high temperature.
KEEPOUT_WEIGHT = 100.0      # per-violation, multiplied by overlap distance (nm)
ISOLATION_WEIGHT = 100.0    # per pad pair, multiplied by the missing distance (nm)
AREA_WEIGHT = 50.0          # member outside its placement area, per nm outside


def point_in_polygon(px: int, py: int,
                     polygon: List[Tuple[int, int]]) -> bool:
    """Ray-casting point-in-polygon test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _dist_to_segment_sq(px: int, py: int,
                        ax: int, ay: int,
                        bx: int, by: int) -> float:
    """Squared distance from point (px,py) to line segment (ax,ay)-(bx,by)."""
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        ex, ey = px - ax, py - ay
        return float(ex * ex + ey * ey)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    nx = ax + t * dx
    ny = ay + t * dy
    ex, ey = px - nx, py - ny
    return ex * ex + ey * ey


def dist_to_polygon(px: int, py: int,
                    polygon: List[Tuple[int, int]]) -> float:
    """Distance from point to nearest polygon edge (nm)."""
    n = len(polygon)
    min_d_sq = float('inf')
    for i in range(n):
        j = (i + 1) % n
        d_sq = _dist_to_segment_sq(px, py,
                                   polygon[i][0], polygon[i][1],
                                   polygon[j][0], polygon[j][1])
        if d_sq < min_d_sq:
            min_d_sq = d_sq
    return math.sqrt(min_d_sq)


def _polygon_is_rectangular(poly: List[Tuple[int, int]],
                             xmin: int, ymin: int,
                             xmax: int, ymax: int,
                             tol: int = 1000) -> bool:
    """Kept for backward compatibility; see board_model.polygon_is_rectangle."""
    return polygon_is_rectangle(poly, tol)


def _segments_intersect(p1, p2, q1, q2) -> bool:
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 0) - (v < 0)

    def on_seg(a, b, c):
        return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))

    o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
    o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and on_seg(p1, p2, q1)) or (o2 == 0 and on_seg(p1, p2, q2))
            or (o3 == 0 and on_seg(q1, q2, p1)) or (o4 == 0 and on_seg(q1, q2, p2)))


def rect_intersects_polygon(x1: int, y1: int, x2: int, y2: int,
                            poly: List[Tuple[int, int]]) -> bool:
    """True if the axis-aligned rectangle and the polygon share any area."""
    for px, py in poly:
        if x1 < px < x2 and y1 < py < y2:
            return True
    for cx, cy in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        if point_in_polygon(cx, cy, poly):
            return True
    rect = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    n = len(poly)
    for k in range(n):
        a, b = poly[k], poly[(k + 1) % n]
        for m in range(4):
            if _segments_intersect(a, b, rect[m], rect[(m + 1) % 4]):
                return True
    return False


def keepout_applies(ko: KeepOut, side: str, copper_sides: Tuple[str, ...] = ()) -> bool:
    """Does this keep-out concern a footprint on `side` whose pads are on
    `copper_sides` (through-hole parts: both)?"""
    if ko.sides is None:
        return True
    if ko.no_footprints and side in ko.sides:
        return True
    if ko.no_pads and any(s in ko.sides for s in (copper_sides or (side,))):
        return True
    return False


def keepout_overlap(ko: KeepOut, side: str,
                    x1: int, y1: int, x2: int, y2: int,
                    copper_sides: Tuple[str, ...] = ()) -> Tuple[int, int]:
    """(ox, oy) overlap of a footprint box with a keep-out, (0, 0) when the
    keep-out does not apply to that footprint or does not touch the box."""
    if not keepout_applies(ko, side, copper_sides):
        return 0, 0
    ox = min(x2, ko.xmax) - max(x1, ko.xmin)
    oy = min(y2, ko.ymax) - max(y1, ko.ymin)
    if ox <= 0 or oy <= 0:
        return 0, 0
    if ko.polygon is not None and not rect_intersects_polygon(x1, y1, x2, y2, ko.polygon):
        return 0, 0
    return ox, oy


def area_outside(area: PlacementArea, x1: int, y1: int, x2: int, y2: int) -> float:
    """How far (nm, summed) a footprint box sticks out of a placement area."""
    dx = max(0, area.xmin - x1) + max(0, x2 - area.xmax)
    dy = max(0, area.ymin - y1) + max(0, y2 - area.ymax)
    total = float(dx + dy)
    if total == 0 and area.polygon is not None:
        for cx, cy in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
            if not point_in_polygon(cx, cy, area.polygon):
                total += dist_to_polygon(cx, cy, area.polygon)
    return total


def area_intrusion(area: PlacementArea, x1: int, y1: int, x2: int, y2: int) -> int:
    """How deep (ox + oy) a footprint box enters an area it is not a member of."""
    ox = min(x2, area.xmax) - max(x1, area.xmin)
    oy = min(y2, area.ymax) - max(y1, area.ymin)
    if ox <= 0 or oy <= 0:
        return 0
    if area.polygon is not None and not rect_intersects_polygon(x1, y1, x2, y2, area.polygon):
        return 0
    return ox + oy


def _pair_key(i: int, j: int) -> Tuple[int, int]:
    return (i, j) if i < j else (j, i)


class CostState:
    """
    Maintains the current cost and supports incremental updates.

    Total cost = sum_of_net_hpwl + overlap_penalty + isolation_penalty
                 + (boundary + keepout + area) * penalty_scale

    Per-net HPWL values are cached so that when a footprint moves,
    only its connected nets need recomputation.

    Per-pair overlap / isolation values and per-footprint boundary, keepout and
    area penalties are cached so incremental updates are O(n) instead of O(n²).
    """

    def __init__(self, model: BoardModel, quiet: bool = False):
        self.model = model
        self.net_hpwl: Dict[int, int] = {}
        self._total_hpwl: int = 0
        self._overlap_penalty: float = 0.0
        self._boundary_penalty: float = 0.0
        self._keepout_penalty: float = 0.0
        self._iso_penalty: float = 0.0
        self._area_penalty: float = 0.0
        self.penalty_scale: float = 1.0  # scales boundary+keepout+area (not overlap/isolation)

        # Caches for incremental updates
        self._pair_overlaps: Dict[Tuple[int, int], float] = {}
        self._iso_pairs: Dict[Tuple[int, int], float] = {}
        self._fp_boundary: Dict[int, float] = {}
        self._fp_keepout: Dict[int, float] = {}
        self._fp_area: Dict[int, float] = {}

        # Isolation model (None when there is nothing beyond the box margins)
        iso = model.isolation
        self._iso = iso if (iso is not None and iso.active) else None
        self._iso_fps: List[int] = sorted(self._iso.fp_pads) if self._iso else []

        # Placement areas: per footprint, the areas it belongs to
        self._areas = model.placement_areas
        self._fp_areas: Dict[int, List[int]] = {}
        for ai, area in enumerate(self._areas):
            for fi in area.members:
                self._fp_areas.setdefault(fi, []).append(ai)

        # Flat bbox arrays + sorted xmin list for O(log n + k) overlap neighbor scans
        n = len(model.footprints)
        self._bx1 = [0] * n
        self._by1 = [0] * n
        self._bx2 = [0] * n
        self._by2 = [0] * n
        self._xmin_items: List[Tuple[int, int]] = []  # sorted (xmin, fp_index)
        self._rebuild_bbox_arrays()

        # Sub-timers for profiling incremental_update breakdown
        self.t_hpwl = 0.0
        self.t_overlap = 0.0
        self.t_iso = 0.0
        self.t_boundary = 0.0
        self.t_keepout = 0.0
        self.t_snapshot = 0.0

        # Precomputed per-footprint net scale (max(1, len(net_codes))).
        # Net codes never change during a run, so this is safe to cache once.
        self._fp_net_scale: List[int] = [
            max(1, len(fp.net_codes)) for fp in model.footprints
        ]

        # Cached board extents — avoid self.model.X attribute chain in hot path.
        self._oxmin: int = model.outline_xmin
        self._oymin: int = model.outline_ymin
        self._oxmax: int = model.outline_xmax
        self._oymax: int = model.outline_ymax
        self._keepouts = model.keepouts

        # Polygon boundary check — skip for rectangular boards.
        # For a rectangle the simple bbox dx/dy check (below) is sufficient;
        # point_in_polygon would always return True for any interior point,
        # so the polygon loop would run ~4 * n_vertices iterations per move
        # for zero benefit.
        poly = model.outline_polygon
        if poly is not None and polygon_is_rectangle(poly):
            poly = None  # treat as rectangular: bbox check is sufficient
        self._outline_polygon = poly

        # Log excluded power nets once at startup (suppressed for preview runs)
        if not quiet:
            power_nets = [net for net in model.nets.values() if net.is_excluded]
            if power_nets:
                names = ', '.join(n.net_name for n in power_nets[:10])
                suffix = f' (+{len(power_nets)-10} more)' if len(power_nets) > 10 else ''
                print(f'[place-news] Excluding {len(power_nets)} power net(s) from HPWL: {names}{suffix}')

        self._compute_all()

    def _rebuild_bbox_arrays(self) -> None:
        """Populate flat bbox arrays and sorted xmin list from current footprint positions."""
        for i, fp in enumerate(self.model.footprints):
            x1, y1, x2, y2 = fp.bbox
            self._bx1[i] = x1
            self._by1[i] = y1
            self._bx2[i] = x2
            self._by2[i] = y2
        self._xmin_items = sorted((self._bx1[i], i)
                                  for i in range(len(self.model.footprints)))

    def _update_bbox(self, fi: int) -> None:
        """Update bbox arrays and sorted xmin list for a single footprint."""
        # Remove old xmin entry from sorted list
        old_x1 = self._bx1[fi]
        pos = bisect.bisect_left(self._xmin_items, (old_x1, fi))
        if pos < len(self._xmin_items) and self._xmin_items[pos] == (old_x1, fi):
            self._xmin_items.pop(pos)
        # Update flat arrays
        x1, y1, x2, y2 = self.model.footprints[fi].bbox
        self._bx1[fi] = x1
        self._by1[fi] = y1
        self._bx2[fi] = x2
        self._by2[fi] = y2
        # Insert new xmin entry
        bisect.insort(self._xmin_items, (x1, fi))

    def _compute_net_hpwl(self, net: Net) -> int:
        if net.is_excluded or len(net.pad_refs) < 2:
            return 0
        fps = self.model.footprints
        xmin = ymin = float('inf')
        xmax = ymax = float('-inf')
        for fi, pi in net.pad_refs:
            fp = fps[fi]
            pad = fp.pads[pi]
            # Inline abs_position using cached trig (avoids math.cos/sin per pad)
            cos_a = fp._cos_a
            sin_a = fp._sin_a
            ax = fp.x + int(pad.offset_x * cos_a - pad.offset_y * sin_a)
            ay = fp.y + int(pad.offset_x * sin_a + pad.offset_y * cos_a)
            if ax < xmin: xmin = ax
            if ax > xmax: xmax = ax
            if ay < ymin: ymin = ay
            if ay > ymax: ymax = ay
        return int(xmax - xmin) + int(ymax - ymin)

    def _compute_all_hpwl(self):
        self._total_hpwl = 0
        for nc, net in self.model.nets.items():
            h = self._compute_net_hpwl(net)
            self.net_hpwl[nc] = h
            self._total_hpwl += h

    def _same_group(self, i: int, j: int) -> bool:
        """Check if footprints i and j are in the same component group."""
        gi = self.model.fp_to_group.get(i)
        return gi is not None and gi == self.model.fp_to_group.get(j)

    def _compute_pair_overlap(self, i: int, j: int) -> float:
        """Compute overlap penalty for a single pair (i, j).

        Skips pairs in the same component group — the designer placed
        them intentionally and the group moves as a rigid body.
        """
        if self._same_group(i, j):
            return 0.0
        fps = self.model.footprints
        x1min, y1min, x1max, y1max = fps[i].bbox
        x2min, y2min, x2max, y2max = fps[j].bbox
        ox = max(0, min(x1max, x2max) - max(x1min, x2min))
        oy = max(0, min(y1max, y2max) - max(y1min, y2min))
        if ox > 0 and oy > 0:
            return (ox + oy) * OVERLAP_WEIGHT
        return 0.0

    def _compute_overlap_penalty(self) -> float:
        """O(n^2) bounding-box overlap check. Populates per-pair cache."""
        fps = self.model.footprints
        n = len(fps)
        self._pair_overlaps.clear()
        total = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                v = self._compute_pair_overlap(i, j)
                if v > 0:
                    self._pair_overlaps[(i, j)] = v
                    total += v
        return total

    # ------------------------------------------------------------------
    # Isolation (clearance / creepage between pads)
    # ------------------------------------------------------------------

    def _iso_pair_relevant(self, i: int, j: int) -> bool:
        """Pairs that placement can change: not the same rigid group, not two
        locked footprints."""
        fps = self.model.footprints
        if fps[i].locked and fps[j].locked:
            return False
        return not self._same_group(i, j)

    def _compute_pair_iso(self, i: int, j: int) -> float:
        if not self._iso_pair_relevant(i, j):
            return 0.0
        short = pair_shortfall(self._iso, self.model.footprints, i, j)
        return short * ISOLATION_WEIGHT if short > 0 else 0.0

    def _iso_near(self, i: int, j: int, reach: int) -> bool:
        return not (self._bx1[j] > self._bx2[i] + reach or self._bx2[j] < self._bx1[i] - reach
                    or self._by1[j] > self._by2[i] + reach or self._by2[j] < self._by1[i] - reach)

    def _compute_iso_penalty(self) -> float:
        self._iso_pairs.clear()
        if self._iso is None:
            return 0.0
        total = 0.0
        reach = self._iso.fp_reach
        lst = self._iso_fps
        for a in range(len(lst)):
            i = lst[a]
            for b in range(a + 1, len(lst)):
                j = lst[b]
                r = max(reach[i], reach[j])
                if not self._iso_near(i, j, r):
                    continue
                v = self._compute_pair_iso(i, j)
                if v > 0:
                    self._iso_pairs[(i, j)] = v
                    total += v
        return total

    # ------------------------------------------------------------------
    # Boundary, keep-outs, placement areas (per footprint)
    # ------------------------------------------------------------------

    def _compute_fp_boundary(self, fp_idx: int) -> float:
        """Compute boundary penalty for a single footprint.

        Penalty scales with the footprint's net count so that
        high-pin-count components get proportionally stronger boundary
        penalties, matching their larger HPWL pull.

        Uses cached bbox arrays (_bx1/_by1/_bx2/_by2), precomputed net scale,
        and cached board extents to avoid redundant attribute lookups.
        """
        if self.model.footprints[fp_idx].locked:
            return 0.0
        net_scale = self._fp_net_scale[fp_idx]
        xmin = self._bx1[fp_idx]
        ymin = self._by1[fp_idx]
        xmax = self._bx2[fp_idx]
        ymax = self._by2[fp_idx]
        dx = max(0, self._oxmin - xmin) + max(0, xmax - self._oxmax)
        dy = max(0, self._oymin - ymin) + max(0, ymax - self._oymax)
        total = (dx + dy) * BOUNDARY_WEIGHT * net_scale
        poly = self._outline_polygon
        if poly is not None and dx == 0 and dy == 0:
            for cx, cy in ((xmin, ymin), (xmax, ymin),
                           (xmax, ymax), (xmin, ymax)):
                if not point_in_polygon(cx, cy, poly):
                    d = dist_to_polygon(cx, cy, poly)
                    total += d * POLYGON_BOUNDARY_WEIGHT * net_scale
        return total

    def _compute_boundary_penalty(self) -> float:
        """Compute boundary penalties for all footprints. Populates per-fp cache."""
        self._fp_boundary.clear()
        total = 0.0
        for i, fp in enumerate(self.model.footprints):
            v = self._compute_fp_boundary(i)
            if v > 0:
                self._fp_boundary[i] = v
                total += v
        return total

    def _compute_fp_keepout(self, fp_idx: int) -> float:
        """Compute keepout penalty for a single footprint.

        Only keep-outs that forbid footprints on this footprint's board side
        count, and a non-rectangular keep-out only when the box really touches
        its outline. Penalty scales with the footprint's net count.
        """
        fp = self.model.footprints[fp_idx]
        if fp.locked or not self._keepouts:
            return 0.0
        net_scale = self._fp_net_scale[fp_idx]
        fxmin = self._bx1[fp_idx]
        fymin = self._by1[fp_idx]
        fxmax = self._bx2[fp_idx]
        fymax = self._by2[fp_idx]
        total = 0.0
        for ko in self._keepouts:
            ox, oy = keepout_overlap(ko, fp.side, fxmin, fymin, fxmax, fymax, fp.copper_sides)
            if ox > 0 and oy > 0:
                total += (ox + oy) * KEEPOUT_WEIGHT * net_scale
        return total

    def _compute_keepout_penalty(self) -> float:
        """Compute keepout penalties for all footprints. Populates per-fp cache."""
        self._fp_keepout.clear()
        total = 0.0
        for i, fp in enumerate(self.model.footprints):
            v = self._compute_fp_keepout(i)
            if v > 0:
                self._fp_keepout[i] = v
                total += v
        return total

    def _compute_fp_area(self, fp_idx: int) -> float:
        """Members must stay inside their placement areas; with exclusive
        areas, other footprints are kept out of them."""
        if not self._areas or self.model.footprints[fp_idx].locked:
            return 0.0
        net_scale = self._fp_net_scale[fp_idx]
        x1 = self._bx1[fp_idx]
        y1 = self._by1[fp_idx]
        x2 = self._bx2[fp_idx]
        y2 = self._by2[fp_idx]
        mine = self._fp_areas.get(fp_idx, ())
        total = 0.0
        for ai, area in enumerate(self._areas):
            if ai in mine:
                out = area_outside(area, x1, y1, x2, y2)
                if out > 0:
                    total += out * AREA_WEIGHT * net_scale
            elif area.exclusive:
                d = area_intrusion(area, x1, y1, x2, y2)
                if d > 0:
                    total += d * KEEPOUT_WEIGHT * net_scale
        return total

    def _compute_area_penalty(self) -> float:
        self._fp_area.clear()
        total = 0.0
        if not self._areas:
            return 0.0
        for i in range(len(self.model.footprints)):
            v = self._compute_fp_area(i)
            if v > 0:
                self._fp_area[i] = v
                total += v
        return total

    def _compute_all(self):
        self._rebuild_bbox_arrays()
        self._compute_all_hpwl()
        self._overlap_penalty = self._compute_overlap_penalty()
        self._iso_penalty = self._compute_iso_penalty()
        self._boundary_penalty = self._compute_boundary_penalty()
        self._keepout_penalty = self._compute_keepout_penalty()
        self._area_penalty = self._compute_area_penalty()

    def update_penalty_scale(self, scale: float) -> None:
        """Set the penalty scale for boundary, keepout and area penalties.

        Called once per temperature step. Overlap and isolation are always
        at full weight.
        """
        self.penalty_scale = scale

    @property
    def total_cost(self) -> float:
        """Cost with current penalty_scale applied to boundary+keepout+area."""
        return (float(self._total_hpwl) + self._overlap_penalty + self._iso_penalty
                + (self._boundary_penalty + self._keepout_penalty + self._area_penalty)
                * self.penalty_scale)

    @property
    def normalized_cost(self) -> float:
        """Cost with all penalties at full weight (penalty_scale=1.0).

        Used for best-solution tracking so solutions are compared fairly
        regardless of when they were found during the anneal.
        """
        return (float(self._total_hpwl) + self._overlap_penalty + self._iso_penalty
                + self._boundary_penalty + self._keepout_penalty + self._area_penalty)

    @property
    def hpwl(self) -> int:
        return self._total_hpwl

    def snapshot(self, moved_fp_indices: Set[int]) -> dict:
        """Save cost state for affected footprints, so it can be restored
        cheaply on move rejection instead of recomputing.

        Saves all existing overlapping / isolation pairs involving moved
        footprints, plus bbox array values for moved footprints.
        """
        _ts = time.perf_counter()
        fps = self.model.footprints

        # Affected nets
        affected_nets: Set[int] = set()
        for fi in moved_fp_indices:
            affected_nets.update(fps[fi].net_codes)

        # Save existing overlap pairs involving moved footprints
        saved_pairs: Dict[Tuple[int, int], float] = {}
        for k, v in self._pair_overlaps.items():
            if k[0] in moved_fp_indices or k[1] in moved_fp_indices:
                saved_pairs[k] = v
        saved_iso: Dict[Tuple[int, int], float] = {}
        for k, v in self._iso_pairs.items():
            if k[0] in moved_fp_indices or k[1] in moved_fp_indices:
                saved_iso[k] = v

        # Save bbox values for moved footprints
        saved_bbox = {}
        for fi in moved_fp_indices:
            saved_bbox[fi] = (self._bx1[fi], self._by1[fi],
                              self._bx2[fi], self._by2[fi])

        self.t_snapshot += time.perf_counter() - _ts
        return {
            'hpwl': self._total_hpwl,
            'overlap': self._overlap_penalty,
            'iso': self._iso_penalty,
            'boundary': self._boundary_penalty,
            'keepout': self._keepout_penalty,
            'area': self._area_penalty,
            'net_hpwl': {nc: self.net_hpwl.get(nc, 0) for nc in affected_nets},
            'pair_overlaps': saved_pairs,
            'iso_pairs': saved_iso,
            'fp_boundary': {fi: self._fp_boundary.get(fi, 0.0) for fi in moved_fp_indices},
            'fp_keepout': {fi: self._fp_keepout.get(fi, 0.0) for fi in moved_fp_indices},
            'fp_area': {fi: self._fp_area.get(fi, 0.0) for fi in moved_fp_indices},
            'saved_bbox': saved_bbox,
            'moved_fp_indices': moved_fp_indices,
        }

    def restore(self, snap: dict) -> None:
        """Restore cost state from a snapshot. O(k) — no recomputation.

        Purges all pair entries involving moved footprints,
        then restores saved pairs and bbox values.
        """
        self._total_hpwl = snap['hpwl']
        self._overlap_penalty = snap['overlap']
        self._iso_penalty = snap.get('iso', 0.0)
        self._boundary_penalty = snap['boundary']
        self._keepout_penalty = snap['keepout']
        self._area_penalty = snap.get('area', 0.0)
        for nc, h in snap['net_hpwl'].items():
            self.net_hpwl[nc] = h

        # Purge ALL pairs involving moved fps, then restore saved ones
        moved = snap.get('moved_fp_indices', set())
        for cache, saved in ((self._pair_overlaps, snap['pair_overlaps']),
                             (self._iso_pairs, snap.get('iso_pairs', {}))):
            keys_to_remove = [k for k in cache if k[0] in moved or k[1] in moved]
            for k in keys_to_remove:
                del cache[k]
            for k, v in saved.items():
                if v > 0:
                    cache[k] = v

        for cache, saved in ((self._fp_boundary, snap['fp_boundary']),
                             (self._fp_keepout, snap['fp_keepout']),
                             (self._fp_area, snap.get('fp_area', {}))):
            for fi, v in saved.items():
                if v > 0:
                    cache[fi] = v
                else:
                    cache.pop(fi, None)

        # Restore bbox arrays and sorted xmin list (footprint positions already reverted)
        for fi, bbox in snap.get('saved_bbox', {}).items():
            # Remove current (post-move) xmin from sorted list
            cur_x1 = self._bx1[fi]
            pos = bisect.bisect_left(self._xmin_items, (cur_x1, fi))
            if pos < len(self._xmin_items) and self._xmin_items[pos] == (cur_x1, fi):
                self._xmin_items.pop(pos)
            # Restore flat arrays
            self._bx1[fi] = bbox[0]
            self._by1[fi] = bbox[1]
            self._bx2[fi] = bbox[2]
            self._by2[fi] = bbox[3]
            # Insort restored xmin
            bisect.insort(self._xmin_items, (bbox[0], fi))

    def incremental_update(self, moved_fp_indices: Set[int]) -> float:
        """
        Recompute cost after footprints moved.

        HPWL: only recomputes nets connected to moved footprints.
        Overlap / isolation: flat bbox scan — O(n) per moved fp.
        Boundary/keepout/area: only recomputes moved footprints — O(k).
        """
        fps = self.model.footprints

        # --- HPWL (incremental, nets only; power nets excluded) ---
        _t0 = time.perf_counter()
        affected_nets: Set[int] = set()
        for fi in moved_fp_indices:
            affected_nets.update(fps[fi].net_codes)
        for nc in affected_nets:
            net = self.model.nets[nc]
            if net.is_excluded:
                continue
            old_h = self.net_hpwl.get(nc, 0)
            new_h = self._compute_net_hpwl(net)
            self._total_hpwl += (new_h - old_h)
            self.net_hpwl[nc] = new_h
        _t1 = time.perf_counter()
        self.t_hpwl += _t1 - _t0

        # --- Overlap (incremental, flat bbox scan) ---
        # Update bbox arrays for moved footprints
        for fi in moved_fp_indices:
            self._update_bbox(fi)

        # Collect all pairs to check using sorted xmin list for O(log n + k) scan:
        # bisect finds all fps with xmin < mi_x2 (candidate left edges), then
        # check remaining 3 conditions on that reduced candidate set.
        pairs_to_update: Set[Tuple[int, int]] = set()
        bx1 = self._bx1; by1 = self._by1
        bx2 = self._bx2; by2 = self._by2
        xmin_items = self._xmin_items
        for mi in moved_fp_indices:
            mi_x1 = bx1[mi]; mi_y1 = by1[mi]
            mi_x2 = bx2[mi]; mi_y2 = by2[mi]
            # All fps with xmin < mi_x2 are candidates (left edge left of query right edge)
            i1 = bisect.bisect_left(xmin_items, (mi_x2, -1))
            for k in range(i1):
                j = xmin_items[k][1]
                if j != mi and j not in moved_fp_indices:
                    if bx2[j] > mi_x1 and by1[j] < mi_y2 and by2[j] > mi_y1:
                        pairs_to_update.add(_pair_key(mi, j))
            # Also add existing overlapping pairs (might have moved apart)
            # — these may not pass the new bbox check above
        for k in list(self._pair_overlaps):
            if k[0] in moved_fp_indices or k[1] in moved_fp_indices:
                pairs_to_update.add(k)

        # Between moved fps themselves
        moved_list = list(moved_fp_indices)
        for idx_a in range(len(moved_list)):
            for idx_b in range(idx_a + 1, len(moved_list)):
                pairs_to_update.add(_pair_key(moved_list[idx_a], moved_list[idx_b]))

        # Recompute affected pairs
        for key in pairs_to_update:
            old_v = self._pair_overlaps.pop(key, 0.0)
            self._overlap_penalty -= old_v
            new_v = self._compute_pair_overlap(key[0], key[1])
            if new_v > 0:
                self._pair_overlaps[key] = new_v
            self._overlap_penalty += new_v
        _t2 = time.perf_counter()
        self.t_overlap += _t2 - _t1

        # --- Isolation (incremental: pairs involving moved fps) ---
        if self._iso is not None:
            iso_pairs: Set[Tuple[int, int]] = set()
            reach = self._iso.fp_reach
            for mi in moved_fp_indices:
                rm = reach.get(mi)
                if rm is None:
                    continue
                for j in self._iso_fps:
                    if j == mi:
                        continue
                    if self._iso_near(mi, j, max(rm, reach[j])):
                        iso_pairs.add(_pair_key(mi, j))
            for k in self._iso_pairs:
                if k[0] in moved_fp_indices or k[1] in moved_fp_indices:
                    iso_pairs.add(k)
            for key in iso_pairs:
                old_v = self._iso_pairs.pop(key, 0.0)
                self._iso_penalty -= old_v
                new_v = self._compute_pair_iso(key[0], key[1])
                if new_v > 0:
                    self._iso_pairs[key] = new_v
                self._iso_penalty += new_v
        _t3 = time.perf_counter()
        self.t_iso += _t3 - _t2

        # --- Boundary (incremental, moved fps only) ---
        for fi in moved_fp_indices:
            old_v = self._fp_boundary.pop(fi, 0.0)
            self._boundary_penalty -= old_v
            new_v = self._compute_fp_boundary(fi)
            if new_v > 0:
                self._fp_boundary[fi] = new_v
            self._boundary_penalty += new_v
        _t4 = time.perf_counter()
        self.t_boundary += _t4 - _t3

        # --- Keepout + placement areas (incremental, moved fps only) ---
        for fi in moved_fp_indices:
            old_v = self._fp_keepout.pop(fi, 0.0)
            self._keepout_penalty -= old_v
            new_v = self._compute_fp_keepout(fi)
            if new_v > 0:
                self._fp_keepout[fi] = new_v
            self._keepout_penalty += new_v
            if self._areas:
                old_a = self._fp_area.pop(fi, 0.0)
                self._area_penalty -= old_a
                new_a = self._compute_fp_area(fi)
                if new_a > 0:
                    self._fp_area[fi] = new_a
                self._area_penalty += new_a
        self.t_keepout += time.perf_counter() - _t4

        return self.total_cost

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def area_violations(self) -> List[Tuple[int, str]]:
        """(footprint index, area name) for members outside / intruders inside."""
        out: List[Tuple[int, str]] = []
        for i in range(len(self.model.footprints)):
            if self.model.footprints[i].locked:
                continue
            x1, y1, x2, y2 = self._bx1[i], self._by1[i], self._bx2[i], self._by2[i]
            mine = self._fp_areas.get(i, ())
            for ai, area in enumerate(self._areas):
                if ai in mine:
                    if area_outside(area, x1, y1, x2, y2) > 0:
                        out.append((i, area.name))
                elif area.exclusive and area_intrusion(area, x1, y1, x2, y2) > 0:
                    out.append((i, area.name))
        return out
