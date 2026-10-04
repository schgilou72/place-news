"""Sourcing: find every BOM line at the distributors, choose where to buy it,
propose replacements that keep the ratings, and build the order lists.

For each line (designators, value, package, MPN when the parts have one):

* with an MPN (or a distributor part number in the fields): the offers of
  every configured distributor for that exact part;
* without: a search by value and package (and voltage, dielectric... when
  the value or the fields give them), keeping only candidates whose value
  and package match;
* the offer is chosen in the user's order of distributors (first one with
  enough stock) or the cheapest; the quantity to order respects the
  minimum order and the multiple, and the price is the one of the break
  actually reached — so the line total holds no surprise;
* parts that are short, at the end of their life or not recommended get
  replacements: the distributors' own suggestions plus a search by
  parameters, filtered for power electronics (same package, voltage /
  current / power at least as high, tolerance as tight, dielectric as
  good, X/Y safety class kept, temperature range covered).

Pure Python (the distributor clients do the network calls).
"""
from __future__ import annotations

import csv
import html
import io
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .bom import BomLine, DISTRIBUTORS, compact_refs
from .distributors import Distributor, DistributorError, Offer, same_mpn

RISKY = ('obsolete', 'eol', 'nrnd')

# ---------------------------------------------------------------------------
# Quantities with units
# ---------------------------------------------------------------------------

_SI = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'μ': 1e-6, 'm': 1e-3, '': 1.0,
       'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9}
_QTY_RE = re.compile(r'(?<![\w./])(\d+(?:[.,]\d+)?)\s*([pnuµμmkKMG]?)\s*(Ω|ohms?|R|F|H|V|A|W|%)?',
                     re.IGNORECASE)


_MULT = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'μ': 1e-6, 'm': 1e-3, 'k': 1e3, 'K': 1e3,
         'M': 1e6, 'G': 1e9, 'R': 1.0, 'r': 1.0}


def parse_value(text: str, kind: str = '') -> Optional[float]:
    """Component value: '10k', '4k7', '0R1', '100nF', '2.2u', '470µF', '1M5'
    ('m' is milli, 'M' mega, as in KiCad)."""
    t = (text or '').strip().replace(',', '.')
    m = re.match(r'^(\d+)([RrkKMGpnuµμm])(\d+)(?![\d.])', t)          # 4k7, 0R1, 2n2
    if m:
        return float(f'{m.group(1)}.{m.group(3)}') * _MULT[m.group(2)]
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([pnuµμmkKMG]?)', t)
    if not m:
        return None
    return float(m.group(1)) * _MULT.get(m.group(2), 1.0)


def quantities(text: str) -> Dict[str, List[float]]:
    """Every quantity with a unit in a text: {'V': [50.0], 'F': [1e-7], ...}."""
    out: Dict[str, List[float]] = {}
    for m in _QTY_RE.finditer((text or '').replace('Ω', 'Ω ')):
        unit = (m.group(3) or '').upper()
        if not unit:
            continue
        unit = {'OHM': 'R', 'OHMS': 'R', 'Ω': 'R'}.get(unit, unit)
        num = float(m.group(1).replace(',', '.'))
        prefix = m.group(2) or ''
        mult = _SI.get(prefix, _SI.get(prefix.lower(), 1.0)) if unit != '%' else 1.0
        if prefix == 'M':
            # all-caps descriptions write milli as M: 150MA, 500MW, 100MV; mega
            # only makes sense for ohms (and 'mOhm' keeps its lower-case m)
            mult = 1e6 if unit == 'R' else 1e-3
        out.setdefault(unit, []).append(num * mult)
    frac = re.search(r'(\d+)\s*/\s*(\d+)\s*W\b', text or '')
    if frac and float(frac.group(2)):
        out.setdefault('W', []).append(float(frac.group(1)) / float(frac.group(2)))
    return out


_DIELECTRIC_RANK = {'Y5V': 0, 'Z5U': 0, 'X5R': 2, 'X6S': 2, 'X7S': 3, 'X7R': 3, 'X7T': 3,
                    'X8R': 4, 'X8L': 4, 'C0G': 5, 'NP0': 5, 'NPO': 5, 'COG': 5}
_SAFETY_RANK = {'X2': 1, 'X1': 2, 'Y4': 3, 'Y3': 3, 'Y2': 4, 'Y1': 5}


@dataclass
class Ratings:
    value: Optional[float] = None       # resistance / capacitance / inductance
    voltage: Optional[float] = None
    current: Optional[float] = None
    power: Optional[float] = None
    tolerance: Optional[float] = None   # percent
    dielectric: str = ''
    safety: str = ''                    # X1, X2, Y1, Y2...
    t_min: Optional[float] = None
    t_max: Optional[float] = None
    rds_on: Optional[float] = None


def _first(vals: Optional[List[float]]) -> Optional[float]:
    return vals[0] if vals else None


def _param(params: Dict[str, str], *patterns: str) -> str:
    for name, value in params.items():
        n = name.lower()
        if any(re.search(p, n) for p in patterns):
            return value
    return ''


def ratings_of(kind: str, text: str = '', params: Optional[Dict[str, str]] = None,
               value_text: str = '') -> Ratings:
    """Ratings from parameters (preferred), a description and a value text."""
    params = params or {}
    r = Ratings()
    q = quantities(' '.join((value_text, text)))
    unit = {'R': 'R', 'C': 'F', 'L': 'H'}.get(kind)
    if value_text and kind in ('R', 'C', 'L'):
        r.value = parse_value(value_text, kind)
    pv = _param(params, r'^resistance', r'^capacitance', r'^inductance', r'^résistance', r'^capacité')
    if pv and kind in ('R', 'C', 'L'):
        r.value = parse_value(pv, kind) or r.value
    if r.value is None and unit:
        r.value = _first(q.get(unit))
    v = _param(params, r'voltage.*rated', r'^voltage rating', r'rated voltage', r'voltage - rated',
               r'drain to source voltage', r'^vds', r'vdss', r'voltage - dc reverse', r'vrrm',
               r'repetitive reverse voltage', r'^vr\b', r'reverse voltage',
               r'collector.*emitter.*breakdown', r'^voltage$', r'tension')
    r.voltage = _first(quantities(v).get('V')) if v else _first(q.get('V'))
    c = _param(params, r'current.*continuous', r'continuous drain current',
               r'current - average rectified', r'^current rating', r'rated current',
               r'current - collector \(ic\)', r'^id\b', r'^if\b', r'forward current',
               r'current.*saturation', r'courant')
    r.current = _first(quantities(c).get('A')) if c else (_first(q.get('A')) if kind not in ('R', 'C') else None)
    p = _param(params, r'^power', r'power \(watts\)', r'power dissipation', r'puissance')
    r.power = _first(quantities(p).get('W')) if p else _first(q.get('W'))
    t = _param(params, r'^tolerance')
    tq = quantities(t).get('%') if t else q.get('%')
    r.tolerance = _first(tq)
    blob = ' '.join([text, value_text] + [f'{k} {v}' for k, v in params.items()]).upper()
    for d in sorted(_DIELECTRIC_RANK, key=len, reverse=True):
        if re.search(rf'(?<![A-Z0-9]){d}(?![A-Z0-9])', blob):
            r.dielectric = d
            break
    # safety classes: 'Y2', 'X1Y2', 'X1/Y2', '_Y2' (a letter before it is part of a word)
    for s in ('Y1', 'Y2', 'X1', 'X2', 'Y3', 'Y4'):
        if re.search(rf'(?<![A-Z]){s}(?!\d)', blob):
            r.safety = s
            break
    temp = _param(params, r'operating temperature', r'temperature range')
    nums = [float(x) for x in re.findall(r'[-+]?\d+(?:\.\d+)?', temp or '')]
    if len(nums) >= 2:
        r.t_min, r.t_max = min(nums[:2]), max(nums[:2])
    rds = _param(params, r'^rds on', r'^rds\(on\)', r'^r ds\(on\)', r'drain.source on.state resistance',
                 r'drain source on resistance', r'^on-resistance')
    if rds:
        r.rds_on = _first(quantities(rds).get('R'))
        if r.rds_on and r.rds_on > 1e3:             # 'MOHM' in capitals: milliohms
            r.rds_on /= 1e9
    return r


