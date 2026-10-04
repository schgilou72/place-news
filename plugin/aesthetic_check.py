"""Aesthetic check of a board: placement and routing criteria, a score out
of 100 and a list of located issues.

The criteria are measured, not guessed:

placement
  alignment     same-type parts that are almost (but not exactly) in line
  orientation   parts of one footprint type turned different ways
  ref_readable  references not horizontal / vertical bottom-to-top
  ref_position  references neither on their part's centre nor on its axis,
                or far from it
  ref_clear     references on a pad or on another reference
routing
  angles        track segments not in 45° steps
  acute         acute angles between two segments of a net
  jogs          small sideways steps between two parallel segments
  detours       nets much longer than the shortest 45° tree of their pads
  pad_exits     tracks leaving a rectangular SMD pad askew
cost (fabrication / assembly)
  vias          vias per routed net
  crossings     ratsnest crossings between nets (each tends to cost a via)
  drill_sizes   different hole diameters (tool changes)
  small_drills  holes under 0.3 mm (advanced capability)
  fine_tracks   tracks under 0.15 mm (advanced capability)
  two_sides     parts on both sides (second assembly pass)
  inner_layers  inner copper layers left empty (layers cost)
  layer_count   more (or fewer) copper layers than the routing room calls for
  board_area    board much larger than the parts need

Routing room (layer count), the rule of thumb
  free routing area per net = (board area x layers - parts area) / nets
compared with what an average net needs: its ratsnest length (45° spanning
tree, x 1.3 for detours) times its track pitch (net-class width +
clearance), over the share of free area tracks can really use (60 %).
Inner layers used as planes are not routing room. Calibrated on the KiCad
demo boards: the estimated track area matches the routed one within ~10 %,
and the densest two-layer demo (StickHub, routed by hand) sits right at
room = need. Fewer layers are suggested only with 30 % of room to spare.

The classic "pin density" table (board area in square inches per 14-pin
equivalent IC: > 1.0 -> 2 signal layers, 0.6-1.0 -> 2 (+ 2 planes),
0.4-0.6 -> 4, 0.3-0.4 -> 6, 0.2-0.3 -> 8, < 0.2 -> 10) is shown too. It
dates from through-hole boards: right for them, far too many layers for
fine-pitch SMD boards (StickHub: < 0.2, routed on 2 layers).

Each criterion gives a value f (0 = perfect) and a quality q = exp(-f/s).
The score is the weighted mean of the qualities (weights: what the user
cares about, learned by aesthetic_learning.py). Pure Python; only
extract_geometry() needs pcbnew.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Box = Tuple[int, int, int, int]
Pt = Tuple[int, int]
MM = 1_000_000

# criterion -> (group, label, scale of f)
CRITERIA: Dict[str, Tuple[str, str, float]] = {
    'alignment':    ('placement', 'Same-type parts aligned', 0.25),
    'orientation':  ('placement', 'One orientation per part type', 0.20),
    'ref_readable': ('placement', 'References readable (0° / 90°)', 0.05),
    'ref_position': ('placement', 'References on their part or its axis', 0.15),
    'ref_clear':    ('placement', 'References clear of pads and of each other', 0.05),
    'angles':       ('routing', 'Track angles in 45° steps', 0.05),
    'acute':        ('routing', 'No acute angles', 0.05),
    'jogs':         ('routing', 'No small zigzags', 0.08),
    'detours':      ('routing', 'No long detours', 0.30),
    'pad_exits':    ('routing', 'Tracks leave pads straight', 0.15),
    'vias':         ('cost', 'Few vias', 1.00),
    'crossings':    ('cost', 'Untangled ratsnest (fewer vias)', 0.25),
    'drill_sizes':  ('cost', 'Few different drill sizes', 0.50),
    'small_drills': ('cost', 'No drills under 0.3 mm', 0.10),
    'fine_tracks':  ('cost', 'No tracks under 0.15 mm', 0.10),
    'two_sides':    ('cost', 'Parts on one side only', 0.15),
    'inner_layers': ('cost', 'No empty inner copper layers', 0.30),
    'layer_count':  ('cost', 'Layer count fits the routing room', 0.30),
    'board_area':   ('cost', 'Board not larger than needed', 0.60),
}

GROUPS = ('placement', 'routing', 'cost')
SMALL_DRILL = 300_000       # 0.3 mm
FINE_TRACK = 150_000        # 0.15 mm

ROUTE_DETOUR = 1.3          # routed length / 45° spanning tree of the pads, typical
ROUTE_FILL = 0.60           # share of the free area tracks can really use
LAYER_MARGIN = 1.3          # fewer layers are suggested only with 30 % room to spare
DEFAULT_PITCH = 450_000     # 0.25 mm track + 0.2 mm clearance
LAYER_STEPS = (1, 2, 4, 6, 8, 10, 12, 14, 16)
POUR_SHARE = 0.20           # a zone over 20 % of the board is a pour / plane

ALIGN_TOL = 10_000          # 10 µm: aligned
REF_AXIS_TOL = 100_000      # 0.1 mm: reference on the part's centre line
REF_FAR = 2 * MM            # reference more than 2 mm away from its part
ANGLE_TOL = 0.5             # degrees


@dataclass
class PartGeom:
    ref: str
    uuid: str
    type_name: str
    angle: float                 # footprint orientation (degrees)
    box: Box                     # body (courtyard) box
    locked: bool = False
    side: str = 'F'
    ref_visible: bool = True
    ref_box: Optional[Box] = None
    ref_angle: float = 0.0       # drawn angle of the reference text

    @property
    def center(self) -> Pt:
        return (self.box[0] + self.box[2]) // 2, (self.box[1] + self.box[3]) // 2

    @property
    def ref_center(self) -> Optional[Pt]:
        if self.ref_box is None:
            return None
        return (self.ref_box[0] + self.ref_box[2]) // 2, (self.ref_box[1] + self.ref_box[3]) // 2


@dataclass
class PadGeom:
    part: int                    # index in parts (-1: none)
    net: str
    center: Pt
    box: Box                     # copper bounding box
    angle: float = 0.0           # pad orientation (degrees)
    smd: bool = True
    sides: Tuple[str, ...] = ('F',)
    round: bool = False
    name: str = ''
    hole: bool = False           # non-plated hole (no copper)


@dataclass
class SegGeom:
    net: str
    layer: str
    a: Pt
    b: Pt
    width: int
    arc: bool = False            # chord of an arc (rounded corner): no angle checks

    @property
    def length(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])


@dataclass
class ViaGeom:
    net: str
    center: Pt
    diameter: int


@dataclass
class BoardGeom:
    parts: List[PartGeom] = field(default_factory=list)
    pads: List[PadGeom] = field(default_factory=list)
    segs: List[SegGeom] = field(default_factory=list)
    vias: List[ViaGeom] = field(default_factory=list)
    outline: Box = (0, 0, 100 * MM, 100 * MM)
    holes: List[Tuple[Pt, int]] = field(default_factory=list)     # (centre, drill diameter)
    copper_layers: Tuple[str, ...] = ('F.Cu', 'B.Cu')
    zone_layers: Tuple[str, ...] = ()       # copper layers holding a pour / plane
    plane_nets: Tuple[str, ...] = ()        # nets left out of the ratsnest (planes, power)
    pour_nets: Tuple[str, ...] = ()         # nets with a large copper zone (pour / plane)
    plane_layers: Tuple[str, ...] = ()      # inner layers used as planes (large zone, few tracks)
    net_pitch: Dict[str, int] = field(default_factory=dict)   # track width + clearance
    fills: List[Tuple[str, str, List[List[Pt]]]] = field(default_factory=list)  # zone fills
    outline_area: Optional[float] = None    # board area (nm²), None: outline box


@dataclass
class Issue:
    criterion: str
    x: int
    y: int
    message: str


# classic table: (pin density above, signal layers, total layers)
EIC_TABLE = ((1.0, 2, 2), (0.6, 2, 4), (0.4, 4, 6), (0.3, 6, 8), (0.2, 8, 12), (0.0, 10, 14))
SQ_INCH = 645.16            # mm²


def eic_density(board_area_mm2: float, pins: int) -> Optional[float]:
    """Classic pin density: board area (in²) / (pins / 14)."""
    if pins <= 0:
        return None
    return (board_area_mm2 / SQ_INCH) / (pins / 14.0)


def eic_layers(density: float) -> Tuple[int, int]:
    """(signal layers, total layers) of the classic pin-density table."""
    for above, signal, total in EIC_TABLE:
        if density > above:
            return signal, total
    return EIC_TABLE[-1][1], EIC_TABLE[-1][2]


@dataclass
class GridPart:
    """A part with pads in a grid (BGA, LGA...): its escape routing."""
    ref: str
    rings: int                   # signal-pad rings from the outside in
    pitch: int
    tracks_per_channel: int
    layers: int                  # signal layers the escape needs


def _grid_escape(geom: 'BoardGeom') -> List[GridPart]:
    """Parts whose pads form a grid at least 3 deep: each signal layer
    escapes the rings reachable through the channels between pads (t tracks
    per channel -> t + 1 rings per layer)."""
    by_part: Dict[int, List['PadGeom']] = {}
    for pd in geom.pads:
        if pd.part >= 0 and not pd.hole:
            by_part.setdefault(pd.part, []).append(pd)
    planes = set(geom.plane_nets)
    out: List[GridPart] = []
    for idx, pads in by_part.items():
        if len(pads) < 9:
            continue
        part = geom.parts[idx]
        cx, cy = part.center
        a = math.radians(part.angle)
        ca, sa = math.cos(a), math.sin(a)
        local = []
        for pd in pads:
            dx, dy = pd.center[0] - cx, pd.center[1] - cy
            local.append((dx * ca - dy * sa, dx * sa + dy * ca, pd))

        def levels(vals: List[float]) -> List[float]:
            vals = sorted(vals)
            out_l = [vals[0]]
            for v in vals[1:]:
                if v - out_l[-1] > 50_000:
                    out_l.append(v)
            return out_l
        xs = levels([p[0] for p in local])
        ys = levels([p[1] for p in local])
        if len(xs) < 5 or len(ys) < 5:
            continue

        def index(levels_: List[float], v: float) -> int:
            return min(range(len(levels_)), key=lambda i: abs(levels_[i] - v))
        sizes = sorted(min(pd.box[2] - pd.box[0], pd.box[3] - pd.box[1]) for pd in pads)
        size = sizes[len(sizes) // 2]
        rings = 0
        inner = 0
        for x, y, pd in local:
            if not pd.net or pd.net in planes:
                continue
            if min(pd.box[2] - pd.box[0], pd.box[3] - pd.box[1]) > 2 * size:
                continue                  # exposed / thermal pad
            i, j = index(xs, x), index(ys, y)
            ring = min(i, j, len(xs) - 1 - i, len(ys) - 1 - j)
            rings = max(rings, ring + 1)
            inner += ring >= 1
        if rings < 3 or inner < 4:
            continue                      # a perimeter package (QFP, QFN, connectors)
        diffs = sorted(b - a_ for a_, b in zip(xs, xs[1:]))
        pitch = int(diffs[len(diffs) // 2])
        track = DEFAULT_PITCH
        nets = [pd.net for pd in pads if pd.net in geom.net_pitch]
        if nets:
            track = min(geom.net_pitch[n] for n in nets)
        # tracks per channel: n x (width + space) + space <= channel (space ~ 0.45 x pitch)
        t = max(0, int((pitch - size - 0.45 * track) // track))
        out.append(GridPart(ref=part.ref, rings=rings, pitch=pitch, tracks_per_channel=t,
                            layers=math.ceil(rings / (t + 1))))
    return out


@dataclass
class LayerEstimate:
    """Routing room against routing need (areas in mm²)."""
    layers: int                  # copper layers of the board
    nets: int                    # nets with two pads or more
    board_area: float
    parts_area: float
    need_per_net: float          # what an average net needs
    planar: bool                 # no ratsnest crossing at all
    plane_layers: int = 0        # inner layers used as planes (no routing room)
    pins: int = 0                # pads with a net (for the classic pin-density rule)
    grids: List[GridPart] = field(default_factory=list)   # BGA-like parts

    @property
    def escape_layers(self) -> int:
        return max((g.layers for g in self.grids), default=0)

    @property
    def eic_density(self) -> Optional[float]:
        return eic_density(self.board_area, self.pins)

    def quick_rules(self) -> List[str]:
        """Quick rules of thumb, shown for comparison only: on KiCad's demo
        boards they ask for too many layers on fine-pitch SMD boards."""
        lines = []
        eic = self.describe_eic()
        if eic:
            lines.append(eic)
        share = self.parts_area / max(1e-9, self.board_area)
        pins_cm2 = self.pins / max(1e-9, self.board_area / 100.0)
        lines.append(f'Parts cover {100 * share:.0f} % of the board'
                     + (' (over 70 %: "high density, 6 to 8 layers" by the quick rule)'
                        if share > 0.7 else '')
                     + f'; {pins_cm2:.1f} pins per cm²'
                     + (' (over 20: "4 layers or more" by the quick rule)' if pins_cm2 > 20 else '')
                     + '.')
        return lines

    def describe_eic(self) -> str:
        d = self.eic_density
        if d is None:
            return ''
        signal, total = eic_layers(d)
        more = '+' if total == EIC_TABLE[-1][2] else ''
        return (f'Classic pin-density rule: {self.board_area / SQ_INCH:.1f} in² / ({self.pins} pins / 14) '
                f'= {d:.2f} -> {signal} signal layers ({total}{more} in all); it dates from '
                f'through-hole parts and asks too many layers for fine-pitch SMD boards.')

    @property
    def signal_layers(self) -> int:
        return max(1, self.layers - self.plane_layers)

    def area_per_net(self, layers: Optional[int] = None) -> float:
        """(board area x layers - parts area) / nets"""
        n = self.signal_layers if layers is None else layers
        return (self.board_area * n - self.parts_area) / max(1, self.nets)

    def _first(self, margin: float, least: int) -> int:
        least = max(least, self.escape_layers)
        for n in LAYER_STEPS:
            if n < least or (n == 1 and not self.planar):
                continue
            if self.area_per_net(n) >= margin * self.need_per_net:
                return n
        return LAYER_STEPS[-1]

    @property
    def needed_layers(self) -> int:
        """Fewest routing layers with enough room (1 only for a planar ratsnest)."""
        return self._first(1.0, 1)

    @property
    def suggested_layers(self) -> int:
        """Routing layers to aim for: at least 2, with room to spare."""
        return max(self.needed_layers, self._first(LAYER_MARGIN, 2))

    @property
    def ratio(self) -> float:
        """Room / need with the board's routing layers (1 = just enough)."""
        return self.area_per_net() / max(1e-9, self.need_per_net)

    def verdict(self) -> str:
        have, need, aim = self.signal_layers, self.needed_layers, self.suggested_layers
        if have < need:
            return f'short: {need} routing layers needed (or a larger board)'
        if have < aim:
            return f'tight: {have} routing layers just fit, {aim} would be comfortable'
        if have > aim:
            return f'{aim} routing layers should do'
        single = ' (even one: the ratsnest has no crossing)' if need == 1 else ''
        return f'{have} routing layers fit{single}'

    def describe(self) -> str:
        planes = (f' (+ {self.plane_layers} plane layer{"s" if self.plane_layers > 1 else ""})'
                  if self.plane_layers else '')
        text = (f'Routing room: ({self.board_area:.0f} mm² x {self.signal_layers} routing '
                f'layer{"s" if self.signal_layers > 1 else ""} - {self.parts_area:.0f} mm² of parts) '
                f'/ {self.nets} nets = {self.area_per_net():.0f} mm² per net{planes}; an average '
                f'net needs about {self.need_per_net:.0f} mm²')
        if self.grids:
            g = max(self.grids, key=lambda g: g.layers)
            text += (f'; {g.ref}: {g.rings} rings of pads at {g.pitch / MM:.2f} mm pitch, '
                     f'{g.tracks_per_channel} track(s) between pads -> {g.layers} layer(s) to escape')
        return text + f' -> {self.verdict()}.'


