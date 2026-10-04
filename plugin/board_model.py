"""Extract KiCad board data into pure Python structures for the optimizer."""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Set, Optional, FrozenSet

from .rules import PadInfo, RuleSet, copper_layer_names, load_rules_file, set_board_layers
from .isolation import IsolationModel, build_isolation_model


# Power net detection — net name patterns that are unambiguously power/ground.
# Strips a leading '/' (KiCad hierarchical prefix) before matching.
# Used to exclude power nets from HPWL: their contribution is a large constant
# that can't be optimised and dominates the incremental HPWL computation.
_POWER_NAME_PREFIXES = (
    'GND', 'AGND', 'DGND', 'PGND', 'SGND',  # ground variants
    'VSS',                                     # ground (CMOS)
    'VCC', 'VDD', 'VEE',                       # supply rails
    'VBAT', 'VBUS',                            # battery / USB bus
)
_POWER_VOLTAGE_RE = re.compile(r'^[+\-]\d[\d.]*V', re.IGNORECASE)

# Margin around pad extents for the placement bounding box (0.25 mm).
# With pad physical sizes included in the extent calculation,
# 0.25 mm closely matches IPC courtyard envelopes.
PAD_MARGIN = 250_000

# Isolation requirements at or below this distance are already guaranteed by
# non-overlapping footprint boxes (each box carries PAD_MARGIN around its pads).
ISOLATION_THRESHOLD = 2 * PAD_MARGIN

# Placement-area source types (KiCad 9: SHEETNAME, COMPONENT_CLASS;
# KiCad 10 adds GROUP_PLACEMENT and DESIGN_BLOCK).
AREA_SHEET = 0
AREA_COMPONENT_CLASS = 1
AREA_GROUP = 2
AREA_DESIGN_BLOCK = 3


def _is_power_net_name(name: str) -> bool:
    """Return True if the net name looks like a power or ground net."""
    n = name.lstrip('/').upper()
    if any(n.startswith(p) for p in _POWER_NAME_PREFIXES):
        return True
    # +3V3, +5V, +3.3V, -12V, etc.
    return bool(_POWER_VOLTAGE_RE.match(n))


# All positions/dimensions in nanometers (KiCad native units: 1 mm = 1_000_000 nm)

@dataclass
class Pad:
    """A single pad on a footprint."""
    net_code: int               # 0 = unconnected
    net_name: str
    offset_x: int              # pad offset from footprint center at 0° rotation (nm)
    offset_y: int
    number: str = ''

    def abs_position(self, fp_x: int, fp_y: int, angle_deg: float) -> Tuple[int, int]:
        """Compute absolute pad position given footprint center and rotation.

        KiCad uses clockwise rotation (Y-axis points down), so we negate
        the angle to convert to standard math (CCW) trigonometry.
        """
        rad = math.radians(-angle_deg)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        rx = int(self.offset_x * cos_a - self.offset_y * sin_a)
        ry = int(self.offset_x * sin_a + self.offset_y * cos_a)
        return (fp_x + rx, fp_y + ry)


