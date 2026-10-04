"""Copper pour (ground plane) on one outer layer after routing, keeping the
isolation rules.

KiCad's zone filler keeps the clearance of the rules from the pour, but not
the creepage distances (and a pour runs along the board surface, the very
path creepage is measured on). So around every copper item on the pour's
layer whose required distance to the pour's net (clearance, physical
clearance or creepage, from the net classes and the project's custom rules)
is larger than the plain clearance, the pour keeps that distance; each
cluster of such items (a primary side, say) gets one convex cut-out, so the
isolation boundary is clean.

The pour is one zone per piece of outline, named "place-news pour"; running
it again replaces it (Edit > Undo restores the previous one).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .rules import PadInfo, RuleSet, track_item, via_item

POUR_NAME = 'place-news pour'
MAX_ERROR = 5_000                # 5 µm polygon approximation (outside the item)
MARGIN = 20_000                  # 20 µm more than required: the fill's own rounding

# Ground-like net names (not PE / EARTH / COM: a protective earth or a relay
# common is no ground plane).
_GROUND_RE = re.compile(r'^(?:[ADPS]?GND\w*|0V|VSS)$', re.IGNORECASE)


@dataclass
class PourReport:
    net: str = ''
    layer: str = 'F.Cu'
    zones: int = 0                  # zones created (one per outline piece)
    cutouts: int = 0                # items with a widened cut-out
    widest: int = 0                 # widest cut-out (nm)
    replaced: int = 0               # previous place-news pours removed
    filled: bool = False
    error: str = ''
    examples: List[str] = field(default_factory=list)   # "J1.1 (HV): 6.00 mm"

    def describe(self) -> List[str]:
        if self.error:
            return [f'Pour not made: {self.error}']
        lines = [f'{self.net} poured on {self.layer}'
                 + (' (replaces the previous pour)' if self.replaced else '')
                 + ('' if self.filled else ' — not filled yet: press B to fill')]
        if self.cutouts:
            lines.append(f'  {self.cutouts} item(s) kept further from the pour than the clearance '
                         f'(isolation rules), up to {self.widest / 1e6:.2f} mm'
                         + (': ' + ', '.join(self.examples[:4]) if self.examples else ''))
        return lines


def _strip(name: str) -> str:
    return name.lstrip('/')


def is_ground_name(name: str) -> bool:
    return bool(_GROUND_RE.match(_strip(name)))


def pour_net_candidates(board) -> List[str]:
    """Nets that can be poured: ground-like names first, then by pad count."""
    counts: Dict[str, int] = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            n = str(pad.GetNetname())
            if n:
                counts[n] = counts.get(n, 0) + 1
    return sorted(counts, key=lambda n: (not is_ground_name(n), -counts[n], n))


def default_pour_net(board) -> str:
    cands = pour_net_candidates(board)
    for n in cands:
        if is_ground_name(n):
            return n
    return ''


def count_pours(board) -> int:
    return sum(1 for z in board.Zones() if str(z.GetZoneName()) == POUR_NAME)


def export_dsn_without_pours(board, dsn: str, use_isolation: bool = True):
    """Specctra export that leaves out a previous place-news pour (exported as
    a plane, it would let Freerouting skip the tracks of its net); the zones
    are put back right after, the board is unchanged."""
    from . import specctra
    held = [z for z in board.Zones() if str(z.GetZoneName()) == POUR_NAME]
    for z in held:
        board.Remove(z)
    try:
        return specctra.export_dsn(board, dsn, use_isolation=use_isolation)
    finally:
        for z in held:
            board.Add(z)


def remove_pours(board) -> int:
    """Detach the zones of a previous place-news pour (kept alive for Undo)."""
    n = 0
    for z in list(board.Zones()):
        try:
            if str(z.GetZoneName()) == POUR_NAME:
                board.Remove(z)
                n += 1
        except Exception:
            continue
    return n


def _item_infos(board, pcbnew, layer_id: int, layer_name: str):
    """(item, PadInfo, label) for every copper item on the layer."""
    from .board_model import _NetClassClearance, _component_classes, _group_chain, _lib_id, \
        net_class_names, pad_rule_info
    nc_clearance = _NetClassClearance(board)
    out = []
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        side = 'B' if fp.GetLayer() == pcbnew.B_Cu else 'F'
        lib = _lib_id(fp)
        try:
            sheet = str(fp.GetSheetname())      # as board_model / specctra read it
        except Exception:
            sheet = ''
        classes = _component_classes(fp)
        groups = _group_chain(fp)
        for pad in fp.Pads():
            try:
                if not pad.IsOnLayer(layer_id):
                    continue
                info = pad_rule_info(pcbnew, pad, pad.GetAttribute(), side, ref, lib, sheet,
                                     classes, groups, nc_clearance)
                out.append((pad, info, f'{ref}.{pad.GetNumber()}'))
            except Exception:
                continue
    for t in board.GetTracks():
        try:
            names = net_class_names(t)
            if t.GetClass() == 'PCB_VIA':
                if not t.IsOnLayer(layer_id):
                    continue
                info = via_item(str(t.GetNetname()), names, nc_clearance(names))
                out.append((t, info, f'via {t.GetNetname()}'))
            elif t.GetLayer() == layer_id:
                info = track_item(str(t.GetNetname()), names, nc_clearance(names), layer_name)
                out.append((t, info, f'track {t.GetNetname()}'))
        except Exception:
            continue
    # Copper zones of other nets (teardrops included) and copper graphics.
    for z in board.Zones():
        try:
            if z.GetIsRuleArea() or str(z.GetZoneName()) == POUR_NAME or not z.IsOnLayer(layer_id):
                continue
            names = net_class_names(z)
            info = PadInfo(net_name=str(z.GetNetname()), netclasses=names,
                           nc_clearance=nc_clearance(names), pad_type='', layers=(layer_name,),
                           kind='zone')
            out.append((z, info, f'zone {z.GetNetname() or z.GetZoneName()}'.strip()))
        except Exception:
            continue
    graphics = list(board.GetDrawings())
    for fp in board.GetFootprints():
        try:
            graphics += list(fp.GraphicalItems())
        except Exception:
            pass
    for g in graphics:
        try:
            if g.GetClass() not in ('PCB_SHAPE', 'PCB_TEXT', 'PCB_TEXTBOX') or g.GetLayer() != layer_id:
                continue
            net = str(g.GetNetname()) if hasattr(g, 'GetNetname') else ''
            names = net_class_names(g) if net else ('Default',)
            info = track_item(net, names, nc_clearance(names), layer_name)
            out.append((g, info, f'copper graphic {net}'.strip()))
        except Exception:
            continue
    return out


def _cut_shape(pcbnew, item, layer_id: int, distance: int, cut) -> None:
    """Add the item's copper, grown by `distance`, to the cut-outs. A zone
    counts with its whole outline (its fill may change)."""
    if item.GetClass() == 'ZONE':
        poly = pcbnew.SHAPE_POLY_SET()
        outline = item.Outline()
        for i in range(outline.OutlineCount()):
            poly.AddOutline(outline.Outline(i))
        poly.Inflate(int(distance), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
        cut.BooleanAdd(poly)
    else:
        item.TransformShapeToPolygon(cut, layer_id, int(distance), MAX_ERROR, pcbnew.ERROR_OUTSIDE)


def _board_outline(board, pcbnew):
    poly = pcbnew.SHAPE_POLY_SET()
    try:
        ok = board.GetBoardPolygonOutlines(poly, True)
    except TypeError:
        ok = board.GetBoardPolygonOutlines(poly)
    if not ok or poly.OutlineCount() == 0:
        bb = board.GetBoardEdgesBoundingBox()
        poly = pcbnew.SHAPE_POLY_SET()
        poly.NewOutline()
        for x, y in ((bb.GetLeft(), bb.GetTop()), (bb.GetRight(), bb.GetTop()),
                     (bb.GetRight(), bb.GetBottom()), (bb.GetLeft(), bb.GetBottom())):
            poly.Append(int(x), int(y))
    return poly


def convex_hull(points: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Monotone chain convex hull (counter-clockwise, no repeated point)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b) -> int:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower: List[Tuple[int, int]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[int, int]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _hulls(cut, pcbnew):
    """Each cluster of cut-outs becomes its convex hull: a clean, rounded
    isolation boundary, with no pour fingers between the parts of a group
    (e.g. a primary side) and no islands inside it."""
    out = pcbnew.SHAPE_POLY_SET()
    for i in range(cut.OutlineCount()):
        chain = cut.Outline(i)
        pts = []
        for k in range(chain.PointCount()):
            v = chain.CPoint(k)
            pts.append((int(v.x), int(v.y)))
        hull = convex_hull(pts)
        if len(hull) < 3:
            continue
        out.NewOutline()
        for x, y in hull:
            out.Append(x, y)
    out.Simplify()
    return out


def add_pour(board, net_name: str, layer: str = 'F.Cu', rules: Optional[RuleSet] = None,
             fill: bool = True) -> PourReport:
    """Pour `net_name` over the whole board on `layer` with isolation cut-outs."""
    import pcbnew
    from .board_model import _NetClassClearance, load_board_rules, net_class_names
    report = PourReport(net=net_name, layer=layer)
    net = board.FindNet(net_name) if net_name else None
    if net is None or not net_name:
        report.error = f'net "{net_name}" not found'
        return report
    layer_id = board.GetLayerID(layer)
    if layer_id < 0 or not board.IsLayerEnabled(layer_id):
        report.error = f'layer {layer} not on the board'
        return report
    if rules is None:
        try:
            rules = load_board_rules(board)
        except Exception:
            rules = RuleSet()
    report.replaced = remove_pours(board)

    nc_clearance = _NetClassClearance(board)
    names = ('Default',)
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if str(pad.GetNetname()) == net_name:
                names = net_class_names(pad)
                break
        else:
            continue
        break
    pour_info = PadInfo(net_name=net_name, netclasses=names, nc_clearance=nc_clearance(names),
                        pad_type='', layers=(layer,), kind='zone')
    try:
        min_clr = int(board.GetDesignSettings().m_MinClearance)
    except Exception:
        min_clr = 0

    outline = _board_outline(board, pcbnew)
    cut = pcbnew.SHAPE_POLY_SET()
    for item, info, label in _item_infos(board, pcbnew, layer_id, layer):
        if info.net_name == net_name:
            continue
        need = rules.requirement(info, pour_info)
        plain = max(info.nc_clearance, pour_info.nc_clearance, min_clr)
        if need <= plain:
            continue                       # the zone filler keeps this one
        try:
            _cut_shape(pcbnew, item, layer_id, int(need) + MARGIN, cut)
        except Exception:
            continue
        report.cutouts += 1
        if need > report.widest:
            report.widest = need
        if len(report.examples) < 6 and label not in report.examples:
            report.examples.append(f'{label} ({need / 1e6:.2f} mm)')
    if cut.OutlineCount():
        cut.Simplify()                 # union: one outline per cluster of items
        outline.BooleanSubtract(_hulls(cut, pcbnew))

    for i in range(outline.OutlineCount()):
        zone = pcbnew.ZONE(board)
        zone.SetLayer(layer_id)
        zone.SetNetCode(net.GetNetCode())
        zone.SetZoneName(POUR_NAME)
        # Fill the zone's own outline in place: ZONE::SetOutline() would take
        # ownership of a polygon Python also frees.
        poly = zone.Outline()
        poly.RemoveAllContours()
        poly.AddOutline(outline.Outline(i))
        for h in range(outline.HoleCount(i)):
            poly.AddHole(outline.Hole(i, h))
        zone.SetAssignedPriority(0)
        zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
        zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
        board.Add(zone)
        report.zones += 1
    if fill and report.zones:
        try:
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
            report.filled = True
        except Exception:
            report.filled = False
    return report