def ratings_of_line(line: BomLine) -> Ratings:
    fields = {k: v for k, v in line.fields.items()
              if k.lower() not in ('reference', 'value', 'footprint', 'datasheet', 'description')}
    return ratings_of(line.kind, ' '.join(fields.values()), fields, line.value)


def ratings_of_offer(kind: str, offer: Offer) -> Ratings:
    return ratings_of(kind, offer.description, offer.params)


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

_CHIP = ('0201', '0402', '0603', '0805', '1008', '1206', '1210', '1806', '1812', '2010', '2220',
         '2512')
# usual lead spacing of radial electrolytic capacitors by can diameter (mm)
_RADIAL_PITCH = {4.0: 1.5, 5.0: 2.0, 6.3: 2.5, 8.0: 3.5, 10.0: 5.0, 12.5: 5.0, 13.0: 5.0,
                 16.0: 7.5, 18.0: 7.5, 20.0: 10.0, 22.0: 10.0, 25.0: 10.0}


def package_tokens(text: str, kicad: bool = False) -> set:
    """Normalised package codes in a text: '0805 (2012 Metric)' -> {'0805'},
    '8-SOIC (0.154", 3.90mm Width)' -> {'SOIC-8'}, 'TO-220-3 Full Pack' ->
    {'TO-220F-3'}, 'TO-252-3, DPak (2 Leads + Tab)' -> {'TO-252-2'}.
    kicad: the text is a KiCad footprint name (its TO-252 / TO-263 lead
    counts leave the tab out, distributors count it)."""
    t = (text or '').upper().replace('_', '-')
    out = set()
    for code in _CHIP:
        if re.search(rf'(?<![\d.]){code}(?![\d.])', t) and not re.search(rf'{code}\s*METRIC', t):
            out.add(code)
    isolated = bool(re.search(r'FULL\s*-?\s*PACK|ISOLATED\s+TAB|\bITO-|\bTO-?220F', t))
    plus_tab = re.search(r'(\d+)\s*LEADS?\s*\+\s*TAB', t)
    explicit: Dict[str, set] = {}
    defaults: Dict[str, set] = {}
    for m in re.finditer(r'\b(I?)TO-?(\d{1,3})([A-Z]{0,3})(?:-(\d{1,2}))?', t):
        ito, num, suffix, leads = m.groups()
        if num == '3' and suffix.startswith('P'):
            out.add('TO-3P' + ('F' if 'F' in suffix[1:] or isolated else ''))
            continue
        iso = 'F' if (ito or suffix.startswith('F') or isolated) else ''
        n = int(num)
        base = f'TO-{n}{iso}'
        if leads:
            count = int(leads)
            if n in (252, 263) and not kicad:
                count = int(plus_tab.group(1)) if plus_tab else count - 1
            explicit.setdefault(base, set()).add(count)
        else:
            defaults.setdefault(base, set()).add(2 if (suffix == 'AC' and n == 220) else 3)
    for base in set(explicit) | set(defaults):
        for count in explicit.get(base) or defaults.get(base, ()):
            out.add(f'{base}-{count}')
    for m in re.finditer(r'\bSOT-?(\d{2,3})(?:-(\d))?', t):
        pins = m.group(2) or '3'                 # SOT-23 is SOT-23-3
        out.add(f'SOT-{m.group(1)}-{pins}')
    wide = bool(re.search(r'WIDE|7\.5\d?\s*MM\s*WIDTH|0\.29\d"|0\.300"', t))
    for fam in ('SOIC', 'TSSOP', 'HTSSOP', 'MSOP', 'SSOP', 'QFN', 'DFN', 'LQFP', 'TQFP', 'DIP', 'SOP'):
        pre = '[A-Z]{0,2}' if fam in ('DIP', 'QFN', 'DFN') else ''     # PDIP, SPDIP, VFQFN, WDFN...
        for m in re.finditer(rf'\b(\d+)-{pre}{fam}\b|\b{pre}{fam}-?(\d+)(W?)\b', t):
            n = m.group(1) or m.group(2)
            w = 'W' if fam == 'SOIC' and (m.group(3) or wide) else ''
            out.add(f'{fam}-{n}{w}')
    for code in ('SMA', 'SMB', 'SMC', 'SOD-123', 'SOD-123F', 'SOD-323', 'SOD-523', 'DO-41', 'DO-15',
                 'DO-201AD', 'DO-214AA', 'DO-214AB', 'DO-214AC', 'DPAK', 'D2PAK'):
        if re.search(rf'\b{re.escape(code)}\b', t):
            out.add(code)
    alias = {'DO-214AC': 'SMA', 'DO-214AA': 'SMB', 'DO-214AB': 'SMC', 'DPAK': 'TO-252-2',
             'D2PAK': 'TO-263-2'}
    for a, b in alias.items():
        if a in out:
            out.add(b)
    return out


@dataclass
class PackageInfo:
    codes: set = field(default_factory=set)
    mount: str = ''                  # SMD | THT
    shape: str = ''                  # radial | elec | disc | film | axial
    pitch: Optional[float] = None    # lead spacing, mm
    diameter: Optional[float] = None
    length: Optional[float] = None   # can height / body length, mm
    body: Optional[Tuple[float, float]] = None   # QFN / DFN / QFP body, mm (sorted)
    row: Optional[float] = None      # DIP row spacing, mm (7.62, 15.24...)


