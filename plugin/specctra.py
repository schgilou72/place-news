"""Specctra (DSN/SES) round trip with Freerouting, carrying our isolation rules.

KiCad exports the board as a Specctra .dsn, Freerouting routes it and writes a
.ses session that KiCad imports back. Two things are fixed in the .dsn before
routing:

* KiCad exports every rule area as a full routing keep-out, even areas that
  only restrict footprints or are placement areas. Each exported keep-out is
  matched to its rule area and kept, narrowed to wires/vias, or dropped
  according to the area's "keep out tracks / vias" settings.
* Freerouting only knows one clearance per net class. The clearance /
  creepage requirements derived from the net classes and the custom rules
  (.kicad_dru) are written as Specctra ``class_class`` clearance rules, and
  each class clearance is raised to what its own nets need between them.

Creepage is turned into a plain clearance (straight-line distance), which is
never longer than the creepage path KiCad checks: the routed board is on the
safe side, and KiCad's DRC remains the final judge.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .rules import PadInfo, RuleSet, track_item, via_item

# ---------------------------------------------------------------------------
# DSN reader / writer (round-trip safe for what KiCad writes)
# ---------------------------------------------------------------------------


class Quoted(str):
    """A token that was written between double quotes."""


def _tokenize_dsn(text: str) -> List[Any]:
    tokens: List[Any] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\r\n':
            i += 1
        elif c in '()':
            tokens.append(c)
            i += 1
        elif c == '"':
            # '(string_quote ")' declares the quote character itself.
            if len(tokens) >= 2 and tokens[-1] == 'string_quote' and tokens[-2] == '(':
                tokens.append('"')
                i += 1
                continue
            j = text.find('"', i + 1)
            if j < 0:
                j = n
            tokens.append(Quoted(text[i + 1:j]))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse_dsn(text: str) -> List[Any]:
    tokens = _tokenize_dsn(text)
    pos = 0

    def parse_list() -> List[Any]:
        nonlocal pos
        out: List[Any] = []
        while pos < len(tokens):
            tok = tokens[pos]
            pos += 1
            if tok == '(' and not isinstance(tok, Quoted):
                out.append(parse_list())
            elif tok == ')' and not isinstance(tok, Quoted):
                return out
            else:
                out.append(tok)
        return out

    top = parse_list()
    if len(top) != 1 or not isinstance(top[0], list):
        raise ValueError('not a Specctra design file')
    return top[0]


def _fmt_token(tok: Any) -> str:
    if isinstance(tok, Quoted):
        return f'"{tok}"'
    s = str(tok)
    if s == '' or (any(ch in s for ch in ' ()') and s != '"'):
        return f'"{s}"'
    return s


def write_dsn(tree: List[Any]) -> str:
    lines: List[str] = []

    def emit(node: List[Any], depth: int) -> None:
        pad = '  ' * depth
        atoms = []
        children = []
        for item in node:
            (children if isinstance(item, list) else atoms).append(item)
        head = '(' + ' '.join(_fmt_token(a) for a in atoms)
        if not children:
            lines.append(pad + head + ')')
            return
        lines.append(pad + head)
        for item in node:
            if isinstance(item, list):
                emit(item, depth + 1)
        lines.append(pad + ')')

    emit(tree, 0)
    return '\n'.join(lines) + '\n'


def _find(node: List[Any], name: str) -> Optional[List[Any]]:
    for item in node:
        if isinstance(item, list) and item and item[0] == name:
            return item
    return None


def _find_all(node: List[Any], name: str) -> List[List[Any]]:
    return [item for item in node if isinstance(item, list) and item and item[0] == name]


# ---------------------------------------------------------------------------
# Rule-area keep-outs
# ---------------------------------------------------------------------------

@dataclass
class RuleAreaInfo:
    """What the DSN fix needs to know about a board rule area."""
    layers: Tuple[str, ...]           # copper layer names, e.g. ('F.Cu',)
    bbox_nm: Tuple[int, int, int, int]  # board coordinates (y down)
    no_tracks: bool
    no_vias: bool
    name: str = ''


def _dsn_units_per_nm(tree: List[Any]) -> float:
    unit = _find(tree, 'unit')
    u = str(unit[1]).lower() if unit and len(unit) > 1 else 'um'
    return {'um': 1e-3, 'mm': 1e-6, 'mil': 1 / 25_400.0, 'inch': 1 / 25_400_000.0,
            'cm': 1e-7}.get(u, 1e-3)


def _keepout_bbox(ko: List[Any]) -> Optional[Tuple[str, Tuple[float, float, float, float]]]:
    for shape in ko[1:]:
        if isinstance(shape, list) and shape and shape[0] in ('polygon', 'rect', 'path'):
            layer = str(shape[1])
            vals = []
            for v in shape[3:] if shape[0] in ('polygon', 'path') else shape[2:]:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            xs = vals[0::2]
            ys = vals[1::2]
            if xs and ys:
                return layer, (min(xs), min(ys), max(xs), max(ys))
    return None


def _exported_type(area: RuleAreaInfo) -> str:
    """Keep-out type KiCad's exporter writes for a rule area."""
    if area.no_tracks == area.no_vias:
        return 'keepout'            # both, or neither (the case fixed here)
    return 'wire_keepout' if area.no_tracks else 'via_keepout'


