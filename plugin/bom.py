"""Bill of materials of a board: lines of identical parts with their
designators and fields (manufacturer part number, distributor part numbers,
parameters), and writing chosen values back into the footprints' fields.

Fields written on the board reach the schematic with Tools > Update
Schematic from PCB > "Other fields". Pure Python except the two functions
that take a pcbnew board.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

# Field names people use (compared without case, spaces, '_', '-', '.', '#').
MPN_FIELDS = ('MPN', 'Manufacturer_Part_Number', 'ManufacturerPartNumber', 'MFR_PN', 'MfrPN',
              'Mfr_No', 'MfrPartNumber', 'PartNumber', 'Manf#', 'MPN#', 'PN', 'RefFabricant',
              'ReferenceFabricant')
MFR_FIELDS = ('Manufacturer', 'Manufacturer_Name', 'MFR', 'Mfr', 'Manf', 'Fabricant', 'Brand')
DISTRIBUTORS = ('DigiKey', 'Mouser', 'Farnell', 'TME', 'LCSC')
SKU_FIELDS: Dict[str, Tuple[str, ...]] = {
    'DigiKey': ('DigiKey', 'DigiKey_PN', 'DigiKeyPartNumber', 'DK', 'DKPN'),
    'Mouser': ('Mouser', 'Mouser_PN', 'MouserPartNumber'),
    'Farnell': ('Farnell', 'Farnell_PN', 'Newark', 'element14', 'FarnellPartNumber'),
    'TME': ('TME', 'TME_PN', 'TMEPartNumber'),
    'LCSC': ('LCSC', 'LCSC_PN', 'LCSCPart', 'LCSCPartNumber', 'JLCPCB', 'JLC'),
}
# Names written by place-news
WRITE_MPN = 'MPN'
WRITE_MFR = 'Manufacturer'

_SKIP_LIBS = ('MountingHole', 'Fiducial', 'Symbol:', 'Logo', 'NetTie')


def _norm(name: str) -> str:
    return re.sub(r'[\s_\-.#/]', '', name).lower()


def find_field(fields: Dict[str, str], candidates: Iterable[str]) -> Tuple[str, str]:
    """(field name, value) of the first non-empty field among the candidates:
    exact names first (case aside), then loose spellings ('Mfr. No' for
    'Mfr_No'). Exact first keeps KiCost's 'manf#' (MPN) and 'manf'
    (manufacturer) apart."""
    candidates = list(candidates)
    by_lower = {k.lower(): k for k in fields}
    by_norm = {_norm(k): k for k in fields}
    for table, key in ((by_lower, str.lower), (by_norm, _norm)):
        for c in candidates:
            k = table.get(key(c))
            if k is not None and str(fields[k]).strip() not in ('', '~', '-'):
                return k, str(fields[k]).strip()
    return '', ''


# Fields that make two parts with the same value different (a 16 V and a
# 100 V capacitor are not one BOM line).
SPEC_FIELDS = ('voltage', 'tension', 'tolerance', 'power', 'puissance', 'dielectric', 'current',
               'courant', 'rating', 'ratings', 'safetyclass', 'temperature', 'tempco', 'type')


def _spec_key(fields: Dict[str, str]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((_norm(k), re.sub(r'\s+', '', str(v)).lower()) for k, v in fields.items()
                        if _norm(k) in SPEC_FIELDS and str(v).strip()))


_NAT = re.compile(r'(\d+)')


def natural_key(ref: str):
    return [int(p) if p.isdigit() else p for p in _NAT.split(ref)]


def compact_refs(refs: Sequence[str]) -> str:
    """'R1, R2, R3, R5' -> 'R1-R3, R5'."""
    out: List[str] = []
    run: List[Tuple[str, int, str]] = []

    def flush():
        if not run:
            return
        if len(run) >= 3:
            out.append(f'{run[0][2]}-{run[-1][2]}')
        else:
            out.extend(r[2] for r in run)
        run.clear()
    for ref in sorted(refs, key=natural_key):
        m = re.match(r'^(.*?)(\d+)$', ref)
        if not m:
            flush()
            out.append(ref)
            continue
        prefix, num = m.group(1), int(m.group(2))
        if run and run[-1][0] == prefix and run[-1][1] == num - 1:
            run.append((prefix, num, ref))
        else:
            flush()
            run.append((prefix, num, ref))
    flush()
    return ', '.join(out)


def kind_of(ref: str) -> str:
    m = re.match(r'^[A-Za-z]+', ref)
    return m.group(0).upper() if m else ''


# ---------------------------------------------------------------------------
# Package from the footprint name (KiCad library naming)
# ---------------------------------------------------------------------------

_PACKAGE_RULES = (
    (re.compile(r'^(?:R|C|L|D|LED|Fuse|FB)_(0[0-9]{3}|1[0-9]{3}|2[0-9]{3})_\d{4}Metric', re.I),
     lambda m: m.group(1)),
    (re.compile(r'^(?:C|CP)_Elec_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)', re.I),
     lambda m: f'SMD elec {m.group(1)}x{m.group(2)}'),
    (re.compile(r'^C(?:P)?_Radial_D(\d+(?:\.\d+)?)mm_H?.*?P(\d+(?:\.\d+)?)mm', re.I),
     lambda m: f'radial D{m.group(1)} P{m.group(2)}'),
    (re.compile(r'^C_Rect_L(\d+(?:\.\d+)?)mm_W(\d+(?:\.\d+)?)mm_P(\d+(?:\.\d+)?)mm', re.I),
     lambda m: f'film L{m.group(1)} W{m.group(2)} P{m.group(3)}'),
    (re.compile(r'^C_Disc_D(\d+(?:\.\d+)?)mm.*?_P(\d+(?:\.\d+)?)mm', re.I),
     lambda m: f'disc D{m.group(1)} P{m.group(2)}'),
    (re.compile(r'^R_Axial_(DIN\d+)_L(\d+(?:\.\d+)?)mm.*?_P(\d+(?:\.\d+)?)mm', re.I),
     lambda m: f'axial {m.group(1)} P{m.group(3)}'),
    (re.compile(r'(TO-\d+(?:[A-Z]{1,2})?(?:-\d+)?)', re.I), lambda m: m.group(1).upper()),
    (re.compile(r'(SOT-\d+(?:-\d+)?)', re.I), lambda m: m.group(1).upper()),
    (re.compile(r'^(?:D_)?(SOD-\d+[A-Z]*)', re.I), lambda m: m.group(1).upper()),
    (re.compile(r'^D_(SM[ABC])\b', re.I), lambda m: m.group(1).upper()),
    (re.compile(r'^D_(DO-\d+[A-Z]*)', re.I), lambda m: m.group(1).upper()),
    (re.compile(r'^((?:H|V|W|U)?(?:TSSOP|SSOP|MSOP|SOIC|SOP|SO|QFN|DFN|LQFP|TQFP|QFP|BGA|LGA|'
                r'PowerPAK|PDIP|DIP)-\d+)', re.I), lambda m: m.group(1).upper()),
)


def package_of(footprint: str) -> str:
    """'Resistor_SMD:R_0805_2012Metric' -> '0805', ...; else the footprint name."""
    name = footprint.split(':')[-1]
    for rx, fmt in _PACKAGE_RULES:
        m = rx.search(name)
        if m:
            return fmt(m)
    return name


# ---------------------------------------------------------------------------
# BOM lines
# ---------------------------------------------------------------------------

@dataclass
class BomLine:
    refs: List[str]
    value: str
    footprint: str
    package: str
    kind: str
    mpn: str = ''
    manufacturer: str = ''
    skus: Dict[str, str] = field(default_factory=dict)       # distributor -> part number
    fields: Dict[str, str] = field(default_factory=dict)     # every field of the first part
    sheet: str = ''

    @property
    def qty(self) -> int:
        return len(self.refs)

    @property
    def designators(self) -> str:
        return compact_refs(self.refs)

    @property
    def key(self) -> Tuple:
        return (self.kind, self.value.strip().lower(), self.footprint, self.mpn.strip().upper(),
                self.manufacturer.strip().upper(), _spec_key(self.fields))


def group_parts(parts: Sequence[Tuple[str, str, str, Dict[str, str], str]]) -> List[BomLine]:
    """parts: (reference, value, footprint lib id, fields, sheet) -> lines."""
    lines: Dict[Tuple, BomLine] = {}
    for ref, value, fpid, fields, sheet in parts:
        _n, mpn = find_field(fields, MPN_FIELDS)
        _n, mfr = find_field(fields, MFR_FIELDS)
        skus = {}
        for dist, names in SKU_FIELDS.items():
            _n, sku = find_field(fields, names)
            if sku:
                skus[dist] = sku
        line = BomLine(refs=[ref], value=value, footprint=fpid, package=package_of(fpid),
                       kind=kind_of(ref), mpn=mpn, manufacturer=mfr, skus=skus,
                       fields=dict(fields), sheet=sheet)
        same = lines.get(line.key)
        if same is None:
            lines[line.key] = line
        else:
            same.refs.append(ref)
            for d, s in skus.items():
                same.skus.setdefault(d, s)
    out = list(lines.values())
    for line in out:
        line.refs.sort(key=natural_key)
    out.sort(key=lambda l: natural_key(l.refs[0]))
    return out


def board_parts(board) -> Tuple[List[Tuple[str, str, str, Dict[str, str], str]], List[str]]:
    """Parts of a pcbnew board that belong in the BOM, and the skipped ones."""
    parts, skipped = [], []
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        fpid = str(fp.GetFPIDAsString())
        if not ref or ref.startswith('#') or any(s in fpid for s in _SKIP_LIBS):
            continue
        try:
            if fp.IsDNP() or fp.IsExcludedFromBOM():
                skipped.append(ref)
                continue
        except Exception:
            pass
        if _mechanical(fp):
            skipped.append(ref)
            continue
        try:
            fields = {str(k): str(v) for k, v in dict(fp.GetFieldsText()).items()}
        except Exception:
            fields = {'Reference': ref, 'Value': str(fp.GetValue())}
        try:
            sheet = str(fp.GetSheetname())
        except Exception:
            sheet = ''
        parts.append((ref, str(fp.GetValue()), fpid, fields, sheet))
    return parts, skipped


def _mechanical(fp) -> bool:
    """No pad at all (a logo), or a mounting hole / fiducial (reference MH,
    H, FID with only non-plated holes). A heatsink with non-plated holes
    stays in the BOM: it is bought."""
    try:
        import pcbnew
        npth = getattr(pcbnew, 'PAD_ATTRIB_NPTH', 3)
        pads = list(fp.Pads())
        if not pads:
            return True
        holes_only = all(p.GetAttribute() == npth for p in pads)
        return holes_only and kind_of(str(fp.GetReference())) in ('MH', 'H', 'HOLE', 'FID', 'MK')
    except Exception:
        return False


def write_fields(board, refs: Iterable[str], values: Dict[str, str],
                 fill_only: Iterable[str] = ()) -> int:
    """Write fields into the footprints `refs` (hidden, on the Fab layer when
    new). An existing field with another spelling of the name is updated,
    except the `fill_only` ones, which are only written when empty (the
    designer's own figures stay). Returns the number of fields written."""
    keep = {_norm(n) for n in fill_only}
    import pcbnew
    wanted = set(refs)
    written = 0
    for fp in board.GetFootprints():
        if str(fp.GetReference()) not in wanted:
            continue
        existing = {}
        try:
            existing = {_norm(str(f.GetName())): f for f in fp.GetFields()}
        except Exception:
            pass
        for name, value in values.items():
            if value is None or str(value) == '':
                continue
            f = existing.get(_norm(name))
            if f is not None:
                current = str(f.GetText()).strip()
                if current and _norm(name) in keep:
                    continue
                if current != str(value):
                    f.SetText(str(value))
                    written += 1
                continue
            fp.SetField(name, str(value))
            f = fp.GetField(name)
            try:
                f.SetVisible(False)
                back = fp.GetLayer() == pcbnew.B_Cu
                f.SetLayer(pcbnew.B_Fab if back else pcbnew.F_Fab)
                f.SetPosition(fp.GetPosition())
            except Exception:
                pass
            written += 1
    return written