# families whose pin count does not fix the body size, nor the DIP row spacing
_BODY_FAMILIES = ('QFN', 'DFN', 'LQFP', 'TQFP')


def _has_family(codes: set, families: Sequence[str]) -> bool:
    return any(c.rsplit('-', 1)[0] in families for c in codes)


def _mm_values(text: str) -> List[float]:
    """Lengths in a text, in mm: '(5.00mm)', '5 mm', '0.197"'."""
    out = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)\s*MM', (text or '').upper())]
    if not out:
        out = [round(float(x) * 25.4, 2) for x in re.findall(r'(\d*\.\d+|\d+)\s*"', text or '')]
    return out


def line_package(footprint: str) -> PackageInfo:
    """What a KiCad footprint (standard libraries' naming) says of the part."""
    lib, _, name = footprint.rpartition(':')
    p = PackageInfo(codes=package_tokens(name, kicad=True))
    u = name.upper()
    if '_THT' in lib.upper() or re.search(r'AXIAL|RADIAL|DISC|-RECT-|_RECT_|DIP-|TO-(?:92|126|220|247|3P)', u):
        p.mount = 'THT'
    elif '_SMD' in lib.upper() or p.codes & set(_CHIP) or 'ELEC' in u:
        p.mount = 'SMD'
    num = r'(\d+(?:\.\d+)?)'
    m = re.search(rf'_P{num}MM', u)
    if m:
        p.pitch = float(m.group(1))
    if 'RADIAL' in u:
        p.shape = 'radial'
        m = re.search(rf'_D{num}MM', u)
        p.diameter = float(m.group(1)) if m else None
        m = re.search(rf'_H{num}MM', u)
        p.length = float(m.group(1)) if m else None
    elif 'ELEC' in u:
        p.shape = 'elec'
        m = re.search(rf'ELEC_{num}X{num}', u)
        if m:
            p.diameter, p.length = float(m.group(1)), float(m.group(2))
    elif 'DISC' in u:
        p.shape = 'disc'
        m = re.search(rf'_D{num}MM', u)
        p.diameter = float(m.group(1)) if m else None
    elif '_RECT_' in u or 'C_RECT' in u:
        p.shape = 'film'
        m = re.search(rf'_L{num}MM', u)
        p.length = float(m.group(1)) if m else None
    elif 'AXIAL' in u:
        p.shape = 'axial'
        m = re.search(rf'_L{num}MM', u)
        p.length = float(m.group(1)) if m else None
        m = re.search(rf'_D{num}MM', u)
        p.diameter = float(m.group(1)) if m else None
    if _has_family(p.codes, _BODY_FAMILIES):
        m = re.search(rf'_{num}X{num}MM', u)                  # QFN-32-1EP_5x5mm_P0.5mm
        if m:
            p.body = tuple(sorted((float(m.group(1)), float(m.group(2)))))
    if _has_family(p.codes, ('DIP',)):
        m = re.search(rf'_W{num}MM', u)                       # DIP-28_W15.24mm
        if m:
            p.row = float(m.group(1))
    return p


def offer_package(o: Offer) -> PackageInfo:
    """What a distributor says of a part's package."""
    pkg_params = {k: v for k, v in o.params.items()
                  if re.search(r'package|case|mount|size|dimension|diameter|height|length|spacing|pitch',
                               k, re.I)}
    text = ' '.join([o.package, o.description] + list(pkg_params.values()))
    p = PackageInfo(codes=package_tokens(text))
    u = text.upper()
    mount = ' '.join(v for k, v in pkg_params.items() if re.search(r'mount', k, re.I)).upper()
    if re.search(r'THROUGH HOLE|\bTHT\b', mount or u) or u.startswith('PLUGIN') or 'PLUGIN,' in u:
        p.mount = 'THT'
    elif re.search(r'SURFACE MOUNT|SMD|SMT', mount or u) or p.codes & set(_CHIP):
        p.mount = 'SMD'
    for word, shape in (('RADIAL', 'radial'), ('AXIAL', 'axial'), ('DISC', 'disc'), ('BOX', 'film')):
        if word in u:
            p.shape = shape
            break
    for k, v in pkg_params.items():
        kl = k.lower()
        vals = _mm_values(v)
        if not vals:
            continue
        if 'spacing' in kl or 'pitch' in kl:
            p.pitch = vals[-1]
        elif 'diameter' in kl:
            p.diameter = vals[-1]
        elif 'height' in kl or kl == 'length':
            p.length = vals[-1]
        elif 'size' in kl or 'dimension' in kl:
            if 'DIA' in v.upper():
                p.diameter = vals[0] if len(vals) == 1 else p.diameter
                m = re.search(r'\((\d+(?:\.\d+)?)MM\s*X\s*(\d+(?:\.\d+)?)MM\)', v.upper())
                if m:
                    p.diameter, p.length = float(m.group(1)), float(m.group(2))
    m = re.search(r'P\s*=\s*(\d+(?:\.\d+)?)\s*MM', u)
    if m and p.pitch is None:
        p.pitch = float(m.group(1))
    m = re.search(r'\bD\s*(\d+(?:\.\d+)?)\s*[X*×]\s*L?\s*(\d+(?:\.\d+)?)', u)
    if m and p.diameter is None:
        p.diameter, p.length = float(m.group(1)), float(m.group(2))
    # body size and row spacing, from the package texts only
    pk = ' '.join([o.package] + [v for k, v in pkg_params.items() if re.search(r'package|case', k, re.I)])
    pk = pk.upper()
    num = r'(\d+(?:\.\d+)?)'
    if _has_family(p.codes, _BODY_FAMILIES):
        m = (re.search(rf'\({num}\s*[X×*]\s*{num}\s*(?:MM)?\)', pk)          # 32-QFN (5x5), QFN-32(5x5)
             or re.search(rf'{num}\s*MM\s*[X×*]\s*{num}\s*MM', pk))           # 5.00mm x 5.00mm
        if m:
            p.body = tuple(sorted((float(m.group(1)), float(m.group(2)))))
    if _has_family(p.codes, ('DIP',)):
        m = re.search(rf'{num}\s*MM\)', pk) or re.search(r'0\.(\d{3})\s*"', pk)
        if m:
            p.row = float(m.group(1)) if 'MM' in m.group(0) else round(int(m.group(1)) * 0.0254, 2)
    return p