@dataclass
class CheckResult:
    features: Dict[str, Optional[float]]
    qualities: Dict[str, Optional[float]]
    weights: Dict[str, float]
    issues: List[Issue]
    layers: Optional[LayerEstimate] = None

    @property
    def score(self) -> Optional[float]:
        return weighted_score(self.qualities, self.weights)

    def group_score(self, group: str) -> Optional[float]:
        q = {k: v for k, v in self.qualities.items() if CRITERIA[k][0] == group}
        return weighted_score(q, self.weights)


def quality(criterion: str, f: Optional[float]) -> Optional[float]:
    if f is None:
        return None
    return math.exp(-max(0.0, f) / CRITERIA[criterion][2])


def weighted_score(qualities: Dict[str, Optional[float]], weights: Dict[str, float]) -> Optional[float]:
    num = den = 0.0
    for k, q in qualities.items():
        if q is None:
            continue
        w = max(0.0, weights.get(k, 1.0))
        num += w * q
        den += w
    return 100.0 * num / den if den > 0 else None


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _overlap(a: Box, b: Box, margin: int = 0) -> bool:
    return (a[0] < b[2] + margin and a[2] > b[0] - margin and
            a[1] < b[3] + margin and a[3] > b[1] - margin)


def _box_gap(a: Box, b: Box) -> float:
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _mm(v: float) -> str:
    return f'{v / MM:.2f} mm'