@dataclass
class Footprint:
    """A component on the board."""
    reference: str             # "U1", "R3"
    index: int                 # position in the footprints list
    x: int                     # center x (nm)
    y: int                     # center y (nm)
    angle_deg: float           # rotation in degrees (any angle)
    width: int                 # bounding box width at 0° rotation (nm)
    height: int                # bounding box height at 0° rotation (nm)
    locked: bool
    pads: List[Pad] = field(default_factory=list)
    net_codes: Set[int] = field(default_factory=set)
    cx_offset: int = 0         # bbox center x offset from fp origin at 0° (nm)
    cy_offset: int = 0         # bbox center y offset from fp origin at 0° (nm)
    uuid: str = ''             # KiCad KIID — stable identity (references may repeat)
    side: str = 'F'            # 'F' (front) or 'B' (back)
    copper_sides: Tuple[str, ...] = ()  # outer copper layers with pads ('F','B'); () = side only
    sheet: str = ''            # hierarchical sheet path, e.g. "/Power/"
    component_classes: Tuple[str, ...] = ()
    groups: Tuple[str, ...] = ()   # names of the enclosing groups, innermost first
    lib_id: str = ''               # footprint library id, e.g. 'Resistor_SMD:R_0805_2012Metric'
    # Cached trig values for abs_position (updated via set_angle)
    _cos_a: float = field(init=False, repr=False, default=1.0)
    _sin_a: float = field(init=False, repr=False, default=0.0)

    def __post_init__(self):
        self._update_trig()

    def _update_trig(self):
        rad = math.radians(-self.angle_deg)
        self._cos_a = math.cos(rad)
        self._sin_a = math.sin(rad)

    def set_angle(self, angle_deg: float):
        """Set rotation angle and update cached trig values."""
        self.angle_deg = angle_deg
        self._update_trig()

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        """Return (xmin, ymin, xmax, ymax) accounting for rotation.

        The bbox center may not coincide with the footprint origin (e.g. for
        asymmetric connectors whose courtyard extends further on one side).
        cx_offset / cy_offset are the bbox-center offsets at 0° rotation;
        abs_position() rotates them into world space.
        """
        cx = self.x + int(self.cx_offset * self._cos_a - self.cy_offset * self._sin_a)
        cy = self.y + int(self.cx_offset * self._sin_a + self.cy_offset * self._cos_a)
        if int(self.angle_deg) % 180 == 90:
            hw, hh = self.height // 2, self.width // 2
        else:
            hw, hh = self.width // 2, self.height // 2
        return (cx - hw, cy - hh, cx + hw, cy + hh)


@dataclass
class Net:
    """A net with references to its pads."""
    net_code: int
    net_name: str
    pad_refs: List[Tuple[int, int]] = field(default_factory=list)  # (fp_index, pad_index)
    is_excluded: bool = False  # True → excluded from HPWL (large constant, can't be optimised)


@dataclass
class KeepOut:
    """A keep-out for footprints (KiCad rule area with "keep out footprints"
    and/or "keep out pads").

    The bounding box is always set; ``polygon`` holds the real outline when it
    is not a plain rectangle. ``sides`` lists the outer copper layers it is on
    ('F', 'B'); None means it applies to every footprint. A footprint keep-out
    stops footprints placed on those sides; a pad keep-out stops any footprint
    with pads there — through-hole parts have pads on both sides.
    """
    xmin: int
    ymin: int
    xmax: int
    ymax: int
    polygon: Optional[List[Tuple[int, int]]] = None
    sides: Optional[FrozenSet[str]] = None
    name: str = ''
    no_footprints: bool = True
    no_pads: bool = False


@dataclass
class PlacementArea:
    """A KiCad placement rule area: its members should stay inside it."""
    xmin: int
    ymin: int
    xmax: int
    ymax: int
    members: Set[int] = field(default_factory=set)
    polygon: Optional[List[Tuple[int, int]]] = None
    source_type: int = AREA_SHEET
    source: str = ''
    name: str = ''
    exclusive: bool = True       # non-members are kept out


@dataclass
class ComponentGroup:
    """A group of footprints that must move as a rigid body."""
    member_indices: List[int]   # footprint indices in this group
    locked: bool = False


@dataclass
class BoardModel:
    """Complete board state for the optimizer — pure Python, no pcbnew."""
    footprints: List[Footprint]
    nets: Dict[int, Net]
    outline_xmin: int
    outline_ymin: int
    outline_xmax: int
    outline_ymax: int
    keepouts: List[KeepOut] = field(default_factory=list)
    moveable_indices: List[int] = field(default_factory=list)
    outline_polygon: Optional[List[Tuple[int, int]]] = None  # board outline vertices (nm)
    component_groups: List[ComponentGroup] = field(default_factory=list)
    # Map from fp_index → group index (None if not grouped)
    fp_to_group: Dict[int, int] = field(default_factory=dict)
    placement_areas: List[PlacementArea] = field(default_factory=list)
    isolation: Optional[IsolationModel] = None
    warnings: List[str] = field(default_factory=list)