def package_match(line: PackageInfo, offer: PackageInfo) -> Optional[bool]:
    """True: the part fits the footprint; False: it does not; None: cannot tell."""
    if line.codes:
        if offer.codes:
            if not line.codes & offer.codes:
                return False
            # same code, another body (QFN-32 5x5 / 7x7) or row spacing (DIP-28 300 / 600 mil)
            if line.body and offer.body and any(abs(a - b) > 0.25 for a, b in zip(line.body, offer.body)):
                return False
            if line.row and offer.row and abs(line.row - offer.row) > 0.3:
                return False
            return True
        if line.mount and offer.mount and line.mount != offer.mount:
            return False              # a through-hole footprint cannot take an SMD part
        return None
    if not line.mount or not offer.mount:
        return None
    if offer.mount != line.mount:
        return False
    if line.shape in ('radial', 'elec'):
        if not line.diameter or offer.diameter is None:
            return None
        if abs(offer.diameter - line.diameter) > 0.3:
            return False
        if line.shape == 'radial':
            lp = line.pitch or _RADIAL_PITCH.get(line.diameter)
            op = offer.pitch or _RADIAL_PITCH.get(round(offer.diameter, 1))
            if lp is None or op is None:
                return None
            if abs(lp - op) > 0.3:
                return False
        if line.length and offer.length and offer.length > line.length + 0.5:
            return False
        return True
    if line.shape in ('disc', 'film'):
        if not line.pitch or offer.pitch is None:
            return None
        if abs(offer.pitch - line.pitch) > 0.3:
            return False
        if line.shape == 'disc' and line.diameter and offer.diameter and offer.diameter > line.diameter + 0.5:
            return False
        if line.shape == 'film' and line.length and offer.length and offer.length > line.length + 0.5:
            return False
        return True
    if line.shape == 'axial':
        if offer.shape and offer.shape != 'axial':
            return False
        if line.length and offer.length:
            return offer.length <= line.length + 0.5
        return None
    return None


def same_package(footprint: str, offer: Offer) -> Optional[bool]:
    """Does the offered part fit this footprint? (True / False / None: unknown)"""
    if ':' not in footprint:
        footprint = ':' + footprint
    return package_match(line_package(footprint), offer_package(offer))


# ---------------------------------------------------------------------------
# Replacement rules (power electronics)
# ---------------------------------------------------------------------------

def replacement_issues(kind: str, orig: Ratings, cand: Ratings) -> List[str]:
    """Why a candidate cannot replace the original (empty: it can). What
    the original requires must be stated by the candidate: an unknown
    voltage, value, tolerance or dielectric is refused, not assumed."""
    issues = []
    if kind in ('R', 'C', 'L') and orig.value:
        if not cand.value:
            issues.append('value unknown')
        elif abs(cand.value - orig.value) > 1e-6 * orig.value:
            issues.append('other value')
    for attr, label, unit in (('voltage', 'voltage', 'V'), ('current', 'current', 'A'),
                              ('power', 'power', 'W')):
        o, c = getattr(orig, attr), getattr(cand, attr)
        if not o:
            continue
        if c is None:
            issues.append(f'{label} unknown')
        elif c + 1e-12 < o:
            issues.append(f'{label} {c:g} {unit} < {o:g} {unit}')
    if orig.tolerance:
        if cand.tolerance is None:
            issues.append('tolerance unknown')
        elif cand.tolerance > orig.tolerance + 1e-9:
            issues.append(f'tolerance {cand.tolerance:g} % > {orig.tolerance:g} %')
    if orig.dielectric:
        if not cand.dielectric:
            issues.append('dielectric unknown')
        elif _DIELECTRIC_RANK.get(cand.dielectric, 0) < _DIELECTRIC_RANK.get(orig.dielectric, 0):
            issues.append(f'dielectric {cand.dielectric} below {orig.dielectric}')
    if orig.safety:
        if not cand.safety:
            issues.append(f'no {orig.safety} safety class')
        elif orig.safety.startswith('Y') and not cand.safety.startswith('Y'):
            issues.append(f'{cand.safety} instead of {orig.safety} (Y class needed)')
        elif _SAFETY_RANK.get(cand.safety, 0) < _SAFETY_RANK.get(orig.safety, 0):
            issues.append(f'safety class {cand.safety} below {orig.safety}')
    if orig.t_max is not None and cand.t_max is not None and cand.t_max < orig.t_max:
        issues.append(f'max temperature {cand.t_max:g} °C < {orig.t_max:g} °C')
    if orig.t_min is not None and cand.t_min is not None and cand.t_min > orig.t_min:
        issues.append(f'min temperature {cand.t_min:g} °C > {orig.t_min:g} °C')
    if orig.rds_on and cand.rds_on and cand.rds_on > orig.rds_on * 1.05:
        issues.append(f'Rds(on) {cand.rds_on:g} Ω > {orig.rds_on:g} Ω')
    return issues


# ---------------------------------------------------------------------------
# Search queries
# ---------------------------------------------------------------------------

_PART_NAME_RE = re.compile(r'^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-_/.+#]{3,}$')
_GENERIC_VALUE_RE = re.compile(r'^\d+(?:[.,]\d+)?\s*[pnuµμmkKMGRr]?\d*\s*(?:Ω|ohms?|F|H|V|A|W)?$')


def value_looks_like_part(line: BomLine) -> bool:
    v = line.value.strip()
    return bool(_PART_NAME_RE.match(v)) and not _GENERIC_VALUE_RE.match(v) \
        and v.upper() not in (line.footprint.split(':')[-1].upper(),)


def _fmt_si(v: float, unit: str) -> str:
    for mult, p in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k'), (1.0, ''), (1e-3, 'm'), (1e-6, 'u'),
                    (1e-9, 'n'), (1e-12, 'p')):
        if abs(v) >= mult * 0.999:
            n = v / mult
            return f'{n:g}{p}{unit}'
    return f'{v:g}{unit}'


def keyword_query(line: BomLine, ratings: Ratings) -> str:
    """Search words for a line without MPN (packages only when they are
    standard codes: 0805, SOT-23-3, TO-220...)."""
    lp = line_package(line.footprint)
    pkg = ' '.join(sorted(lp.codes)) or {'radial': 'radial', 'elec': 'SMD', 'disc': 'disc',
                                          'film': 'film', 'axial': 'axial'}.get(lp.shape, '')
    words: List[str] = []
    if line.kind == 'R' and ratings.value:
        words = [_fmt_si(ratings.value, 'Ω' if ratings.value < 1000 else '').replace('Ω', ' ohm'),
                 pkg, 'resistor']
        if ratings.tolerance:
            words.insert(1, f'{ratings.tolerance:g}%')
    elif line.kind == 'C' and ratings.value:
        words = [_fmt_si(ratings.value, 'F')]
        if ratings.voltage:
            words.append(f'{ratings.voltage:g}V')
        if ratings.dielectric:
            words.append(ratings.dielectric)
        if ratings.safety:
            words.append(ratings.safety)
        words += [pkg, 'capacitor']
    elif line.kind == 'L' and ratings.value:
        words = [_fmt_si(ratings.value, 'H'), pkg, 'inductor']
    else:
        words = [line.value, pkg]
    return ' '.join(w for w in words if w).strip()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Alternate:
    offer: Offer
    why: str