def fix_rule_area_keepouts(tree: List[Any], areas: Sequence[RuleAreaInfo],
                           tol_nm: int = 2000) -> Dict[str, int]:
    """KiCad exports a rule area that forbids neither tracks nor vias (it only
    keeps out footprints / pads, or is a placement area) as a full keep-out.
    Remove those; leave the others as exported. Each exported keep-out is
    matched to a rule area by type, layer and bounding box, and each (area,
    layer) is used once. Returns counts: kept, wire_only, via_only, removed,
    unmatched (unmatched keep-outs are left in place)."""
    stats = {'kept': 0, 'wire_only': 0, 'via_only': 0, 'removed': 0, 'unmatched': 0}
    structure = _find(tree, 'structure')
    if structure is None:
        return stats
    k = _dsn_units_per_nm(tree)
    tol = tol_nm * k
    used: set = set()
    new_items: List[Any] = []
    label = {'keepout': 'kept', 'wire_keepout': 'wire_only', 'via_keepout': 'via_only'}
    for item in structure:
        if not (isinstance(item, list) and item and item[0] in label):
            new_items.append(item)
            continue
        found = _keepout_bbox(item)
        match = None
        if found:
            layer, (x1, y1, x2, y2) = found
            for idx, a in enumerate(areas):
                if (idx, layer) in used or layer not in a.layers or _exported_type(a) != item[0]:
                    continue
                bx1, by1, bx2, by2 = a.bbox_nm
                # DSN y axis points up: y_dsn = -y_board
                cand = (bx1 * k, -by2 * k, bx2 * k, -by1 * k)
                if all(abs(p - q) <= tol for p, q in zip((x1, y1, x2, y2), cand)):
                    match = a
                    used.add((idx, layer))
                    break
        if match is None:
            stats['unmatched'] += 1
            new_items.append(item)
        elif not match.no_tracks and not match.no_vias:
            stats['removed'] += 1
        else:
            stats[label[item[0]]] += 1
            new_items.append(item)
    structure[:] = new_items
    return stats


# ---------------------------------------------------------------------------
# Isolation rules as class clearances
# ---------------------------------------------------------------------------
#
# Freerouting keeps one clearance value per pair of net classes (its
# "clearance matrix"), shared by wires, vias and pins. A class's own rule
# sets the clearance between nets of that class; a 'class_class' rule sets the
# value between two classes. Without one, two classes get the largest of their
# own clearances and the default class's.
#
# KiCad's exporter also writes '(clearance x (type smd_smd))' in the structure
# rules, which gives the SMD pads of the default class a separate clearance
# class that class_class rules do not reach. It is removed whenever the
# default class takes part in a raised or class_class rule (its only purpose
# is to hide pad-to-pad warnings inside fine-pitch footprints).

DSN_DEFAULT_CLASS = 'kicad_default'