def _angle_of(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dy, dx))


def _off_45(angle: float) -> float:
    a = angle % 45.0
    return min(a, 45.0 - a)


def _octile(a: Pt, b: Pt) -> float:
    dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)


def _mst_length(points: Sequence[Pt]) -> float:
    """Length of the minimum spanning tree with 45° (octile) distances."""
    n = len(points)
    if n < 2:
        return 0.0
    in_tree = [False] * n
    dist = [math.inf] * n
    dist[0] = 0.0
    total = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: dist[i])
        in_tree[u] = True
        total += dist[u]
        for v in range(n):
            if not in_tree[v]:
                d = _octile(points[u], points[v])
                if d < dist[v]:
                    dist[v] = d
    return total


# ---------------------------------------------------------------------------
# Placement criteria
# ---------------------------------------------------------------------------

def _type_groups(parts: Sequence[PartGeom], by_axis: bool) -> Dict[Tuple, List[int]]:
    groups: Dict[Tuple, List[int]] = {}
    for i, p in enumerate(parts):
        key = (p.type_name, int(round(p.angle)) % 180) if by_axis else (p.type_name,)
        groups.setdefault(key, []).append(i)
    return groups


def check_alignment(parts: Sequence[PartGeom]) -> Tuple[Optional[float], List[Issue]]:
    issues: List[Issue] = []
    close = near = 0
    for members in _type_groups(parts, by_axis=True).values():
        for ia in range(len(members)):
            a = parts[members[ia]]
            wa, ha = a.box[2] - a.box[0], a.box[3] - a.box[1]
            for ib in members[ia + 1:]:
                b = parts[ib]
                wb, hb = b.box[2] - b.box[0], b.box[3] - b.box[1]
                (ax, ay), (bx, by) = a.center, b.center
                for along, across, span, thick in ((abs(ax - bx), abs(ay - by), max(wa, wb), max(ha, hb)),
                                                   (abs(ay - by), abs(ax - bx), max(ha, hb), max(wa, wb))):
                    snap = max(MM // 2, int(0.6 * thick))
                    if along <= 3 * span + 3 * MM and across <= snap:
                        close += 1
                        if across > ALIGN_TOL:
                            near += 1
                            issues.append(Issue('alignment', (ax + bx) // 2, (ay + by) // 2,
                                                f'{a.ref} and {b.ref} are almost aligned '
                                                f'({_mm(across)} off)'))
                        break
    return (near / close if close else None), issues


def check_orientation(parts: Sequence[PartGeom]) -> Tuple[Optional[float], List[Issue]]:
    issues: List[Issue] = []
    total = minority = 0
    for (type_name,), members in _type_groups(parts, by_axis=False).items():
        if len(members) < 2:
            continue
        total += len(members)
        axes: Dict[int, List[int]] = {}
        for i in members:
            axes.setdefault(int(round(parts[i].angle)) % 180, []).append(i)
        if len(axes) < 2:
            continue
        major = max(axes, key=lambda a: (len(axes[a]), -a))
        for axis, idxs in axes.items():
            if axis == major:
                continue
            for i in idxs:
                minority += 1
                p = parts[i]
                issues.append(Issue('orientation', *p.center,
                                    f'{p.ref} is turned differently from the other {type_name} '
                                    f'({axis}° instead of {major}°)'))
    return (minority / total if total else None), issues


def _readable(angle: float) -> bool:
    a = angle % 360.0
    return any(abs(a - t) <= 1.0 for t in (0.0, 90.0, 360.0))


def check_references(parts: Sequence[PartGeom], pads: Sequence[PadGeom]
                     ) -> Dict[str, Tuple[Optional[float], List[Issue]]]:
    refs = [p for p in parts if p.ref_visible and p.ref_box is not None]
    out: Dict[str, Tuple[Optional[float], List[Issue]]] = {}
    if not refs:
        return {k: (None, []) for k in ('ref_readable', 'ref_position', 'ref_clear')}
    unreadable: List[Issue] = []
    position: List[Issue] = []
    clear: List[Issue] = []
    clear_refs = set()
    for p in refs:
        rc = p.ref_center
        if not _readable(p.ref_angle):
            unreadable.append(Issue('ref_readable', rc[0], rc[1],
                                    f'{p.ref}: reference at {p.ref_angle % 360:.0f}°'))
        gap = _box_gap(p.ref_box, p.box)
        dx, dy = abs(rc[0] - p.center[0]), abs(rc[1] - p.center[1])
        if gap > REF_FAR:
            position.append(Issue('ref_position', rc[0], rc[1],
                                  f'{p.ref}: reference {_mm(gap)} away from its part'))
        elif dx > REF_AXIS_TOL and dy > REF_AXIS_TOL:
            position.append(Issue('ref_position', rc[0], rc[1],
                                  f'{p.ref}: reference off the part\'s centre lines'))
        for pad in pads:
            if p.side in pad.sides and _overlap(p.ref_box, pad.box, margin=50_000):
                clear.append(Issue('ref_clear', rc[0], rc[1], f'{p.ref}: reference touches a pad'))
                clear_refs.add(p.ref)
                break
    for ia in range(len(refs)):
        for b in refs[ia + 1:]:
            a = refs[ia]
            if a.side == b.side and _overlap(a.ref_box, b.ref_box):
                c = a.ref_center
                clear.append(Issue('ref_clear', c[0], c[1],
                                   f'references of {a.ref} and {b.ref} overlap'))
                clear_refs.update((a.ref, b.ref))
    n = len(refs)
    out['ref_readable'] = (len(unreadable) / n, unreadable)
    out['ref_position'] = (len(position) / n, position)
    out['ref_clear'] = (len(clear_refs) / n, clear)
    return out


# ---------------------------------------------------------------------------
# Routing criteria
# ---------------------------------------------------------------------------

def _key(p: Pt) -> Tuple[int, int]:
    return (int(round(p[0] / 1000.0)), int(round(p[1] / 1000.0)))   # 1 µm grid


def _endpoint_map(segs: Sequence[SegGeom]) -> Dict[Tuple, List[Tuple[int, int]]]:
    ends: Dict[Tuple, List[Tuple[int, int]]] = {}
    for i, s in enumerate(segs):
        ends.setdefault((s.net, s.layer) + _key(s.a), []).append((i, 0))
        ends.setdefault((s.net, s.layer) + _key(s.b), []).append((i, 1))
    return ends


def _inside_pad_or_via(p: Pt, pads: Sequence[PadGeom], vias: Sequence[ViaGeom]) -> bool:
    for pad in pads:
        if pad.box[0] <= p[0] <= pad.box[2] and pad.box[1] <= p[1] <= pad.box[3]:
            return True
    for v in vias:
        if math.hypot(p[0] - v.center[0], p[1] - v.center[1]) <= v.diameter / 2:
            return True
    return False


def _by_net(items) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for it in items:
        out.setdefault(it.net, []).append(it)
    return out


def check_routing(geom: BoardGeom) -> Dict[str, Tuple[Optional[float], List[Issue]]]:
    segs = [s for s in geom.segs if s.length > 0]
    out: Dict[str, Tuple[Optional[float], List[Issue]]] = {}
    if not segs:
        return {k: (None, []) for k in ('angles', 'acute', 'jogs', 'detours', 'vias', 'pad_exits')}

    pads_net = _by_net(geom.pads)
    vias_net = _by_net(geom.vias)
    segs_net = _by_net(segs)

    # angles (arc chords excluded: rounded corners are fine)
    total = off = 0.0
    angle_issues: List[Issue] = []
    for s in segs:
        if s.arc:
            continue
        ang = _angle_of(s.b[0] - s.a[0], s.b[1] - s.a[1])
        total += s.length
        if _off_45(ang) > ANGLE_TOL:
            off += s.length
            angle_issues.append(Issue('angles', (s.a[0] + s.b[0]) // 2, (s.a[1] + s.b[1]) // 2,
                                      f'{s.net}: track at {ang % 180:.0f}° (not a 45° step)'))
    out['angles'] = (off / total if total else None, angle_issues)

    # junctions between exactly two segments of a net, away from pads / vias
    ends = _endpoint_map(segs)
    junctions = []
    for key, lst in ends.items():
        if len(lst) != 2 or lst[0][0] == lst[1][0] or segs[lst[0][0]].arc or segs[lst[1][0]].arc:
            continue                  # (both ends of one tiny segment on the same point)
        p = (key[2] * 1000, key[3] * 1000)
        if _inside_pad_or_via(p, pads_net.get(key[0], ()), vias_net.get(key[0], ())):
            continue
        junctions.append((p, lst))

    def away(seg: SegGeom, end: int) -> Tuple[float, float]:
        if end == 0:
            return seg.b[0] - seg.a[0], seg.b[1] - seg.a[1]
        return seg.a[0] - seg.b[0], seg.a[1] - seg.b[1]

    acute_issues: List[Issue] = []
    for p, ((i, ei), (j, ej)) in junctions:
        v1, v2 = away(segs[i], ei), away(segs[j], ej)
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        cosv = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        theta = math.degrees(math.acos(cosv))
        if theta < 89.0:
            acute_issues.append(Issue('acute', p[0], p[1],
                                      f'{segs[i].net}: acute angle ({theta:.0f}°)'))
    out['acute'] = (len(acute_issues) / len(junctions) if junctions else 0.0, acute_issues)

    # jogs: a short segment between two parallel ones (a sideways step)
    junction_at = {}
    for p, lst in junctions:
        for i, e in lst:
            junction_at[(i, e)] = lst
    jog_issues: List[Issue] = []
    for i, s in enumerate(segs):
        if s.length >= max(2 * s.width, 300_000):
            continue
        la, lb = junction_at.get((i, 0)), junction_at.get((i, 1))
        if not la or not lb:
            continue
        prev = next((x for x in la if x[0] != i), None)
        nxt = next((x for x in lb if x[0] != i), None)
        if prev is None or nxt is None:
            continue
        dp = away(segs[prev[0]], prev[1])
        dn = away(segs[nxt[0]], nxt[1])
        dm = (s.b[0] - s.a[0], s.b[1] - s.a[1])

        def parallel(u, v) -> bool:
            return abs(u[0] * v[1] - u[1] * v[0]) <= 1e-3 * math.hypot(*u) * math.hypot(*v)
        # parallel neighbours, but the short piece itself goes sideways
        if parallel(dp, dn) and not parallel(dm, dp):
            jog_issues.append(Issue('jogs', (s.a[0] + s.b[0]) // 2, (s.a[1] + s.b[1]) // 2,
                                    f'{s.net}: small zigzag ({_mm(s.length)} step)'))
    out['jogs'] = (len(jog_issues) / len(segs), jog_issues)

    # detours: routed length vs shortest 45° tree of the net's pads
    by_net: Dict[str, float] = {}
    for s in segs:
        by_net[s.net] = by_net.get(s.net, 0.0) + s.length
    pads_by_net: Dict[str, List[Pt]] = {}
    for pad in geom.pads:
        if pad.net:
            pads_by_net.setdefault(pad.net, []).append(pad.center)
    excess = []
    detour_issues: List[Issue] = []
    for net, length in by_net.items():
        pts = pads_by_net.get(net, [])
        mst = _mst_length(pts)
        if mst < MM:
            continue
        ratio = length / mst
        excess.append(max(0.0, ratio - 1.3))
        if ratio > 1.6:
            cx = sum(p[0] for p in pts) // len(pts)
            cy = sum(p[1] for p in pts) // len(pts)
            detour_issues.append(Issue('detours', cx, cy,
                                       f'{net}: route {ratio:.1f}x longer than the shortest path'))
    out['detours'] = ((sum(excess) / len(excess)) if excess else None, detour_issues)

    # vias (a cost criterion, measured here with the routed nets)
    vias_by_net: Dict[str, List[ViaGeom]] = {}
    for v in geom.vias:
        vias_by_net.setdefault(v.net, []).append(v)
    via_issues = [Issue('vias', vs[0].center[0], vs[0].center[1], f'{net}: {len(vs)} vias')
                  for net, vs in vias_by_net.items() if len(vs) >= 3]
    out['vias'] = (len(geom.vias) / max(1, len(by_net)), via_issues)

    # pad exits
    exits = 0
    exit_issues: List[Issue] = []
    for pad in geom.pads:
        if not pad.smd or pad.round or not pad.net:
            continue
        for s in segs_net.get(pad.net, ()):
            if s.arc:
                continue
            for p, q in ((s.a, s.b), (s.b, s.a)):
                if not (pad.box[0] <= p[0] <= pad.box[2] and pad.box[1] <= p[1] <= pad.box[3]):
                    continue
                if pad.box[0] <= q[0] <= pad.box[2] and pad.box[1] <= q[1] <= pad.box[3]:
                    continue          # segment entirely inside the pad
                exits += 1
                rel = (_angle_of(q[0] - p[0], q[1] - p[1]) - pad.angle) % 90.0
                if min(rel, 90.0 - rel) > 1.0:
                    exit_issues.append(Issue('pad_exits', pad.center[0], pad.center[1],
                                             f'{pad.name or pad.net}: track leaves the pad askew'))
    out['pad_exits'] = (len(exit_issues) / exits if exits else None, exit_issues)
    return out


# ---------------------------------------------------------------------------
# Cost criteria
# ---------------------------------------------------------------------------

def _segments_cross(p1: Pt, p2: Pt, q1: Pt, q2: Pt) -> bool:
    """Proper crossing of two segments (touching ends do not count)."""
    def orient(a: Pt, b: Pt, c: Pt) -> int:
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 0) - (v < 0)
    if p1 in (q1, q2) or p2 in (q1, q2):
        return False
    o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
    o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
    return o1 * o2 < 0 and o3 * o4 < 0