@dataclass
class LineResult:
    line: BomLine
    need: int
    offers: List[Offer] = field(default_factory=list)        # the part itself
    candidates: List[Offer] = field(default_factory=list)    # proposals (no MPN)
    alternates: List[Alternate] = field(default_factory=list)
    chosen: Optional[Offer] = None
    status: str = 'not found'      # ok | short | risk | proposed | not found
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    # set by finalize(): lines buying the same part share one order
    shared: Optional[Tuple[int, float, float]] = None      # (qty for this line, unit, total)
    extra: float = 0.0
    group_qty: int = 0
    merged_with: List[str] = field(default_factory=list)

    @property
    def mpn(self) -> str:
        return self.chosen.mpn if self.chosen else self.line.mpn

    @property
    def manufacturer(self) -> str:
        return self.chosen.manufacturer if self.chosen else self.line.manufacturer

    @property
    def order(self) -> Optional[Tuple[int, float, float]]:
        """(quantity ordered for this line, unit price, line total)."""
        if self.shared is not None:
            return self.shared
        return self.chosen.cost(self.need) if self.chosen else None

    @property
    def extra_cost(self) -> float:
        """What the minimum order / multiple costs on top of the parts needed."""
        if self.shared is not None:
            return self.extra
        o = self.order
        if not o:
            return 0.0
        qty, unit, total = o
        return max(0.0, total - self.need * unit)

    @property
    def package_warning(self) -> str:
        """Set when the chosen part's package is known and does not fit the
        footprint (an MPN of another package variant, say)."""
        o = self.chosen
        if o is None or same_package(self.line.footprint, o) is not False:
            return ''
        theirs = o.package or ', '.join(sorted(offer_package(o).codes)) or 'another package'
        return f'check the package: {theirs} for footprint {self.line.package or self.line.footprint}'

    @property
    def lifecycle(self) -> str:
        states = [o.lifecycle for o in self.offers if o.lifecycle != 'unknown']
        for s in ('obsolete', 'eol', 'nrnd'):
            if s in states:
                return s
        return self.chosen.lifecycle if self.chosen else (states[0] if states else 'unknown')


@dataclass
class Settings:
    boards: int = 1
    spare_percent: float = 10.0
    strategy: str = 'priority'          # priority | cheapest
    order: Tuple[str, ...] = DISTRIBUTORS
    search_without_mpn: bool = True
    alternates: bool = True
    r_tolerance: float = 1.0            # resistors without a tolerance: at most this (%)
    ceramic_min: str = 'X5R'            # chip capacitors without a dielectric: at least this


def requirements(line: BomLine, s: Settings) -> Ratings:
    """What a part must meet for this line: the schematic's figures, plus
    sensible defaults (1 % resistors, no Y5V / Z5U chip capacitors)."""
    r = ratings_of_line(line)
    if line.kind == 'R' and r.tolerance is None and s.r_tolerance:
        r.tolerance = s.r_tolerance
    if line.kind == 'C' and not r.dielectric and s.ceramic_min and \
            line_package(line.footprint).codes & set(_CHIP):
        r.dielectric = s.ceramic_min
    return r


def need_for(line: BomLine, s: Settings) -> int:
    base = line.qty * max(1, s.boards)
    return base + int(math.ceil(base * max(0.0, s.spare_percent) / 100.0))


def choose(offers: Sequence[Offer], need: int, s: Settings) -> Optional[Offer]:
    """The offer to buy: in the user's order (first with enough stock for
    the quantity actually ordered), or the cheapest line total; never an
    obsolete part when another exists."""
    usable = [o for o in offers if o.prices]
    if not usable:
        return None
    fine = [o for o in usable if o.lifecycle != 'obsolete'] or usable
    stocked = [o for o in fine if (o.stock or 0) >= o.order_qty(need)]
    pool = stocked or fine

    def total(o: Offer) -> float:
        c = o.cost(need)
        return c[2] if c else float('inf')
    if s.strategy == 'priority':
        rank = {d: i for i, d in enumerate(s.order)}
        return min(pool, key=lambda o: (rank.get(o.distributor, 99), total(o)))
    return min(pool, key=total)


def _matches_line(line: BomLine, r_line: Ratings, offer: Offer) -> bool:
    if same_package(line.footprint, offer) is not True:      # unknown is not good enough
        return False
    r_off = ratings_of_offer(line.kind, offer)
    return not replacement_issues(line.kind, r_line, r_off)