@dataclass
class NetRouteInfo:
    """Rule-evaluation view of a net: its classes and its pads. The entry
    NO_NET gathers the pads without net (never routed)."""
    name: str
    netclasses: Tuple[str, ...]
    nc_clearance: int
    pads: set = field(default_factory=set)        # distinct PadInfo profiles
    routable: bool = True


NO_NET = ''


def _routing_items(net: NetRouteInfo, rules: RuleSet,
                   name: Optional[str] = None) -> Tuple[List[PadInfo], List[PadInfo]]:
    """(tracks on every copper layer + a via, distinct pads) of a net."""
    pads = sorted(net.pads, key=repr)
    if not net.routable:
        return [], pads
    if name is None:
        name = net.name if 'net_name' in rules.attrs else ''
    routed = [track_item(name, net.netclasses, net.nc_clearance, layer)
              for layer in (rules.copper_layers or ('F.Cu', 'B.Cu'))]
    routed.append(via_item(name, net.netclasses, net.nc_clearance))
    return routed, pads


def net_pair_requirement(a: Tuple[List[PadInfo], List[PadInfo]],
                         b: Tuple[List[PadInfo], List[PadInfo]], rules: RuleSet,
                         memo: Optional[Dict[Tuple[PadInfo, PadInfo], int]] = None) -> int:
    """Largest distance the rules ask between new copper (tracks, vias) of
    one net and any copper of the other. Pad-to-pad is left out: pads are
    already placed."""
    ra, pa = a
    rb, pb = b
    best = 0

    def req(x: PadInfo, y: PadInfo) -> int:
        if memo is None:
            return rules.requirement(x, y)
        v = memo.get((x, y))
        if v is None:
            v = memo[(x, y)] = rules.requirement(x, y)
        return v

    def same_layer(x: PadInfo, y: PadInfo) -> bool:
        if x.kind == 'track' and not y.on_layer(x.layers[0]):
            return False
        if y.kind == 'track' and not x.on_layer(y.layers[0]):
            return False
        return True

    for x in ra:
        for y in list(rb) + list(pb):
            if same_layer(x, y):
                best = max(best, req(x, y))
    for y in rb:
        for x in pa:
            if same_layer(x, y):
                best = max(best, req(x, y))
    return best


def track_info(net: NetRouteInfo, layer: str = 'F.Cu') -> PadInfo:
    return track_item(net.name, net.netclasses, net.nc_clearance, layer)


@dataclass
class ClassRulesReport:
    classes: Dict[str, int] = field(default_factory=dict)     # class -> clearance (nm), raised ones
    pairs: Dict[Tuple[str, str], int] = field(default_factory=dict)  # class pair -> nm
    original: Dict[str, int] = field(default_factory=dict)    # class -> exported clearance (nm)
    removed_smd_rule: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.classes or self.pairs)


def _rule_clearance(rule: Optional[List[Any]]) -> Optional[List[Any]]:
    """The untyped '(clearance x)' entry of a rule node."""
    if rule is None:
        return None
    for item in rule[1:]:
        if isinstance(item, list) and item and item[0] == 'clearance' and \
                not any(isinstance(t, list) for t in item[2:]):
            return item
    return None