def polygon_is_rectangle(poly: List[Tuple[int, int]], tol: int = 1000) -> bool:
    """True if the polygon is an axis-aligned rectangle: its distinct vertices
    are exactly the four corners of its bounding box (1 µm tolerance).
    Chamfered, rounded, diamond or L-shaped outlines are not rectangles."""
    if not poly:
        return True
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    # Every vertex on the bounding-box perimeter (rules out L shapes, notches)...
    for x, y in poly:
        if not (abs(x - xmin) <= tol or abs(x - xmax) <= tol
                or abs(y - ymin) <= tol or abs(y - ymax) <= tol):
            return False
    # ...and every edge axis-aligned (rules out chamfers, arcs, diamonds).
    n = len(poly)
    for k in range(n):
        (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % n]
        if abs(x1 - x2) > tol and abs(y1 - y2) > tol:
            return False
    return True


def _zone_outline(zone) -> Optional[List[Tuple[int, int]]]:
    try:
        ps = zone.Outline()
        if ps is None or ps.OutlineCount() == 0:
            return None
        chain = ps.Outline(0)
        pts = [(chain.CPoint(i).x, chain.CPoint(i).y) for i in range(chain.PointCount())]
        return pts if len(pts) >= 3 else None
    except Exception:
        return None


def _zone_sides(zone, pcbnew) -> FrozenSet[str]:
    sides = set()
    try:
        ls = zone.GetLayerSet()
        if ls.Contains(pcbnew.F_Cu):
            sides.add('F')
        if ls.Contains(pcbnew.B_Cu):
            sides.add('B')
    except Exception:
        sides = {'F', 'B'}
    return frozenset(sides)


def _zone_placement(zone) -> Optional[Tuple[int, str]]:
    """(source_type, source) when the rule area is a placement area."""
    for enabled, stype, src in (
        ('GetPlacementAreaEnabled', 'GetPlacementAreaSourceType', 'GetPlacementAreaSource'),  # KiCad 10
        ('GetRuleAreaPlacementEnabled', 'GetRuleAreaPlacementSourceType',
         'GetRuleAreaPlacementSource'),                                                    # KiCad 9
    ):
        fn = getattr(zone, enabled, None)
        if fn is None:
            continue
        try:
            if not fn():
                return None
            return int(getattr(zone, stype)()), str(getattr(zone, src)())
        except Exception:
            return None
    return None


def _strip_slash(s: str) -> str:
    return s[:-1] if s.endswith('/') else s


def area_members(footprints: List[Footprint], source_type: int, source: str) -> Optional[Set[int]]:
    """Footprints belonging to a placement area (None if the source type is
    not supported). Mirrors KiCad's memberOfSheetOrChildren / hasComponentClass
    / memberOfGroup tests."""
    if source_type == AREA_SHEET:
        target = _strip_slash(source)
        return {f.index for f in footprints
                if _strip_slash(f.sheet) == target or _strip_slash(f.sheet).startswith(target + '/')}
    if source_type == AREA_COMPONENT_CLASS:
        return {f.index for f in footprints if source in f.component_classes}
    if source_type == AREA_GROUP:
        return {f.index for f in footprints if source in f.groups}
    return None


def _group_chain(fp) -> Tuple[str, ...]:
    names: List[str] = []
    try:
        grp = fp.GetParentGroup()
        guard = 0
        while grp is not None and guard < 32:
            guard += 1
            name = grp.GetName() if hasattr(grp, 'GetName') else ''
            if name:
                names.append(str(name))
            item = grp.AsEdaItem() if hasattr(grp, 'AsEdaItem') else grp
            grp = item.GetParentGroup() if hasattr(item, 'GetParentGroup') else None
    except Exception:
        pass
    return tuple(names)


def _lib_id(fp) -> str:
    try:
        return str(fp.GetFPIDAsString())
    except Exception:
        return ''


def net_class_names(item) -> Tuple[str, ...]:
    """Constituent net classes of a pad / track ('HV,Default' -> ('HV', 'Default'))."""
    try:
        return tuple(n.strip() for n in str(item.GetNetClassName()).split(',')
                     if n.strip()) or ('Default',)
    except Exception:
        return ('Default',)