class Sourcer:
    """Runs the searches over the configured distributors."""

    def __init__(self, distributors: Sequence[Distributor], settings: Settings,
                 progress: Optional[Callable[[int, int, str], bool]] = None):
        self.dists = [d for d in distributors if d.configured()]
        self.s = settings
        self.progress = progress
        self.cancelled = False

    def _each(self, fn, dists: Optional[Sequence[Distributor]] = None) -> Tuple[List[Offer], List[str]]:
        """Run fn(distributor) on every distributor in parallel."""
        dists = list(dists if dists is not None else self.dists)
        offers: List[Offer] = []
        errors: List[str] = []
        if not dists:
            return offers, errors
        with ThreadPoolExecutor(max_workers=len(dists)) as pool:
            futures = {pool.submit(fn, d): d for d in dists}
            for fut, d in futures.items():
                try:
                    offers += fut.result() or []
                except DistributorError as e:
                    errors.append(str(e))
                except Exception as e:          # a bad answer must not stop the run
                    errors.append(f'{d.name}: {e}')
        return offers, errors

    def run(self, lines: Sequence[BomLine]) -> List[LineResult]:
        results = []
        for k, line in enumerate(lines):
            if self.progress and not self.progress(k, len(lines), line.designators):
                self.cancelled = True
                break
            try:
                results.append(self.source_line(line))
            except Exception as e:                  # one line must not lose the others
                res = LineResult(line=line, need=need_for(line, self.s))
                res.errors.append(f'search failed: {e}')
                results.append(res)
        finalize(results)
        return results

    def source_line(self, line: BomLine) -> LineResult:
        res = LineResult(line=line, need=need_for(line, self.s))
        r_query = ratings_of_line(line)              # what the schematic says (search words)
        r_line = requirements(line, self.s)          # what a part must meet
        mpn = line.mpn
        # 1. the part itself: by MPN, by distributor part numbers in the fields
        if mpn:
            offers, errs = self._each(lambda d: d.search_mpn(mpn, line.manufacturer))
            res.offers, res.errors = offers, errs
        by_name = {d.name: d for d in self.dists}
        for dist, sku in line.skus.items():
            d = by_name.get(dist)
            if d is None or any(o.distributor == dist and o.sku.upper() == sku.upper() for o in res.offers):
                continue
            try:
                got = d.lookup_sku(sku)
                res.offers += got
                if got and not mpn:
                    mpn = got[0].mpn
            except Exception as e:
                res.errors.append(str(e) if isinstance(e, DistributorError) else f'{dist}: {e}')
        if mpn and res.offers:
            # the same MPN at the distributors that were not asked by MPN yet
            missing = [d for d in self.dists if not any(o.distributor == d.name for o in res.offers)]
            if missing and not line.mpn:
                more, errs = self._each(lambda d: d.search_mpn(mpn), missing)
                res.offers += more
                res.errors += errs
        elif not mpn and line.kind not in ('R', 'C', 'L') and value_looks_like_part(line):
            # the value is a part name (IRF840, UC3843...)
            offers, errs = self._each(lambda d: d.search_mpn(line.value))
            if offers:
                res.offers = offers
                res.notes.append(f'found by its value "{line.value}" used as part number')
            res.errors += errs
        # 2. no part yet: a resistor, capacitor or inductor is searched by
        # value and package; other parts need a part number.
        if not res.offers and not mpn and self.s.search_without_mpn:
            if line.kind not in ('R', 'C', 'L'):
                res.notes.append('needs a part number (MPN field, or a part name as value)')
            elif r_line.value is None:
                res.notes.append(f'value "{line.value}" not understood: give an MPN')
            else:
                query = keyword_query(line, r_query)
                cands, errs = self._each(lambda d: d.search_keyword(query))
                res.errors += errs
                good = [c for c in cands if _matches_line(line, r_line, c)]
                res.candidates = sorted(good, key=lambda o: _rank(o, res.need))[:12]
                res.notes.append(f'no part number: proposals for "{query}"' if res.candidates
                                 else f'nothing found for "{query}"')
        # 3. choose — among the same MPN, a package variant that fits the
        # footprint first (the part is then flagged if none does)
        pool = res.offers or res.candidates
        fits = [o for o in pool if same_package(line.footprint, o) is not False]
        res.chosen = choose(fits, res.need, self.s) or choose(pool, res.need, self.s)
        res.status = status_of(res, res.chosen.order_qty(res.need) if res.chosen else res.need)
        # 4. replacements when the part is at risk or short
        if self.s.alternates and res.status in ('risk', 'short', 'not found') and (res.offers or mpn):
            self._alternates(res, r_query, r_line)
        return res

    def _alternates(self, res: LineResult, r_asked: Ratings, r_default: Ratings) -> None:
        line = res.line
        ref_offer = next((o for o in res.offers if o.params), res.offers[0] if res.offers else None)
        r_orig = ratings_of_offer(line.kind, ref_offer) if ref_offer else Ratings()
        for attr in ('voltage', 'current', 'power', 'tolerance', 'dielectric', 'safety', 'value',
                     't_min', 't_max'):
            if getattr(r_asked, attr) not in (None, ''):          # the schematic wins
                setattr(r_orig, attr, getattr(r_asked, attr))
            elif getattr(r_orig, attr) in (None, ''):              # else the defaults
                setattr(r_orig, attr, getattr(r_default, attr))
        cands: List[Offer] = []
        for o in res.offers:
            d = next((d for d in self.dists if d.name == o.distributor), None)
            if d is not None:
                try:
                    cands += d.alternates(o)
                except Exception as e:
                    res.errors.append(str(e) if isinstance(e, DistributorError) else f'{d.name}: {e}')
        # a search by parameters, as well
        query = keyword_query(line, r_orig) if line.kind in ('R', 'C', 'L') else ''
        if not query and ref_offer is not None:
            query = ' '.join(w for w in (_short_desc(ref_offer.description),
                                         ' '.join(sorted(line_package(line.footprint).codes))) if w)
        if query:
            more, errs = self._each(lambda d: d.search_keyword(query))
            cands += more
            res.errors += errs
        seen = {(o.mpn or '').upper() for o in res.offers}
        for c in sorted(cands, key=lambda o: _rank(o, res.need)):
            key = (c.mpn or c.sku).upper()
            if not key or key in seen or c.lifecycle in RISKY:
                continue
            if same_package(line.footprint, c) is not True:
                continue
            issues = replacement_issues(line.kind, r_orig, ratings_of_offer(line.kind, c))
            if issues:
                continue
            if (c.stock or 0) < c.order_qty(res.need):
                continue
            seen.add(key)
            why = c.note or 'same package and value, ratings kept'
            res.alternates.append(Alternate(c, why))
            if len(res.alternates) >= 6:
                break


def status_of(res: LineResult, qty: int) -> str:
    """ok / short (stock below the quantity ordered) / risk (end of life) /
    proposed (no MPN in the schematic) / not found."""
    if res.chosen is None:
        return 'not found'
    if not res.offers:
        return 'proposed'
    if res.lifecycle in RISKY:
        return 'risk'
    if (res.chosen.stock or 0) < qty:
        return 'short'
    return 'ok'


def finalize(results: Sequence[LineResult]) -> None:
    """Lines buying the same part (same distributor and part number) share
    one order: one minimum order, one multiple, one price break for the sum
    of their needs. The parts bought beyond the needs are counted on the
    first of these lines."""
    groups: Dict[Tuple[str, str], List[LineResult]] = {}
    for r in results:
        r.shared, r.extra, r.group_qty, r.merged_with = None, 0.0, 0, []
        if r.chosen is not None and r.chosen.prices:
            groups.setdefault((r.chosen.distributor, (r.chosen.sku or r.chosen.mpn).upper()), []).append(r)
    for rs in groups.values():
        offer = rs[0].chosen
        need = sum(r.need for r in rs)
        qty = offer.order_qty(need)
        unit = offer.unit_price(qty) or 0.0
        beyond = qty - need
        for k, r in enumerate(rs):
            share = r.need + (beyond if k == 0 else 0)
            r.shared = (share, unit, share * unit)
            r.extra = beyond * unit if k == 0 else 0.0
            r.group_qty = qty
            r.merged_with = [x.line.designators for x in rs if x is not r]
            r.status = status_of(r, qty)


def _short_desc(desc: str) -> str:
    words = re.findall(r'[A-Za-z0-9.\-+/%]+', desc or '')
    return ' '.join(words[:6])


def _rank(o: Offer, need: int):
    c = o.cost(need)
    return (o.lifecycle in RISKY, -(min(o.stock or 0, need)), c[2] if c else float('inf'))


# ---------------------------------------------------------------------------
# Fields to write into the components
# ---------------------------------------------------------------------------