def apply_isolation_classes(tree: List[Any], nets: Dict[str, NetRouteInfo],
                            rules: RuleSet) -> ClassRulesReport:
    """Write the isolation requirements into the DSN network: raise each
    class's own clearance to what its nets need between them, and add
    class_class rules where a pair of classes needs another value than
    Freerouting would derive."""
    report = ClassRulesReport()
    network = _find(tree, 'network')
    structure = _find(tree, 'structure')
    if network is None:
        return report
    k = _dsn_units_per_nm(tree)

    classes = _find_all(network, 'class')
    names = [str(c[1]) for c in classes]
    canon = rules.net_canonicalizer()

    # Distinct rule views of the nets of each class, with net counts. Nets
    # the rules cannot tell apart share one view.
    def view_key(n: NetRouteInfo) -> Any:
        return (canon(n.name) if n.routable else None, n.netclasses, n.nc_clearance,
                frozenset(n.pads), n.routable)

    views: Dict[str, List[Tuple[Any, int]]] = {}
    items: Dict[Any, Tuple[List[PadInfo], List[PadInfo]]] = {}

    def add_view(cls_name: str, counts: Dict[Any, int], n: NetRouteInfo) -> None:
        key = view_key(n)
        counts[key] = counts.get(key, 0) + 1
        if key not in items:
            items[key] = _routing_items(n, rules, canon(n.name) if n.routable else '')

    for cls, name in zip(classes, names):
        counts: Dict[Any, int] = {}
        for t in cls[2:]:
            if isinstance(t, list):
                continue
            n = nets.get(str(t))
            if n is not None and n.routable:
                add_view(name, counts, n)
        views[name] = list(counts.items())

    # Pads without net belong to Freerouting's default class.
    no_net = nets.get(NO_NET)
    if no_net is not None and no_net.pads:
        if DSN_DEFAULT_CLASS not in views:
            names.append(DSN_DEFAULT_CLASS)       # no class node: Freerouting's own default
            views[DSN_DEFAULT_CLASS] = []
        counts = dict(views[DSN_DEFAULT_CLASS])
        add_view(DSN_DEFAULT_CLASS, counts, no_net)
        views[DSN_DEFAULT_CLASS] = list(counts.items())

    pair_cache: Dict[Tuple[Any, Any], int] = {}
    memo: Dict[Tuple[PadInfo, PadInfo], int] = {}

    def need(ka: Any, kb: Any) -> int:
        key = (ka, kb) if repr(ka) <= repr(kb) else (kb, ka)
        v = pair_cache.get(key)
        if v is None:
            v = net_pair_requirement(items[key[0]], items[key[1]], rules, memo)
            pair_cache[key] = v
        return v

    def worst(ca: str, cb: str) -> Optional[int]:
        best = None
        for ka, na in views.get(ca, []):
            for kb, nb in views.get(cb, []):
                if ca == cb and ka == kb and na < 2:
                    continue          # a net is never isolated from itself
                if not items[ka][0] and not items[kb][0]:
                    continue          # nothing to route on either side
                v = need(ka, kb)
                best = v if best is None else max(best, v)
        return best

    # Own clearances
    own: Dict[str, int] = {}
    for cls, name in zip(classes, names):
        rule = _find(cls, 'rule')
        clr = _rule_clearance(rule)
        current = int(round(float(clr[1]) / k)) if clr is not None else 0
        report.original[name] = current
        w = worst(name, name)
        value = current if w is None else max(current, w)
        if value != current:
            if rule is None:
                rule = ['rule']
                cls.append(rule)
            if clr is None:
                rule.append(['clearance', _num(value * k)])
            else:
                clr[1] = _num(value * k)
            report.classes[name] = value
        own[name] = value

    struct_rule = _find(structure, 'rule') if structure is not None else None
    struct_clr = _rule_clearance(struct_rule)
    default_own = own.get(DSN_DEFAULT_CLASS)
    if default_own is None:
        default_own = int(round(float(struct_clr[1]) / k)) if struct_clr is not None else 0

    # Class pairs
    network[:] = [i for i in network if not (isinstance(i, list) and i and i[0] == 'class_class')]
    default_touched = DSN_DEFAULT_CLASS in report.classes
    for ia in range(len(names)):
        for ib in range(ia + 1, len(names)):
            a, b = names[ia], names[ib]
            w = worst(a, b)
            if w is None:
                continue
            implicit = max(default_own, own.get(a, 0), own.get(b, 0))
            if w != implicit:
                network.append(['class_class', ['classes', a, b],
                                ['rule', ['clearance', _num(w * k)]]])
                report.pairs[(a, b)] = w
                if DSN_DEFAULT_CLASS in (a, b):
                    default_touched = True

    if default_touched and struct_rule is not None:
        kept = [i for i in struct_rule if not (
            isinstance(i, list) and i and i[0] == 'clearance'
            and any(isinstance(t, list) and t and t[0] == 'type' for t in i[2:]))]
        if len(kept) != len(struct_rule):
            struct_rule[:] = kept
            report.removed_smd_rule = True
        if struct_clr is not None and DSN_DEFAULT_CLASS in own:
            struct_clr[1] = _num(own[DSN_DEFAULT_CLASS] * k)
    return report