def ratsnest_edges(pads: Sequence[Tuple[str, Pt]], skip_nets: Iterable[str] = ()
                   ) -> List[Tuple[str, Pt, Pt]]:
    """Minimum spanning tree of each net's pads (the ratsnest lines)."""
    skip = set(skip_nets)
    by_net: Dict[str, List[Pt]] = {}
    for net, c in pads:
        if net and net not in skip:
            by_net.setdefault(net, []).append(c)
    edges: List[Tuple[str, Pt, Pt]] = []
    for net, pts in by_net.items():
        pts = list(dict.fromkeys(pts))
        n = len(pts)
        if n < 2:
            continue
        in_tree = [False] * n
        best = [math.inf] * n
        link = [-1] * n
        best[0] = 0.0
        for _ in range(n):
            u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i])
            in_tree[u] = True
            if link[u] >= 0:
                edges.append((net, pts[link[u]], pts[u]))
            for v in range(n):
                if not in_tree[v]:
                    d = math.hypot(pts[u][0] - pts[v][0], pts[u][1] - pts[v][1])
                    if d < best[v]:
                        best[v], link[v] = d, u
    return edges


def count_crossings(edges: Sequence[Tuple[str, Pt, Pt]]) -> List[Pt]:
    """Points where ratsnest lines of different nets cross."""
    pts: List[Pt] = []
    boxes = [(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])) for _n, a, b in edges]
    for i in range(len(edges)):
        ni, a1, a2 = edges[i]
        bi = boxes[i]
        for j in range(i + 1, len(edges)):
            nj, b1, b2 = edges[j]
            if ni == nj:
                continue
            bj = boxes[j]
            if bi[2] < bj[0] or bj[2] < bi[0] or bi[3] < bj[1] or bj[3] < bi[1]:
                continue
            if _segments_cross(a1, a2, b1, b2):
                pts.append(((a1[0] + a2[0] + b1[0] + b2[0]) // 4, (a1[1] + a2[1] + b1[1] + b2[1]) // 4))
    return pts


def estimate_layers(geom: BoardGeom, crossings: Optional[int] = None) -> Optional[LayerEstimate]:
    """How many copper layers the routing needs: free routing area per net
    against the area an average net needs."""
    by_net: Dict[str, List[Pt]] = {}
    for p in geom.pads:
        if p.net and not p.hole:
            by_net.setdefault(p.net, []).append(p.center)
    nets = {n: list(dict.fromkeys(pts)) for n, pts in by_net.items()}
    nets = {n: pts for n, pts in nets.items() if len(pts) >= 2}
    if not nets:
        return None
    o = geom.outline
    board_area = geom.outline_area or float(o[2] - o[0]) * float(o[3] - o[1])
    parts_area = sum(float(p.box[2] - p.box[0]) * float(p.box[3] - p.box[1]) for p in geom.parts)
    pours = set(geom.pour_nets)
    demand = 0.0
    for n, pts in nets.items():
        if n in pours:
            continue                      # the pour connects it
        demand += _mst_length(pts) * ROUTE_DETOUR * geom.net_pitch.get(n, DEFAULT_PITCH)
    mm2 = float(MM) * MM
    if crossings is None:
        crossings = len(count_crossings(ratsnest_edges(
            [(p.net, p.center) for p in geom.pads if not p.hole], geom.plane_nets)))
    return LayerEstimate(layers=max(1, len(geom.copper_layers)), nets=len(nets),
                         board_area=board_area / mm2, parts_area=parts_area / mm2,
                         need_per_net=demand / ROUTE_FILL / len(nets) / mm2,
                         planar=crossings == 0,
                         plane_layers=len([l for l in geom.plane_layers
                                           if l in geom.copper_layers]),
                         pins=sum(1 for p in geom.pads if p.net and not p.hole),
                         grids=_grid_escape(geom))


def check_cost(geom: BoardGeom) -> Dict[str, Tuple[Optional[float], List[Issue]]]:
    out: Dict[str, Tuple[Optional[float], List[Issue]]] = {}

    # ratsnest crossings (placement tangle: each crossing tends to need a via)
    edges = ratsnest_edges([(p.net, p.center) for p in geom.pads if not p.hole], geom.plane_nets)
    crosses = count_crossings(edges)
    out['crossings'] = ((len(crosses) / len(edges)) if edges else None,
                        [Issue('crossings', x, y, 'ratsnest lines of two nets cross here')
                         for x, y in crosses[:60]])

    # drills
    holes = list(geom.holes)
    if holes:
        sizes = {int(round(d / 10_000.0)) for _c, d in holes}         # 0.01 mm classes
        small = [(c, d) for c, d in holes if d < SMALL_DRILL]
        out['drill_sizes'] = (max(0, len(sizes) - 3) / 3.0,
                              [] if len(sizes) <= 3 else
                              [Issue('drill_sizes', geom.outline[0], geom.outline[1],
                                     f'{len(sizes)} different drill sizes: '
                                     + ', '.join(f'{s / 100:.2f}' for s in sorted(sizes)) + ' mm')])
        out['small_drills'] = (len(small) / len(holes),
                               [Issue('small_drills', c[0], c[1], f'drill {d / MM:.2f} mm')
                                for c, d in small[:40]])
    else:
        out['drill_sizes'] = out['small_drills'] = (None, [])

    # fine tracks
    total = sum(s.length for s in geom.segs)
    fine = [s for s in geom.segs if 0 < s.width < FINE_TRACK]
    out['fine_tracks'] = ((sum(s.length for s in fine) / total) if total else None,
                          [Issue('fine_tracks', (s.a[0] + s.b[0]) // 2, (s.a[1] + s.b[1]) // 2,
                                 f'{s.net}: track {s.width / MM:.3f} mm wide') for s in fine[:40]])

    # parts on both sides
    if geom.parts:
        back = [p for p in geom.parts if p.side == 'B']
        front = len(geom.parts) - len(back)
        minority = back if len(back) <= front else [p for p in geom.parts if p.side == 'F']
        out['two_sides'] = ((len(minority) / len(geom.parts)) if back and front else 0.0,
                            [Issue('two_sides', *p.center, f'{p.ref} is on the other side')
                             for p in (minority if back and front else [])][:40])
    else:
        out['two_sides'] = (None, [])

    # inner layers
    inner = [l for l in geom.copper_layers if l not in ('F.Cu', 'B.Cu')]
    if inner:
        used = {s.layer for s in geom.segs} | set(geom.zone_layers)
        empty = [l for l in inner if l not in used]
        out['inner_layers'] = (len(empty) / len(geom.copper_layers),
                               [Issue('inner_layers', geom.outline[0], geom.outline[1],
                                      f'{l} carries no copper') for l in empty])
    else:
        out['inner_layers'] = (0.0, [])

    # routing layers against the routing room (planes are a design choice and
    # single-sided is not pushed for)
    est = estimate_layers(geom, len(crosses))
    if est is None:
        out['layer_count'] = (None, [])
    else:
        have, need, aim = est.signal_layers, est.needed_layers, est.suggested_layers
        f, msg = 0.0, None
        if have < need:
            f = (need - have) / need
            msg = (f'routing room is short: {est.area_per_net():.0f} mm² per net, about '
                   f'{est.need_per_net:.0f} mm² needed -> {need} routing layers '
                   f'(or a larger board)')
        elif have > aim:
            f = (have - aim) / est.layers
            msg = (f'{have} routing layers, {aim} should do: {est.area_per_net(aim):.0f} mm² '
                   f'of routing room per net with {aim}, about {est.need_per_net:.0f} mm² needed '
                   f'(2 -> 4 layers is often +30 to 50 % on the board price)')
        out['layer_count'] = (f, [Issue('layer_count', geom.outline[0], geom.outline[1], msg)]
                              if msg else [])

    # board area against what the parts need
    if geom.parts:
        x1 = min(p.box[0] for p in geom.parts)
        y1 = min(p.box[1] for p in geom.parts)
        x2 = max(p.box[2] for p in geom.parts)
        y2 = max(p.box[3] for p in geom.parts)
        used_area = max(1.0, float(x2 - x1) * float(y2 - y1))
        o = geom.outline
        board_area = float(o[2] - o[0]) * float(o[3] - o[1])
        ratio = board_area / (1.3 * used_area)
        out['board_area'] = (max(0.0, ratio - 1.0),
                             [Issue('board_area', (x1 + x2) // 2, (y1 + y2) // 2,
                                    f'parts use {used_area / MM / MM:.0f} mm² of a '
                                    f'{board_area / MM / MM:.0f} mm² board')] if ratio > 1.5 else [])
    else:
        out['board_area'] = (None, [])
    return out


# ---------------------------------------------------------------------------
# Whole check
# ---------------------------------------------------------------------------

def check(geom: BoardGeom, weights: Optional[Dict[str, float]] = None) -> CheckResult:
    results: Dict[str, Tuple[Optional[float], List[Issue]]] = {}
    results['alignment'] = check_alignment(geom.parts)
    results['orientation'] = check_orientation(geom.parts)
    results.update(check_references(geom.parts, geom.pads))
    results.update(check_routing(geom))
    results.update(check_cost(geom))
    features = {k: results[k][0] for k in CRITERIA}
    qualities = {k: quality(k, features[k]) for k in CRITERIA}
    issues = [i for k in CRITERIA for i in results[k][1]]
    w = {k: 1.0 for k in CRITERIA}
    w.update(weights or {})
    return CheckResult(features=features, qualities=qualities, weights=w, issues=issues,
                       layers=estimate_layers(geom))


# ---------------------------------------------------------------------------
# Reading a KiCad board
# ---------------------------------------------------------------------------

def _layer_name(board, layer_id) -> str:
    """Canonical name (F.Cu, In1.Cu, B.Cu): user names of renamed layers
    would hide which layers are the outer ones."""
    import pcbnew
    try:
        return str(pcbnew.BOARD.GetStandardLayerName(layer_id))
    except Exception:
        return str(board.GetLayerName(layer_id))


def _box_of(bb) -> Box:
    return (bb.GetX(), bb.GetY(), bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight())


def extract_geometry(board) -> BoardGeom:
    """Placement and routing geometry of a pcbnew board."""
    import pcbnew
    geom = BoardGeom()
    try:
        geom.outline = _box_of(board.GetBoardEdgesBoundingBox())
    except Exception:
        pass
    try:
        from .board_model import _board_outline_polygon
        poly = _board_outline_polygon(board, pcbnew)
        if poly and len(poly) >= 3:
            geom.outline_area = abs(sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1]
                                        for i in range(len(poly)))) / 2.0
    except Exception:
        pass
    try:
        from .board_model import _NetClassValue, net_class_names
        width_of = _NetClassValue(board, 'TrackWidth')
        clear_of = _NetClassValue(board, 'Clearance')
    except Exception:
        width_of = clear_of = None
    smd_attrs = {getattr(pcbnew, 'PAD_ATTRIB_SMD', 1), getattr(pcbnew, 'PAD_ATTRIB_CONN', 2)}
    npth = getattr(pcbnew, 'PAD_ATTRIB_NPTH', 3)
    circle = getattr(pcbnew, 'PAD_SHAPE_CIRCLE', 0)
    for fp in board.GetFootprints():
        try:
            back = fp.GetLayer() == pcbnew.B_Cu
        except Exception:
            back = False
        box = None
        try:
            cy = fp.GetCourtyard(pcbnew.B_CrtYd if back else pcbnew.F_CrtYd)
            if cy.OutlineCount() > 0:
                box = _box_of(cy.BBox())
        except Exception:
            box = None
        if box is None:
            box = _box_of(fp.GetBoundingBox(False))
        ref = fp.Reference()
        try:
            ref_visible = bool(ref.IsVisible()) and ref.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS)
        except Exception:
            ref_visible = False
        try:
            ref_angle = ref.GetDrawRotation().AsDegrees()
        except Exception:
            ref_angle = ref.GetTextAngleDegrees()
        try:
            uid = str(fp.m_Uuid.AsString())
        except Exception:
            uid = ''
        try:
            type_name = str(fp.GetFPID().GetLibItemName())
        except Exception:
            type_name = str(fp.GetReference()).rstrip('0123456789')
        index = len(geom.parts)
        geom.parts.append(PartGeom(
            ref=str(fp.GetReference()), uuid=uid, type_name=type_name,
            angle=fp.GetOrientationDegrees() % 360.0, box=box, locked=bool(fp.IsLocked()),
            side='B' if back else 'F', ref_visible=ref_visible,
            ref_box=_box_of(ref.GetBoundingBox()) if ref_visible else None,
            ref_angle=ref_angle))
        for pad in fp.Pads():
            try:
                ls = pad.GetLayerSet()
                sides = tuple(s for s, l in (('F', pcbnew.F_Cu), ('B', pcbnew.B_Cu)) if ls.Contains(l))
                pos = pad.GetPosition()
                geom.pads.append(PadGeom(
                    part=index, net=str(pad.GetNetname()), center=(pos.x, pos.y),
                    box=_box_of(pad.GetBoundingBox()), angle=pad.GetOrientationDegrees(),
                    smd=pad.GetAttribute() in smd_attrs, sides=sides or ('F',),
                    round=pad.GetShape(pcbnew.F_Cu) == circle,
                    name=f'{fp.GetReference()}.{pad.GetNumber()}',
                    hole=pad.GetAttribute() == npth))
            except Exception:
                continue
            net = geom.pads[-1].net
            if net and net not in geom.net_pitch and width_of is not None:
                try:
                    names = net_class_names(pad)
                    geom.net_pitch[net] = int(width_of(names)) + int(clear_of(names))
                except Exception:
                    pass
            try:
                d = pad.GetDrillSize()
                if d.x > 0 and d.y > 0:
                    geom.holes.append(((pos.x, pos.y), min(d.x, d.y)))
            except Exception:
                pass
    for t in board.GetTracks():
        try:
            cls = t.GetClass()
            net = str(t.GetNetname())
            if cls == 'PCB_VIA':
                p = t.GetPosition()
                geom.vias.append(ViaGeom(net=net, center=(p.x, p.y), diameter=t.GetWidth(pcbnew.F_Cu)))
                geom.holes.append(((p.x, p.y), t.GetDrillValue()))
                continue
            layer = _layer_name(board, t.GetLayer())
            a, b = t.GetStart(), t.GetEnd()
            if cls == 'PCB_ARC':
                m = t.GetMid()
                geom.segs.append(SegGeom(net, layer, (a.x, a.y), (m.x, m.y), t.GetWidth(), arc=True))
                geom.segs.append(SegGeom(net, layer, (m.x, m.y), (b.x, b.y), t.GetWidth(), arc=True))
            else:
                geom.segs.append(SegGeom(net, layer, (a.x, a.y), (b.x, b.y), t.GetWidth()))
        except Exception:
            continue
    try:
        geom.copper_layers = tuple(_layer_name(board, l)
                                   for l in board.GetEnabledLayers().CuStack())
    except Exception:
        pass
    for z in board.Zones():                 # filled copper, for the drawing
        try:
            if z.GetIsRuleArea():
                continue
            for l in z.GetLayerSet().CuStack():
                if not z.HasFilledPolysForLayer(l):
                    continue
                polys = z.GetFilledPolysList(l)
                rings: List[List[Pt]] = []
                for i in range(polys.OutlineCount()):
                    chains = [polys.Outline(i)] + [polys.Hole(i, h) for h in range(polys.HoleCount(i))]
                    for ch in chains:
                        rings.append([(int(ch.CPoint(k).x), int(ch.CPoint(k).y))
                                      for k in range(ch.PointCount())])
                if rings:
                    geom.fills.append((_layer_name(board, l), str(z.GetNetname()), rings))
        except Exception:
            continue
    zl, planes, pours, big_layers = set(), set(), set(), set()
    o = geom.outline
    box_area = float(o[2] - o[0]) * float(o[3] - o[1])
    for z in board.Zones():
        try:
            if z.GetIsRuleArea() or (hasattr(z, 'IsTeardropArea') and z.IsTeardropArea()):
                continue
            bb = z.GetBoundingBox()
            big = float(bb.GetWidth()) * float(bb.GetHeight()) >= POUR_SHARE * box_area
            for l in z.GetLayerSet().CuStack():
                zl.add(_layer_name(board, l))
                if big:
                    big_layers.add(_layer_name(board, l))
            if z.GetNetname() and big:
                planes.add(str(z.GetNetname()))
                pours.add(str(z.GetNetname()))
        except Exception:
            continue
    # inner layers holding a large zone and few tracks are planes
    total_len = sum(s.length for s in geom.segs) or 1.0
    per_layer: Dict[str, float] = {}
    for sg in geom.segs:
        per_layer[sg.layer] = per_layer.get(sg.layer, 0.0) + sg.length
    geom.plane_layers = tuple(sorted(l for l in big_layers if l not in ('F.Cu', 'B.Cu')
                                     and per_layer.get(l, 0.0) < 0.1 * total_len))
    try:
        from .board_model import _is_power_net_name
        for pad in geom.pads:
            if pad.net and _is_power_net_name(pad.net):
                planes.add(pad.net)
    except Exception:
        pass
    geom.zone_layers = tuple(sorted(zl))
    geom.plane_nets = tuple(sorted(planes))
    geom.pour_nets = tuple(sorted(pours))
    return geom