def pad_rule_info(pcbnew, pad, attr, side: str, ref: str, lib_id: str, sheet: str,
                  classes: Tuple[str, ...], groups: Tuple[str, ...], nc_clearance) -> PadInfo:
    """What the design rules can see of a pad. A non-plated hole (no copper,
    no net) only matters for creepage."""
    if attr == getattr(pcbnew, 'PAD_ATTRIB_NPTH', -1):
        pad_type, layers = 'npth', ('*.Cu',)
    elif attr == getattr(pcbnew, 'PAD_ATTRIB_PTH', -2):
        pad_type, layers = 'tht', ('*.Cu',)
    else:
        pad_type = 'conn' if attr == getattr(pcbnew, 'PAD_ATTRIB_CONN', -3) else 'smd'
        try:
            on_b = pad.GetLayerSet().Contains(pcbnew.B_Cu)
            on_f = pad.GetLayerSet().Contains(pcbnew.F_Cu)
        except Exception:
            on_f, on_b = side == 'F', side == 'B'
        layers = tuple(l for l, on in (('F.Cu', on_f), ('B.Cu', on_b)) if on) or ('F.Cu',)
    nc_names = net_class_names(pad)
    return PadInfo(
        net_name=str(pad.GetNetname()),
        netclasses=nc_names,
        nc_clearance=nc_clearance(nc_names),
        fp_ref=ref,
        fp_lib_id=lib_id,
        component_classes=classes,
        sheet=sheet,
        groups=groups,
        pad_type=pad_type,
        layers=layers,
        kind='pad',
    )


def board_copper_layers(board) -> Tuple[str, ...]:
    try:
        return copper_layer_names(int(board.GetCopperLayerCount()))
    except Exception:
        return copper_layer_names(2)


def board_layer_aliases(board) -> Dict[str, str]:
    """User names of renamed copper layers -> canonical names."""
    import pcbnew
    out: Dict[str, str] = {}
    try:
        for lid in board.GetEnabledLayers().CuStack():
            user = str(board.GetLayerName(lid))
            canon = str(pcbnew.BOARD.GetStandardLayerName(lid))
            if user and user != canon:
                out[user] = canon
    except Exception:
        pass
    return out


def load_board_rules(board) -> RuleSet:
    """Custom rules of the board's project, with its minimum clearance and
    copper layers (also registered for layer matching in rule conditions)."""
    try:
        min_clr = int(board.GetDesignSettings().m_MinClearance)
    except Exception:
        min_clr = 0
    layers = board_copper_layers(board)
    set_board_layers(layers, board_layer_aliases(board))
    return load_rules_file(_rules_path(board), board_min_clearance=min_clr,
                           copper_layers=layers)


def _component_classes(fp) -> Tuple[str, ...]:
    try:
        s = str(fp.GetComponentClassAsString())
    except Exception:
        return ()
    return tuple(c.strip() for c in s.split(',') if c.strip())


class _NetClassValue:
    """Effective value (clearance, track width...) of a (possibly composite)
    net class, KiCad-style: the highest-priority constituent that defines
    the value wins."""

    def __init__(self, board, what: str = 'Clearance'):
        self._what = what
        self._cache: Dict[Tuple[str, ...], int] = {}
        self._ns = None
        self._default = 0
        try:
            self._ns = board.GetDesignSettings().m_NetSettings
            self._default = int(getattr(self._ns.GetDefaultNetclass(), 'Get' + what)())
        except Exception:
            self._ns = None

    def __call__(self, names: Tuple[str, ...]) -> int:
        if names in self._cache:
            return self._cache[names]
        best = None
        if self._ns is not None:
            cands = []
            for n in names:
                try:
                    nc = self._ns.GetNetClassByName(n)
                except Exception:
                    nc = None
                if nc is None:
                    continue
                prio = nc.GetPriority() if hasattr(nc, 'GetPriority') else 0
                has_fn = getattr(nc, 'Has' + self._what, None)
                has = has_fn() if has_fn is not None else True
                cands.append((prio, has, int(getattr(nc, 'Get' + self._what)()) if has else 0))
            for _prio, has, val in sorted(cands, key=lambda t: t[0]):
                if has:
                    best = val
                    break
        if best is None:
            best = self._default
        self._cache[names] = best
        return best


class _NetClassClearance(_NetClassValue):
    def __init__(self, board):
        super().__init__(board, 'Clearance')