def _num(v: float) -> str:
    s = f'{v:.4f}'.rstrip('0').rstrip('.')
    return s or '0'


# ---------------------------------------------------------------------------
# Tools on the user's machine
# ---------------------------------------------------------------------------

def _java_major(java: str) -> int:
    try:
        out = subprocess.run([java, '-version'], capture_output=True, text=True, timeout=30,
                             **_no_window())
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
        if m:
            major = int(m.group(1))
            return int(m.group(2)) if major == 1 and m.group(2) else major
    except Exception:
        pass
    return 0


MIN_JAVA = 25          # Freerouting 2.x is built for Java 25


def find_java(preferred: str = '') -> Tuple[str, int]:
    """Best Java runtime available: (path, major version). Freerouting 2.x
    needs Java 25 or later."""
    cands: List[str] = []
    if preferred:
        cands.append(preferred)
    home = os.environ.get('JAVA_HOME')
    if home:
        cands.append(os.path.join(home, 'bin', 'java.exe' if os.name == 'nt' else 'java'))
    w = shutil.which('java')
    if w:
        cands.append(w)
    patterns = []
    if os.name == 'nt':
        for root in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')):
            patterns += [os.path.join(root, d, '*', 'bin', 'java.exe')
                         for d in ('Java', 'Eclipse Adoptium', 'Microsoft', 'Zulu', 'Amazon Corretto',
                                   'BellSoft', 'OpenJDK')]
    elif sys.platform == 'darwin':
        patterns += ['/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java',
                     os.path.expanduser('~/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java'),
                     '/opt/homebrew/opt/openjdk*/bin/java', '/usr/local/opt/openjdk*/bin/java']
    else:
        patterns += ['/usr/lib/jvm/*/bin/java', os.path.expanduser('~/.sdkman/candidates/java/*/bin/java')]
    for p in patterns:
        cands += sorted(glob.glob(p), reverse=True)
    # macOS: /usr/bin/java is a stub that pops up an installer when no JDK exists.
    mac_stub_only = sys.platform == 'darwin' and not glob.glob('/Library/Java/JavaVirtualMachines/*')
    best = ('', 0)
    seen = set()
    for c in cands:
        if not c or c in seen or not os.path.isfile(c):
            continue
        if mac_stub_only and c == '/usr/bin/java':
            continue
        seen.add(c)
        v = _java_major(c)
        if v > best[1]:
            best = (c, v)
    return best


def find_freerouting_jar(preferred: str = '') -> str:
    """A Freerouting jar: the one configured, one next to the plugin, or the
    one installed by KiCad's Freerouting plugin (Plugin and Content Manager)."""
    if preferred and os.path.isfile(preferred):
        return preferred
    here = os.path.dirname(os.path.abspath(__file__))
    patterns = [os.path.join(here, 'freerouting*.jar'), os.path.join(here, 'jar', 'freerouting*.jar')]
    docs = []
    for env in ('KICAD10_3RD_PARTY', 'KICAD9_3RD_PARTY'):
        if os.environ.get(env):
            docs.append(os.environ[env])
    user_docs = os.path.join(os.path.expanduser('~'), 'Documents', 'KiCad')
    for ver in ('10.0', '9.0'):
        docs.append(os.path.join(user_docs, ver, '3rdparty'))
        docs.append(os.path.join(os.path.expanduser('~'), '.local', 'share', 'kicad', ver, '3rdparty'))
    for d in docs:
        patterns.append(os.path.join(d, 'plugins', '*', '**', 'freerouting*.jar'))
    for p in patterns:
        hits = sorted(glob.glob(p, recursive=True), reverse=True)
        if hits:
            return hits[0]
    return ''