def fields_for(res: LineResult) -> Dict[str, str]:
    """MPN, manufacturer, part numbers at every distributor carrying the
    chosen part, and its key parameters."""
    if res.chosen is None:
        return {}
    o = res.chosen
    out = {'MPN': o.mpn, 'Manufacturer': o.manufacturer}
    for x in res.offers + res.candidates + [a.offer for a in res.alternates]:
        if same_mpn(x.mpn, o.mpn) and x.sku:
            out.setdefault(x.distributor, x.sku)
    out[o.distributor] = o.sku
    r = ratings_of_offer(res.line.kind, o)
    if r.voltage:
        out['Voltage'] = f'{r.voltage:g} V'
    if r.current and res.line.kind not in ('R', 'C'):
        out['Current'] = f'{r.current:g} A'
    if r.power:
        out['Power'] = f'{r.power:g} W'
    if r.tolerance:
        out['Tolerance'] = f'{r.tolerance:g} %'
    if r.dielectric:
        out['Dielectric'] = r.dielectric
    if r.safety:
        out['Safety class'] = r.safety
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------------------
# Order lists and exports
# ---------------------------------------------------------------------------

def money(v: Optional[float], digits: int = 4) -> str:
    return '' if v is None else f'{v:.{digits}f}'


def order_lines(results: Sequence[LineResult]) -> Dict[str, List[LineResult]]:
    out: Dict[str, List[LineResult]] = {}
    for r in results:
        if r.chosen is not None:
            out.setdefault(r.chosen.distributor, []).append(r)
    return out


def totals(results: Sequence[LineResult]) -> Dict[str, float]:
    t: Dict[str, float] = {}
    for r in results:
        o = r.order
        if o:
            t[r.chosen.distributor] = t.get(r.chosen.distributor, 0.0) + o[2]
    return t


BOM_COLUMNS = ('Designators', 'Qty per board', 'Qty needed', 'Qty to order', 'Value', 'Package',
               'Footprint', 'Manufacturer', 'MPN', 'Distributor', 'Distributor PN',
               'Unit price (EUR)', 'Min. order', 'Multiple', 'Line total (EUR)',
               'Extra due to min. order (EUR)', 'Stock', 'Lifecycle', 'Lead time', 'Status',
               'Alternates', 'Link', 'Notes')


def bom_rows(results: Sequence[LineResult]) -> List[List[str]]:
    rows = []
    for r in results:
        o, order = r.chosen, r.order
        rows.append([
            r.line.designators, str(r.line.qty), str(r.need), str(order[0]) if order else '',
            r.line.value, r.line.package, r.line.footprint, r.manufacturer, r.mpn,
            o.distributor if o else '', o.sku if o else '', money(order[1]) if order else '',
            str(o.moq) if o else '', str(o.multiple) if o else '', money(order[2], 2) if order else '',
            money(r.extra_cost, 2) if order else '', '' if not o or o.stock is None else str(o.stock),
            r.lifecycle, o.lead_time if o else '', r.status,
            ' | '.join(f'{a.offer.mpn} ({a.offer.distributor} {a.offer.sku})' for a in r.alternates[:4]),
            o.url if o else '',
            '; '.join(([r.package_warning] if r.package_warning else [])
                      + ([f'ordered together with {", ".join(r.merged_with)} (same part)']
                         if r.merged_with else []) + r.notes + r.errors[:2])])
    return rows


def write_bom_csv(path: str, results: Sequence[LineResult], excel_fr: bool = True) -> None:
    """The whole BOM. excel_fr: ';' separators and decimal commas (French Excel)."""
    rows = bom_rows(results)
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter=';' if excel_fr else ',')
        w.writerow(BOM_COLUMNS)
        for row in rows:
            if excel_fr:
                row = [re.sub(r'^(-?\d+)\.(\d+)$', r'\1,\2', c) for c in row]
            w.writerow(row)


ORDER_COLUMNS = ('Distributor PN', 'Quantity', 'Customer reference', 'MPN', 'Manufacturer',
                 'Unit price (EUR)', 'Min. order', 'Multiple', 'Line total (EUR)')


@dataclass
class OrderLine:
    offer: Offer
    qty: int                        # what is bought (minimum order, multiple)
    need: int
    unit: float
    refs: List[str] = field(default_factory=list)

    @property
    def designators(self) -> str:
        return compact_refs(self.refs)

    @property
    def total(self) -> float:
        return self.qty * self.unit


def merged_orders(lines: Sequence[LineResult]) -> List[OrderLine]:
    """One row per part bought: lines sharing a part number are added up."""
    rows: Dict[Tuple[str, str], OrderLine] = {}
    for r in lines:
        o, order = r.chosen, r.order
        if not o or not order:
            continue
        key = (o.distributor, (o.sku or o.mpn).upper())
        row = rows.get(key)
        if row is None:
            rows[key] = OrderLine(offer=o, qty=r.group_qty or order[0], need=r.need, unit=order[1],
                                  refs=list(r.line.refs))
        else:
            row.need += r.need
            row.refs += r.line.refs
    return list(rows.values())