def _board_outline_polygon(board, pcbnew) -> Optional[List[Tuple[int, int]]]:
    poly_set = pcbnew.SHAPE_POLY_SET()
    try:
        # KiCad 10 added a mandatory 'aInferOutlineIfNecessary' argument.
        board.GetBoardPolygonOutlines(poly_set, True)
    except TypeError:
        board.GetBoardPolygonOutlines(poly_set)        # KiCad 9
    if poly_set.OutlineCount() > 0:
        outline = poly_set.Outline(0)
        pts = [(outline.CPoint(vi).x, outline.CPoint(vi).y)
               for vi in range(outline.PointCount())]
        if len(pts) >= 3:
            return pts
    return None


def extract_board_model(board, selected_only: bool = False,
                        use_isolation: bool = True,
                        exclusive_areas: bool = True) -> BoardModel:
    """
    Extract board data from a pcbnew.BOARD into a BoardModel.

    This is the ONLY function in the optimizer that reads pcbnew APIs.
    It runs once at the start of optimization.

    If selected_only is True, only footprints currently selected in the
    KiCad editor will be moveable; all others are treated as locked.
    """
    import pcbnew

    footprints: List[Footprint] = []
    nets: Dict[int, Net] = {}
    warnings: List[str] = []
    nc_clearance = _NetClassClearance(board)
    iso_pads = []   # (fp_index, pad_index, net_code, PadInfo, ox, oy, hx, hy)

    for i, fp in enumerate(board.GetFootprints()):
        pos = fp.GetPosition()
        angle = fp.GetOrientationDegrees() % 360.0
        ref = fp.GetReference()
        try:
            side = 'B' if fp.GetLayer() == pcbnew.B_Cu else 'F'
        except Exception:
            side = 'F'
        try:
            sheet = str(fp.GetSheetname())
        except Exception:
            sheet = ''
        classes = _component_classes(fp)
        groups = _group_chain(fp)
        lib_id = _lib_id(fp)

        pads_list = []
        fp_net_codes: Set[int] = set()
        copper_sides: Set[str] = set()

        # Track extents at 0° rotation to compute a tight bounding box.
        # Initialise to None; set from first pad or courtyard data.
        ext_xmin = ext_xmax = ext_ymin = ext_ymax = None

        # KiCad uses CW rotation (Y-down), so un-rotate with +angle
        # (not -angle) to recover canonical 0° pad offsets.
        rad = math.radians(angle)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        quarter = abs(angle % 90.0) < 1e-6 or abs(angle % 90.0 - 90.0) < 1e-6
        swap = quarter and int(round(angle)) % 180 == 90

        for j, pad in enumerate(fp.Pads()):
            pad_pos = pad.GetPosition()
            nc = pad.GetNetCode()

            # Un-rotate pad offset to get canonical offset at 0°
            dx = pad_pos.x - pos.x
            dy = pad_pos.y - pos.y
            offset_x = int(dx * cos_a - dy * sin_a)
            offset_y = int(dx * sin_a + dy * cos_a)

            # Pad physical size — use max dimension as conservative extent
            # (handles pads with custom rotation relative to footprint)
            pad_size = pad.GetSize()
            pad_half = max(pad_size.x, pad_size.y) // 2

            # Expand extents to include this pad
            if ext_xmin is None:
                ext_xmin = offset_x - pad_half
                ext_xmax = offset_x + pad_half
                ext_ymin = offset_y - pad_half
                ext_ymax = offset_y + pad_half
            else:
                ext_xmin = min(ext_xmin, offset_x - pad_half)
                ext_xmax = max(ext_xmax, offset_x + pad_half)
                ext_ymin = min(ext_ymin, offset_y - pad_half)
                ext_ymax = max(ext_ymax, offset_y + pad_half)

            try:
                number = str(pad.GetNumber())
            except Exception:
                number = ''
            p = Pad(
                net_code=nc,
                net_name=pad.GetNetname(),
                offset_x=offset_x,
                offset_y=offset_y,
                number=number,
            )
            pads_list.append(p)

            if nc > 0:
                fp_net_codes.add(nc)
                if nc not in nets:
                    nets[nc] = Net(net_code=nc, net_name=pad.GetNetname())

            # Which outer copper layers carry pads (for "keep out pads" areas)
            try:
                attr = pad.GetAttribute()
            except Exception:
                attr = None
            if attr in (getattr(pcbnew, 'PAD_ATTRIB_PTH', -2), getattr(pcbnew, 'PAD_ATTRIB_NPTH', -1)):
                copper_sides.update(('F', 'B'))
            else:
                try:
                    if pad.GetLayerSet().Contains(pcbnew.F_Cu):
                        copper_sides.add('F')
                    if pad.GetLayerSet().Contains(pcbnew.B_Cu):
                        copper_sides.add('B')
                except Exception:
                    copper_sides.add(side)

            # --- Isolation data: copper box of the pad, at footprint 0° ---
            if use_isolation:
                try:
                    bb = pad.GetBoundingBox()
                    bcx = bb.GetX() + bb.GetWidth() / 2.0
                    bcy = bb.GetY() + bb.GetHeight() / 2.0
                    hw = bb.GetWidth() / 2.0
                    hh = bb.GetHeight() / 2.0
                except Exception:
                    bcx, bcy = pad_pos.x, pad_pos.y
                    hw, hh = pad_size.x / 2.0, pad_size.y / 2.0
                bdx = bcx - pos.x
                bdy = bcy - pos.y
                box_ox = int(bdx * cos_a - bdy * sin_a)
                box_oy = int(bdx * sin_a + bdy * cos_a)
                if quarter:
                    hx, hy = (hh, hw) if swap else (hw, hh)
                else:
                    # Non-orthogonal footprint: keep a box that covers the pad
                    # whatever the rotation (conservative).
                    hx = hy = math.hypot(hw, hh)
                info = pad_rule_info(pcbnew, pad, attr, side, ref, lib_id, sheet, classes,
                                     groups, nc_clearance)
                iso_pads.append((i, j, nc, info, box_ox, box_oy, int(hx), int(hy)))

        # Also expand extents from the courtyard outline.
        # fp.GraphicalItems() includes FP_SHAPE items on F_CrtYd / B_CrtYd.
        # Their GetStart()/GetEnd() coordinates are in BOARD (world) space,
        # so we apply the same (translate → un-rotate) transform used for pads
        # above to get footprint-local (0°) coordinates.
        # This catches components (inductors, connectors) whose body extends
        # well beyond their pad extents.
        try:
            for item in fp.GraphicalItems():
                if item.GetLayer() not in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
                    continue
                pts_world = []
                try:
                    # For circles, GetStart() is the center and GetEnd() is a point on the perimeter.
                    # Just adding them would only bound a single quadrant!
                    is_circle = False
                    if hasattr(item, 'GetShape'):
                        shape_val = item.GetShape()
                        is_circle = shape_val in (getattr(pcbnew, 'S_CIRCLE', -1), getattr(pcbnew, 'SHAPE_T_CIRCLE', -1))

                    if is_circle and hasattr(item, 'GetCenter') and hasattr(item, 'GetRadius'):
                        c = item.GetCenter()
                        r = item.GetRadius()

                        class Pt:
                            def __init__(self, x, y):
                                self.x = x
                                self.y = y

                        # Calculate local center coordinates
                        tx_c = c.x - pos.x
                        ty_c = c.y - pos.y
                        lcx = tx_c * cos_a - ty_c * sin_a
                        lcy = tx_c * sin_a + ty_c * cos_a

                        # Generate world points that un-rotate perfectly to the local 2r x 2r bounding box
                        def local_to_world(lx, ly):
                            return Pt(pos.x + lx * cos_a + ly * sin_a, pos.y - lx * sin_a + ly * cos_a)

                        pts_world.append(local_to_world(lcx - r, lcy - r))
                        pts_world.append(local_to_world(lcx + r, lcy - r))
                        pts_world.append(local_to_world(lcx - r, lcy + r))
                        pts_world.append(local_to_world(lcx + r, lcy + r))
                    else:
                        pts_world.append(item.GetStart())
                        pts_world.append(item.GetEnd())
                except AttributeError:
                    pass
                try:
                    shape = item.GetPolyShape()
                    if shape.OutlineCount() > 0:
                        outline = shape.Outline(0)
                        for vi in range(outline.PointCount()):
                            pts_world.append(outline.CPoint(vi))
                except AttributeError:
                    pass
                for pt in pts_world:
                    tx = pt.x - pos.x
                    ty = pt.y - pos.y
                    lx = int(tx * cos_a - ty * sin_a)
                    ly = int(tx * sin_a + ty * cos_a)
                    if ext_xmin is None:
                        ext_xmin = lx; ext_xmax = lx
                        ext_ymin = ly; ext_ymax = ly
                    else:
                        ext_xmin = min(ext_xmin, lx); ext_xmax = max(ext_xmax, lx)
                        ext_ymin = min(ext_ymin, ly); ext_ymax = max(ext_ymax, ly)
        except Exception:
            pass  # courtyard graphics not available; pad extents alone are used

        if ext_xmin is None:
            ext_xmin = ext_xmax = ext_ymin = ext_ymax = 0

        # Bounding box = union of pad + courtyard extents, plus margin.
        # PAD_MARGIN is added symmetrically so it doesn't shift the center.
        fp_width = (ext_xmax - ext_xmin) + 2 * PAD_MARGIN
        fp_height = (ext_ymax - ext_ymin) + 2 * PAD_MARGIN
        # Ensure minimum size for single-pad / no-pad footprints
        fp_width = max(fp_width, 2 * PAD_MARGIN)
        fp_height = max(fp_height, 2 * PAD_MARGIN)

        # Center of the bbox may not coincide with the fp origin for asymmetric
        # footprints (e.g. connectors with courtyard offset to one side).
        # Store the local (0°) offset so bbox property can apply abs_position().
        fp_cx_offset = (ext_xmin + ext_xmax) // 2
        fp_cy_offset = (ext_ymin + ext_ymax) // 2

        is_locked = fp.IsLocked() or (selected_only and not fp.IsSelected())
        try:
            uuid = str(fp.m_Uuid.AsString())
        except Exception:
            uuid = ''
        f = Footprint(
            reference=ref,
            index=i,
            x=pos.x,
            y=pos.y,
            angle_deg=angle,
            width=fp_width,
            height=fp_height,
            cx_offset=fp_cx_offset,
            cy_offset=fp_cy_offset,
            locked=is_locked,
            pads=pads_list,
            net_codes=fp_net_codes,
            uuid=uuid,
            side=side,
            copper_sides=tuple(sorted(copper_sides)),
            sheet=sheet,
            component_classes=classes,
            groups=groups,
            lib_id=lib_id,
        )
        footprints.append(f)

    # Build pad_refs for each net
    for fi, fp in enumerate(footprints):
        for pi, pad in enumerate(fp.pads):
            if pad.net_code > 0:
                nets[pad.net_code].pad_refs.append((fi, pi))

    # Board outline bounding box
    bbox = board.GetBoardEdgesBoundingBox()
    ox = bbox.GetX()
    oy = bbox.GetY()

    # Rule areas: footprint keep-outs and placement areas
    keepouts: List[KeepOut] = []
    placement_areas: List[PlacementArea] = []
    for zone in board.Zones():
        try:
            if not zone.GetIsRuleArea():
                continue
            zb = zone.Outline().BBox()
            xmin, ymin = zb.GetX(), zb.GetY()
            xmax, ymax = xmin + zb.GetWidth(), ymin + zb.GetHeight()
        except Exception:
            continue
        poly = _zone_outline(zone)
        if poly is not None and polygon_is_rectangle(poly):
            poly = None
        try:
            name = str(zone.GetZoneName())
        except Exception:
            name = ''

        try:
            no_fp = bool(zone.GetDoNotAllowFootprints())
        except Exception:
            no_fp = False
        try:
            no_pads = bool(zone.GetDoNotAllowPads())
        except Exception:
            no_pads = False
        if no_fp or no_pads:
            sides = _zone_sides(zone, pcbnew)
            if sides:
                keepouts.append(KeepOut(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
                                        polygon=poly, sides=sides, name=name,
                                        no_footprints=no_fp, no_pads=no_pads))

        placement = _zone_placement(zone)
        if placement is not None:
            stype, source = placement
            members = area_members(footprints, stype, source)
            label = name or source
            if members is None:
                warnings.append(f"Placement area '{label}': source type not supported (ignored)")
            elif not members:
                warnings.append(f"Placement area '{label}': no footprint matches '{source}'")
            else:
                placement_areas.append(PlacementArea(
                    xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax, members=members,
                    polygon=poly, source_type=stype, source=source, name=label,
                    exclusive=exclusive_areas))

    # Extract component groups
    component_groups: List[ComponentGroup] = []
    fp_to_group: Dict[int, int] = {}
    try:
        uuid_to_idx: Dict[str, int] = {f.uuid: f.index for f in footprints if f.uuid}

        for grp in board.Groups():
            member_uuids = []
            for item in grp.GetItems():
                uid = str(item.m_Uuid.AsString())
                if uid in uuid_to_idx:
                    member_uuids.append(uid)

            if len(member_uuids) >= 2:
                indices = [uuid_to_idx[u] for u in member_uuids]
                grp_locked = grp.IsLocked() or any(
                    footprints[i].locked for i in indices)
                gi = len(component_groups)
                component_groups.append(ComponentGroup(
                    member_indices=indices, locked=grp_locked))
                for idx in indices:
                    fp_to_group[idx] = gi
    except Exception:
        pass  # group extraction not available

    # Build moveable list: exclude locked footprints and locked-group members.
    # For unlocked groups, include only ONE representative per group to avoid
    # selecting the same group multiple times.
    locked_by_group: Set[int] = set()
    group_reps: Set[int] = set()
    for grp in component_groups:
        if grp.locked:
            locked_by_group.update(grp.member_indices)
        else:
            # Use the first member as the group representative
            group_reps.add(grp.member_indices[0])
            # Exclude other members from moveable
            for idx in grp.member_indices[1:]:
                locked_by_group.add(idx)

    moveable = [i for i, fp in enumerate(footprints)
                if not fp.locked and i not in locked_by_group]

    # Extract board outline polygon for non-rectangular boards
    outline_polygon: Optional[List[Tuple[int, int]]] = None
    try:
        outline_polygon = _board_outline_polygon(board, pcbnew)
    except Exception as e:
        warnings.append(f"Board outline polygon not read ({e}); using its bounding box")

    # Mark power/ground nets — excluded from HPWL (their contribution is a large
    # constant and can't be reduced by placement).
    #
    # Two complementary methods:
    #   1. #PWR phantom footprints (KiCad 5/6 boards): any net referenced by a
    #      footprint whose reference starts with '#PWR' is a power net.
    #   2. Net name pattern matching (all boards including KiCad 7): net names
    #      matching common power/ground patterns (GND*, VCC*, +nV, etc.).
    for fp in footprints:
        if fp.reference.startswith('#PWR'):
            for nc in fp.net_codes:
                if nc in nets:
                    nets[nc].is_excluded = True
    for nc, net in nets.items():
        if not net.is_excluded and _is_power_net_name(net.net_name):
            net.is_excluded = True

    # Isolation rules: net-class clearances + custom rules (.kicad_dru)
    isolation: Optional[IsolationModel] = None
    if use_isolation:
        rules = load_board_rules(board)
        for rname, reason in rules.unsupported:
            warnings.append(f"Rule '{rname}' ignored: {reason}")
        isolation = build_isolation_model(iso_pads, rules, ISOLATION_THRESHOLD)

    return BoardModel(
        footprints=footprints,
        nets=nets,
        outline_xmin=ox,
        outline_ymin=oy,
        outline_xmax=ox + bbox.GetWidth(),
        outline_ymax=oy + bbox.GetHeight(),
        keepouts=keepouts,
        moveable_indices=moveable,
        outline_polygon=outline_polygon,
        component_groups=component_groups,
        fp_to_group=fp_to_group,
        placement_areas=placement_areas,
        isolation=isolation,
        warnings=warnings,
    )


def _rules_path(board) -> str:
    """Custom rules live next to the project: <name>.kicad_dru."""
    try:
        fn = str(board.GetFileName())
    except Exception:
        fn = ''
    if not fn:
        return ''
    return os.path.splitext(fn)[0] + '.kicad_dru'