def find_kicad_cli() -> str:
    names = ['kicad-cli.exe', 'kicad-cli'] if os.name == 'nt' else ['kicad-cli']
    dirs = [os.path.dirname(sys.executable)]
    if sys.platform == 'darwin':
        dirs += ['/Applications/KiCad/KiCad.app/Contents/MacOS']
    if os.name == 'nt':
        for root in (os.environ.get('ProgramFiles', r'C:\Program Files'),):
            dirs += sorted(glob.glob(os.path.join(root, 'KiCad', '*', 'bin')), reverse=True)
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return shutil.which('kicad-cli') or ''


def _no_window() -> Dict[str, Any]:
    if os.name == 'nt':
        return {'creationflags': 0x08000000}   # CREATE_NO_WINDOW
    return {}


# ---------------------------------------------------------------------------
# Board side
# ---------------------------------------------------------------------------

def rule_areas_of(board) -> List[RuleAreaInfo]:
    """Board rule areas as the DSN exporter sees them (layer names as in the
    board, which the DSN uses)."""
    out: List[RuleAreaInfo] = []
    for zone in board.Zones():
        try:
            if not zone.GetIsRuleArea():
                continue
            bb = zone.Outline().BBox()
            layers = tuple(str(board.GetLayerName(lid)) for lid in zone.GetLayerSet().CuStack())
            out.append(RuleAreaInfo(
                layers=layers,
                bbox_nm=(bb.GetX(), bb.GetY(), bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight()),
                no_tracks=bool(zone.GetDoNotAllowTracks()),
                no_vias=bool(zone.GetDoNotAllowVias()),
                name=str(zone.GetZoneName())))
        except Exception:
            continue
    return out


def net_route_infos(board, rules: Optional[RuleSet] = None) -> Dict[str, NetRouteInfo]:
    """Net classes and distinct pad views of every net with pads; pads
    without net (unconnected pins, mounting holes) under NO_NET."""
    import pcbnew
    from .board_model import (_NetClassClearance, _component_classes, _group_chain, _lib_id,
                              pad_rule_info)
    from .isolation import _profile_key
    clearance = _NetClassClearance(board)
    attrs = rules.attrs if rules is not None else frozenset()
    canon = rules.net_canonicalizer() if rules is not None else None
    out: Dict[str, NetRouteInfo] = {}
    for fp in board.GetFootprints():
        try:
            ref = str(fp.GetReference())
            side = 'B' if fp.GetLayer() == pcbnew.B_Cu else 'F'
            sheet = str(fp.GetSheetname())
        except Exception:
            ref, side, sheet = '', 'F', ''
        classes = _component_classes(fp)
        groups = _group_chain(fp)
        lib_id = _lib_id(fp)
        for pad in fp.Pads():
            name = str(pad.GetNetname())
            try:
                attr = pad.GetAttribute()
            except Exception:
                attr = None
            info = pad_rule_info(pcbnew, pad, attr, side, ref, lib_id, sheet, classes, groups,
                                 clearance)
            net = out.get(name)
            if net is None:
                net = out[name] = NetRouteInfo(name=name, netclasses=info.netclasses,
                                               nc_clearance=info.nc_clearance,
                                               routable=bool(name))
            net.pads.add(_profile_key(info, attrs, canon))
    return out


@dataclass
class ExportReport:
    dsn: str
    keepouts: Dict[str, int]
    classes: ClassRulesReport
    rules: Optional[RuleSet]


def export_dsn(board, dsn_path: str, use_isolation: bool = True) -> ExportReport:
    """Export the board to Specctra DSN and apply our fixes."""
    import pcbnew
    from .board_model import load_board_rules
    if not pcbnew.ExportSpecctraDSN(board, dsn_path):
        raise RuntimeError('KiCad could not export the Specctra DSN file')
    with open(dsn_path, 'r', encoding='utf-8') as f:
        tree = parse_dsn(f.read())
    keepouts = fix_rule_area_keepouts(tree, rule_areas_of(board))
    rules = None
    classes = ClassRulesReport()
    if use_isolation:
        rules = load_board_rules(board)
        classes = apply_isolation_classes(tree, net_route_infos(board, rules), rules)
    with open(dsn_path, 'w', encoding='utf-8') as f:
        f.write(write_dsn(tree))
    return ExportReport(dsn=dsn_path, keepouts=keepouts, classes=classes, rules=rules)