def order_csv(lines: Sequence[LineResult]) -> str:
    """One distributor's order list, for its BOM / list upload (map the
    columns once: part number, quantity, customer reference). The customer
    reference carries the designators: DigiKey, Mouser and Farnell print it
    on each bag (48 characters at most)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ORDER_COLUMNS)
    for row in merged_orders(lines):
        o = row.offer
        ref = row.designators
        if len(ref) > 48:
            ref = ref[:45] + '...'
        w.writerow([o.sku, row.qty, ref, o.mpn, o.manufacturer, money(row.unit), o.moq, o.multiple,
                    money(row.total, 2)])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_STATUS_LABEL = {'ok': 'OK', 'short': 'short', 'risk': 'end of life',
                 'proposed': 'proposed', 'not found': 'not found'}
_STATUS_COLOR = {'ok': '#2f9e44', 'short': '#f08c00', 'risk': '#e03131', 'proposed': '#1c7ed6',
                 'not found': '#868e96'}


def build_report(title: str, results: Sequence[LineResult], s: Settings, date: str,
                 rate_note: str = '', skipped: Sequence[str] = ()) -> str:
    e = html.escape
    tot = totals(results)
    grand = sum(tot.values())
    per_board = grand / max(1, s.boards)
    counts = {k: sum(1 for r in results if r.status == k) for k in _STATUS_LABEL}
    extra = sum(r.extra_cost for r in results)
    n_pkg = sum(1 for r in results if r.package_warning)
    rows = []
    for n, r in enumerate(results, start=1):
        o, order = r.chosen, r.order
        color = _STATUS_COLOR.get(r.status, '#868e96')
        offers = ''.join(
            f'<li>{e(x.distributor)} {e(x.sku)} ({e(x.mpn)}): {_thousands(x.stock)} in stock, '
            f'min. {x.moq}, {e(_price_text(x))}'
            f'{" — " + e(x.lifecycle) if x.lifecycle in RISKY else ""}</li>'
            for x in sorted(r.offers or r.candidates[:5], key=lambda x: x.distributor))
        alts = ''.join(f'<li>{e(a.offer.mpn)} — {e(a.offer.manufacturer)} ({e(a.offer.distributor)} '
                       f'{e(a.offer.sku)}, {_thousands(a.offer.stock)} in stock, '
                       f'{e(_price_text(a.offer))}): {e(a.why)}</li>' for a in r.alternates)
        notes = ''.join(f'<li class="muted">{e(t)}</li>' for t in r.notes + r.errors[:3])
        detail = ''
        if offers or alts or notes:
            detail = (f'<details><summary>details</summary><ul>{offers}</ul>'
                      + (f'<b>Replacements</b><ul>{alts}</ul>' if alts else '') + f'<ul>{notes}</ul></details>')
        rows.append(
            f'<tr><td class="num">{n}</td><td>{e(r.line.designators)}</td><td>{e(r.line.value)}</td>'
            f'<td>{e(r.line.package)}</td><td>{e(r.manufacturer)}<br><b>{e(r.mpn)}</b></td>'
            f'<td>{e(o.distributor) if o else "—"}<br><span class="muted">{e(o.sku) if o else ""}</span></td>'
            f'<td class="num">{r.line.qty}<br><span class="muted">{r.need}</span></td>'
            f'<td class="num">{order[0] if order else "—"}</td>'
            f'<td class="num">{money(order[1]) if order else "—"}</td>'
            f'<td class="num">{o.moq if o else ""}{f" ×{o.multiple}" if o and o.multiple > 1 else ""}</td>'
            f'<td class="num">{money(order[2], 2) if order else "—"}'
            f'{f"<br><span class=muted>+{r.extra_cost:.2f} min.</span>" if r.extra_cost >= 0.01 else ""}</td>'
            f'<td class="num">{_thousands(o.stock) if o else ""}</td>'
            f'<td><span class="tag" style="background:{color}">{e(_STATUS_LABEL.get(r.status, r.status))}'
            f'</span>{"<br><span class=muted>" + e(r.lifecycle) + "</span>" if r.lifecycle in RISKY else ""}'
            f'{"<br><span class=warn>⚠ " + e(r.package_warning) + "</span>" if r.package_warning else ""}'
            f'{detail}</td></tr>')
    dist_rows = ''.join(
        f'<tr><td>{e(d)}</td><td class="num">{len(lst)}</td><td class="num">{tot.get(d, 0):.2f}</td></tr>'
        for d, lst in sorted(order_lines(results).items()))
    skipped_note = (f'<p class="muted">Left out (DNP / not in BOM): {e(compact_refs(skipped))}</p>'
                    if skipped else '')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<style>
:root {{ --fg:#1d2326; --muted:#5f6b70; --line:#dfe5e8; --panel:#f6f8f9; }}
body {{ margin:0; background:#fff; color:var(--fg); font:14px/1.45 system-ui, sans-serif; }}
main {{ max-width:1280px; margin:0 auto; padding:20px 16px 40px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .sub, .muted {{ color:var(--muted); }}
.scores {{ display:flex; gap:12px; flex-wrap:wrap; margin:14px 0 18px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 16px; min-width:140px; }}
.card b {{ display:block; font-size:26px; }} .card span {{ color:var(--muted); font-size:13px; }}
table {{ border-collapse:collapse; width:100%; margin:8px 0 22px; }}
th, td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; }}
td.num, th.num {{ text-align:right; white-space:nowrap; }}
.tag {{ color:#fff; border-radius:6px; padding:1px 7px; font-size:12px; white-space:nowrap; }}
.warn {{ color:#c25e00; font-size:12px; }}
details summary {{ cursor:pointer; color:var(--muted); font-size:12px; margin-top:4px; }}
details ul {{ margin:4px 0 4px 16px; padding:0; font-size:12px; }}
.wrap {{ overflow-x:auto; }}
h2 {{ font-size:17px; margin:22px 0 6px; }}
</style></head><body><main>
<h1>{e(title)}</h1>
<p class="sub">place-news sourcing · prices of {e(date)} · {s.boards} board(s) + {s.spare_percent:g} % spare ·
{"first distributor with stock in your order: " + e(" > ".join(s.order)) if s.strategy == "priority" else "cheapest line total"}
{(" · " + e(rate_note)) if rate_note else ""}</p>
<div class="scores">
 <div class="card"><b>{grand:.2f} €</b><span>to order</span></div>
 <div class="card"><b>{per_board:.2f} €</b><span>per board</span></div>
 <div class="card"><b>{extra:.2f} €</b><span>due to minimum orders</span></div>
 <div class="card"><b>{counts["ok"]}</b><span>lines OK</span></div>
 <div class="card"><b>{counts["short"] + counts["risk"]}</b><span>short or end of life</span></div>
 <div class="card"><b>{counts["proposed"] + counts["not found"]}</b><span>proposed / not found</span></div>
{f'<div class="card"><b>{n_pkg}</b><span>package to check</span></div>' if n_pkg else ''}
</div>
<h2>Bill of materials</h2>
<div class="wrap"><table>
<tr><th class="num">#</th><th>Designators</th><th>Value</th><th>Package</th><th>Manufacturer / MPN</th>
<th>Distributor</th><th class="num">Qty / board<br>needed</th><th class="num">To order</th>
<th class="num">Unit €</th><th class="num">Min. ×mult.</th><th class="num">Total €</th>
<th class="num">Stock</th><th>Status</th></tr>
{"".join(rows)}</table></div>
<h2>Orders</h2>
<table><tr><th>Distributor</th><th class="num">Lines</th><th class="num">Total € (excl. VAT and shipping)</th></tr>
{dist_rows}</table>
{skipped_note}
<p class="muted">Status: OK — the part is in stock; short — not enough stock anywhere; end of life —
obsolete, last time buy or not for new designs (see the replacements); proposed — no part number in
the schematic, a part matching the value and package is proposed; not found — give a part number;
⚠ check package — the part found by its part number does not come in the footprint's package.</p>
<p class="muted">Unit price = the price break reached by the quantity to order; the quantity to
order respects each distributor's minimum order and multiple. Replacements are kept only with the
same package and value, voltage / current / power at least as high, tolerance as tight, dielectric
as good, the X/Y safety class and the temperature range of the original.</p>
</main></body></html>
'''


def _thousands(n: Optional[int]) -> str:
    return '' if n is None else f'{n:,}'.replace(',', '\u202f')


def _price_text(o: Offer) -> str:
    if not o.prices:
        return 'no price'
    first = sorted(o.prices, key=lambda b: b.qty)[0]
    return f'{first.price:.4f} € from {first.qty}'