def outer_layers_only(src: str, dst: str) -> List[str]:
    """Write a copy of a DSN file whose inner copper layers are planes
    ('power' layers, which Freerouting does not route on): a trial on the two
    outer layers. Planes exported on them still connect their nets. Returns
    the names of the inner layers (empty for a two-layer board)."""
    with open(src, 'r', encoding='utf-8') as f:
        tree = parse_dsn(f.read())
    structure = _find(tree, 'structure')
    layers = _find_all(structure, 'layer') if structure is not None else []
    inner = layers[1:-1]
    for layer in inner:
        kind = _find(layer, 'type')
        if kind is not None and len(kind) > 1:
            kind[1] = 'power'
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(write_dsn(tree))
    return [str(layer[1]) for layer in inner]


@dataclass
class RouteResult:
    ok: bool
    ses: str
    log: List[str]
    unrouted: Optional[int] = None
    violations: Optional[int] = None
    cancelled: bool = False
    returncode: Optional[int] = None
    timed_out: bool = False


_SCORE_RE = re.compile(r'\((\d+) unrouted and (\d+) violations?\)')


def run_freerouting(java: str, jar: str, dsn: str, ses: str, passes: int = 20,
                    threads: int = 0, poll: Optional[Callable[[str, float], bool]] = None,
                    timeout_s: float = 3600.0, via_cost: Optional[int] = None) -> RouteResult:
    """Run Freerouting headless. `poll(last_line, elapsed)` is called about
    ten times a second; return False to cancel."""
    cmd = [java, '-jar', jar, '-de', dsn, '-do', ses, '-mp', str(max(1, passes))]
    if threads > 0:
        cmd += ['-mt', str(threads)]
    # Headless, and no usage statistics sent from the user's machine.
    cmd += ['--gui.enabled=false', '--usage_and_diagnostic_data.disable_analytics=true']
    # Freerouting >= 2.4 may fan out SMD pins with the default class's vias
    # when a class's own via does not fit; those vias carry the default
    # clearance, which would defeat the isolation rules. (Older versions
    # ignore this setting with a warning.)
    cmd += ['--router.fanout.fallback_to_board_vias=false']
    if via_cost:
        cmd += [f'--router.scoring.via_costs={int(via_cost)}']
    if os.path.exists(ses):
        os.remove(ses)
    log: List[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace', bufsize=1, **_no_window())
    import threading
    lines: List[str] = []

    def reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip())

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    start = time.time()
    cancelled = timed_out = False
    while proc.poll() is None:
        time.sleep(0.1)
        elapsed = time.time() - start
        if poll is not None and not poll(lines[-1] if lines else '', elapsed):
            cancelled = True
        if elapsed > timeout_s:
            cancelled = timed_out = True
        if cancelled:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
    t.join(timeout=5)
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except Exception:
        pass
    log = list(lines)
    unrouted = violations = None
    for line in reversed(log):
        m = _SCORE_RE.search(line)
        if m:
            unrouted, violations = int(m.group(1)), int(m.group(2))
            break
    ok = (not cancelled) and proc.returncode == 0 and os.path.isfile(ses)
    return RouteResult(ok=ok, ses=ses, log=log, unrouted=unrouted, violations=violations,
                       cancelled=cancelled, returncode=proc.returncode, timed_out=timed_out)


def _drop_section(text: str, name: str) -> str:
    """Remove every top-level-or-nested '(name ...)' list from S-expression text."""
    out = []
    i, n = 0, len(text)
    pat = re.compile(r'\(\s*' + re.escape(name) + r'(?=[\s()])')
    while i < n:
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        depth, j, in_q = 0, m.start(), False
        while j < n:
            c = text[j]
            if c == '"':
                in_q = not in_q
            elif not in_q:
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
            j += 1
        i = j
    return ''.join(out)


def strip_ses_placement(ses_path: str) -> None:
    """Keep only the routes of a session file: the parts stay where they are
    (KiCad would otherwise re-apply the session's placement, and warn about
    references the DSN export had to rename, such as duplicates)."""
    with open(ses_path, 'r', encoding='utf-8') as f:
        text = f.read()
    stripped = _drop_section(text, 'placement')
    if stripped != text:
        with open(ses_path, 'w', encoding='utf-8') as f:
            f.write(stripped)


def import_ses(board, ses_path: str, keep_placement: bool = True) -> bool:
    """Import the routes of a session file. KiCad replaces every unlocked
    track and via of the board with the session's wiring."""
    import pcbnew
    if keep_placement:
        strip_ses_placement(ses_path)
    return bool(pcbnew.ImportSpecctraSES(board, ses_path))


# ---------------------------------------------------------------------------
# KiCad DRC on a copy of the board
# ---------------------------------------------------------------------------

@dataclass
class DrcSummary:
    counts: Dict[str, int] = field(default_factory=dict)
    between_parts: Dict[str, int] = field(default_factory=dict)   # clearance / creepage between items
    inside_parts: Dict[str, int] = field(default_factory=dict)    # both items in one footprint
    rule_area: Dict[str, int] = field(default_factory=dict)       # reported against a rule-area outline
    unconnected: int = 0
    report_path: str = ''
    error: str = ''                                                # DRC could not run


def _owner(desc: str) -> str:
    if desc.startswith('Rule area'):
        return '<rule area>'
    m = re.search(r' of (\S+)', desc)
    return m.group(1) if m else desc


def drc_summary(board, kicad_cli: str, workdir: Optional[str] = None,
                timeout_s: float = 600.0) -> DrcSummary:
    """Save a copy of the board next to copies of its project and rules,
    run 'kicad-cli pcb drc' on it (zones refilled) and summarise the
    isolation-related results. Exclusions saved in the project are honoured.
    The copy uses the project settings saved on disk."""
    import pcbnew
    if not kicad_cli:
        return DrcSummary(error='kicad-cli was not found')
    src = str(board.GetFileName() or '')
    if not src:
        return DrcSummary(error='the board has never been saved, so its project rules are unknown')
    base = os.path.splitext(src)[0]
    work = workdir or tempfile.mkdtemp(prefix='place-news-drc-')
    os.makedirs(work, exist_ok=True)
    name = os.path.basename(base) or 'board'
    copy = os.path.join(work, name + '.kicad_pcb')
    for ext in ('.kicad_pro', '.kicad_dru'):
        if os.path.isfile(base + ext):
            shutil.copyfile(base + ext, os.path.join(work, name + ext))
    if not pcbnew.SaveBoard(copy, board, True):
        return DrcSummary(error='could not save a copy of the board')
    out = os.path.join(work, name + '.drc.json')
    try:
        proc = subprocess.run([kicad_cli, 'pcb', 'drc', '--format', 'json', '--units', 'mm',
                               '--severity-error', '--severity-warning', '--refill-zones',
                               '-o', out, copy],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout_s, **_no_window())
    except subprocess.TimeoutExpired:
        return DrcSummary(error=f'kicad-cli did not finish within {timeout_s:.0f} s')
    except OSError as e:
        return DrcSummary(error=f'kicad-cli could not be started ({e})')
    try:
        with open(out, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-1:] or ['']
        return DrcSummary(error=f'kicad-cli failed (exit code {proc.returncode}) {tail[0]}'.strip())
    summary = DrcSummary(report_path=out)
    for v in data.get('violations', []):
        t = v.get('type', '?')
        summary.counts[t] = summary.counts.get(t, 0) + 1
        if t not in ('clearance', 'creepage', 'hole_clearance', 'shorting_items'):
            continue
        owners = [_owner(i.get('description', '')) for i in v.get('items', [])]
        if '<rule area>' in owners:
            bucket = summary.rule_area
        elif len(owners) == 2 and owners[0] == owners[1] and not owners[0].startswith(('Track', 'Via')):
            bucket = summary.inside_parts
        else:
            bucket = summary.between_parts
        bucket[t] = bucket.get(t, 0) + 1
    summary.unconnected = len(data.get('unconnected_items', []))
    return summary
