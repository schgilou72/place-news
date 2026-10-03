"""End-to-end check against a real KiCad installation (pcbnew + kicad-cli).

Builds a small switch-mode power supply board (primary / secondary, HV net
class, creepage rule, placement area, keep-outs, duplicate references),
scrambles the placement, runs the optimizer, then asks KiCad's own DRC whether
the result respects courtyards, clearance, creepage and keep-outs.

Run with KiCad's Python, e.g.:  python3 -m unittest tests.e2e_kicad -v
Skipped automatically when pcbnew or kicad-cli is not available.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import pcbnew  # noqa: F401
    HAVE_PCBNEW = True
except ImportError:
    HAVE_PCBNEW = False

KICAD_CLI = shutil.which('kicad-cli')

PRO = {
    "meta": {"filename": "smps.kicad_pro", "version": 3},
    "net_settings": {
        "meta": {"version": 4},
        "classes": [
            {"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6,
             "via_drill": 0.3, "priority": 2147483647},
            {"name": "HV", "clearance": 1.0, "track_width": 0.5, "via_diameter": 0.8,
             "via_drill": 0.4, "priority": 0},
        ],
        "netclass_patterns": [{"netclass": "HV", "pattern": p} for p in
                              ("HV_*", "SW", "GATE*", "PRI_*", "FB_P", "CS", "RT")],
    },
}

DRU = """(version 1)
# Reinforced isolation between the primary (HV) side and everything else
(rule "HV creepage"
  (constraint creepage (min 6mm))
  (condition "A.hasNetclass('HV') && !B.hasNetclass('HV')"))
(rule "HV clearance"
  (constraint clearance (min 4mm))
  (condition "A.hasNetclass('HV') && !B.hasNetclass('HV')"))
"""


def _crtyd_rect(x1, y1, x2, y2):
    return (f'  (fp_rect (start {x1} {y1}) (end {x2} {y2}) (stroke (width 0.05) (type default))'
            f' (fill none) (layer "F.CrtYd"))\n')


def _tht(num, x, y, size, drill, shape='circle'):
    return (f'  (pad "{num}" thru_hole {shape} (at {x} {y}) (size {size} {size}) (drill {drill})'
            f' (layers "*.Cu" "*.Mask"))\n')


def _smd(num, x, y, w, h):
    return f'  (pad "{num}" smd rect (at {x} {y}) (size {w} {h}) (layers "F.Cu" "F.Paste" "F.Mask"))\n'


def _fp(name, body):
    return (f'(footprint "{name}" (version 20240108) (generator "pcbnew")\n  (layer "F.Cu")\n'
            f'  (property "Reference" "REF**" (at 0 -3 0) (layer "F.SilkS") '
            f'(effects (font (size 1 1) (thickness 0.15))))\n'
            f'  (property "Value" "{name}" (at 0 3 0) (layer "F.Fab") '
            f'(effects (font (size 1 1) (thickness 0.15))))\n{body})\n')


LIBRARY = {
    'TO220': _fp('TO220', _crtyd_rect(-5.3, -2.0, 5.3, 3.2)
                 + _tht(1, -2.54, 0, 1.8, 1.1, 'rect') + _tht(2, 0, 0, 1.8, 1.1)
                 + _tht(3, 2.54, 0, 1.8, 1.1)),
    'CAP_D10': _fp('CAP_D10', '  (fp_circle (center 0 0) (end 5.25 0) (stroke (width 0.05) (type default))'
                   ' (fill none) (layer "F.CrtYd"))\n'
                   + _tht(1, -2.5, 0, 2.0, 1.0, 'rect') + _tht(2, 2.5, 0, 2.0, 1.0)),
    'XFMR': _fp('XFMR', _crtyd_rect(-12, -8, 12, 8)
                + ''.join(_tht(n + 1, -10, y, 2.0, 1.0) for n, y in enumerate((-5, -1.67, 1.67, 5)))
                + ''.join(_tht(n + 5, 10, y, 2.0, 1.0) for n, y in enumerate((-5, -1.67, 1.67, 5)))),
    'R0805': _fp('R0805', _crtyd_rect(-1.7, -1.0, 1.7, 1.0) + _smd(1, -1.0, 0, 1.0, 1.3)
                 + _smd(2, 1.0, 0, 1.0, 1.3)),
    'SOIC8': _fp('SOIC8', _crtyd_rect(-3.7, -2.7, 3.7, 2.7)
                 + ''.join(_smd(n + 1, -2.7, y, 1.5, 0.6) for n, y in enumerate((-1.905, -0.635, 0.635, 1.905)))
                 + ''.join(_smd(8 - n, 2.7, y, 1.5, 0.6) for n, y in enumerate((-1.905, -0.635, 0.635, 1.905)))),
    'TB2': _fp('TB2', _crtyd_rect(-2.8, -4, 7.9, 4) + _tht(1, 0, 0, 2.5, 1.3, 'rect')
               + _tht(2, 5.08, 0, 2.5, 1.3)),
    'MH3': _fp('MH3', '  (fp_circle (center 0 0) (end 3.5 0) (stroke (width 0.05) (type default))'
               ' (fill none) (layer "F.CrtYd"))\n'
               '  (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask"))\n'),
}

# ref, footprint, sheet, {pad number: net}
PARTS = [
    ('J1', 'TB2', '/Primary/', {1: 'HV_BUS+', 2: 'HV_BUS-'}),
    ('C1', 'CAP_D10', '/Primary/', {1: 'HV_BUS+', 2: 'HV_BUS-'}),
    ('Q1', 'TO220', '/Primary/', {1: 'GATE', 2: 'SW', 3: 'PRI_RTN'}),
    ('R4', 'R0805', '/Primary/', {1: 'PRI_RTN', 2: 'HV_BUS-'}),
    ('T1', 'XFMR', '/Primary/', {1: 'HV_BUS+', 2: 'SW', 5: 'SEC_A', 6: 'SEC_B'}),
    ('U1', 'SOIC8', '/Primary/', {1: 'GATE_DRV', 2: 'FB_P', 3: 'PRI_VCC', 4: 'HV_BUS-',
                                  5: 'CS', 6: 'RT'}),
    ('R1', 'R0805', '/Primary/', {1: 'GATE_DRV', 2: 'GATE'}),
    ('C3', 'R0805', '/Primary/', {1: 'PRI_VCC', 2: 'HV_BUS-'}),
    ('U2', 'SOIC8', '/Primary/', {1: 'FB_P', 2: 'HV_BUS-', 7: 'FB_S', 8: 'GND'}),
    ('D1', 'TO220', '/Secondary/', {1: 'SEC_A', 2: 'VOUT', 3: 'SEC_B'}),
    ('C2', 'CAP_D10', '/Secondary/', {1: 'VOUT', 2: 'GND'}),
    ('J2', 'TB2', '/Secondary/', {1: 'VOUT', 2: 'GND'}),
    ('R2', 'R0805', '/Secondary/', {1: 'VOUT', 2: 'FB_S'}),
    ('R3', 'R0805', '/Secondary/', {1: 'FB_S', 2: 'GND'}),
    ('MH', 'MH3', '', {}),          # two mounting holes with the SAME reference
    ('MH', 'MH3', '', {}),
]

BOARD_W, BOARD_H = 100.0, 60.0
SECONDARY_AREA = (66.0, 2.0, 98.0, 58.0)     # placement area for /Secondary/
KEEPOUT_F = (0.0, 0.0, 9.0, 9.0)             # corner reserved on the front side
KEEPOUT_B = (40.0, 20.0, 60.0, 40.0)         # back side only: must not block front parts
KEEPOUT_PADS_B = (12.0, 44.0, 40.0, 58.0)    # back-side pads: blocks through-hole parts only


def _rect_zone(board, x1, y1, x2, y2, layer, name):
    z = pcbnew.ZONE(board)
    z.SetIsRuleArea(True)
    for flag in ('SetDoNotAllowTracks', 'SetDoNotAllowVias', 'SetDoNotAllowPads',
                 'SetDoNotAllowZoneFills', 'SetDoNotAllowFootprints'):
        getattr(z, flag)(False)
    ls = pcbnew.LSET()
    ls.AddLayer(layer)
    z.SetLayerSet(ls)
    ol = z.Outline()
    ol.NewOutline()
    for (x, y) in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        ol.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    z.SetZoneName(name)
    board.Add(z)
    return z


def build_board(workdir, seed=1):
    lib = os.path.join(workdir, 'Test.pretty')
    os.makedirs(lib, exist_ok=True)
    for name, text in LIBRARY.items():
        with open(os.path.join(lib, name + '.kicad_mod'), 'w') as f:
            f.write(text)

    board = pcbnew.CreateEmptyBoard()
    for (x1, y1, x2, y2) in ((0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
                             (BOARD_W, BOARD_H, 0, BOARD_H), (0, BOARD_H, 0, 0)):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pcbnew.VECTOR2I_MM(x1, y1))
        s.SetEnd(pcbnew.VECTOR2I_MM(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(pcbnew.FromMM(0.1))
        board.Add(s)

    nets = {}
    for _ref, _fpn, _sheet, pins in PARTS:
        for net in pins.values():
            if net not in nets:
                ni = pcbnew.NETINFO_ITEM(board, net)
                board.Add(ni)
                nets[net] = ni

    rnd = random.Random(seed)
    for ref, fpname, sheet, pins in PARTS:
        fp = pcbnew.FootprintLoad(lib, fpname)
        fp.SetReference(ref)
        if sheet:
            fp.SetSheetname(sheet)
        # Freshly imported look: everything piled up in the middle.
        fp.SetPosition(pcbnew.VECTOR2I_MM(rnd.uniform(35, 65), rnd.uniform(20, 40)))
        board.Add(fp)
        for pad in fp.Pads():
            try:
                num = int(str(pad.GetNumber()))
            except ValueError:
                continue
            if num in pins:
                pad.SetNet(nets[pins[num]])

    ko = _rect_zone(board, *KEEPOUT_F, pcbnew.F_Cu, 'KO_corner')
    ko.SetDoNotAllowFootprints(True)
    ko.SetDoNotAllowPads(True)
    ko_b = _rect_zone(board, *KEEPOUT_B, pcbnew.B_Cu, 'KO_back')       # footprints only
    ko_b.SetDoNotAllowFootprints(True)
    ko_p = _rect_zone(board, *KEEPOUT_PADS_B, pcbnew.B_Cu, 'KO_pads_back')  # pads only
    ko_p.SetDoNotAllowPads(True)
    area = _rect_zone(board, *SECONDARY_AREA, pcbnew.F_Cu, 'SECONDARY')
    area.SetPlacementAreaEnabled(True)
    area.SetPlacementAreaSourceType(0)
    area.SetPlacementAreaSource('/Secondary/')

    path = os.path.join(workdir, 'smps.kicad_pcb')
    # Project files first, and save the board without its (empty) settings:
    # otherwise KiCad keeps an in-memory project without our net classes.
    with open(os.path.join(workdir, 'smps.kicad_pro'), 'w') as f:
        json.dump(PRO, f, indent=2)
    with open(os.path.join(workdir, 'smps.kicad_dru'), 'w') as f:
        f.write(DRU)
    pcbnew.SaveBoard(path, board, True)
    board = pcbnew.LoadBoard(path)
    board.SynchronizeNetsAndNetClasses(False)   # the GUI does this on load
    return board, path


def run_drc(path):
    out = path + '.drc.json'
    subprocess.run([KICAD_CLI, 'pcb', 'drc', '--format', 'json', '--units', 'mm',
                    '--severity-all', '-o', out, path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(out) as f:
        return json.load(f)['violations']


def same_footprint(v):
    """True when both items of a violation belong to the same footprint."""
    owners = []
    for item in v.get('items', []):
        d = item.get('description', '')
        owners.append(d.rsplit(' of ', 1)[-1].split(' on ')[0] if ' of ' in d else d)
    return len(owners) == 2 and owners[0] == owners[1]


@unittest.skipUnless(HAVE_PCBNEW and KICAD_CLI, 'needs KiCad (pcbnew module and kicad-cli)')
class TestEndToEnd(unittest.TestCase):
    def test_smps_board(self):
        from plugin.board_model import extract_board_model
        from plugin.annealer import run_sa, SAConfig
        from plugin.cost_function import CostState
        from plugin.placement import apply_model_to_board
        from plugin.isolation import list_violations

        random.seed(11)
        workdir = tempfile.mkdtemp(prefix='place-news-e2e-')
        board, path = build_board(workdir)

        from plugin.silkscreen import extract_silkscreen_model, place_silkscreen, apply_silkscreen

        model = extract_board_model(board)
        self.assertEqual(model.warnings, [])
        self.assertTrue(model.isolation is not None and model.isolation.active)
        self.assertEqual(model.isolation.max_req, 6_000_000)
        self.assertEqual(len(model.keepouts), 3)
        self.assertEqual(len(model.placement_areas), 1)
        self.assertEqual(model.placement_areas[0].members,
                         {f.index for f in model.footprints if f.sheet == '/Secondary/'})
        self.assertEqual(len({fp.uuid for fp in model.footprints}), len(model.footprints))
        t1 = next(f for f in model.footprints if f.reference == 'T1')
        r1 = next(f for f in model.footprints if f.reference == 'R1')
        self.assertEqual(t1.copper_sides, ('B', 'F'))
        self.assertEqual(r1.copper_sides, ('F',))

        run_sa(model, SAConfig(max_iterations=120, reheat_count=2))
        cs = CostState(model, quiet=True)
        self.assertEqual(cs.area_violations(), [])
        self.assertAlmostEqual(cs._keepout_penalty, 0.0, delta=1.0)
        apply_model_to_board(board, model)
        verify = extract_board_model(board)
        silk = extract_silkscreen_model(board, verify)
        apply_silkscreen(board, silk, place_silkscreen(silk), board_model=verify)
        pcbnew.SaveBoard(path, board, True)

        # The two 'MH' footprints must both have been placed (UUID mapping).
        reloaded = pcbnew.LoadBoard(path)
        mh = [fp for fp in reloaded.GetFootprints() if fp.GetReference() == 'MH']
        positions = {(fp.GetPosition().x, fp.GetPosition().y) for fp in mh}
        expected = {(int(f.x), int(f.y)) for f in model.footprints if f.reference == 'MH'}
        self.assertEqual(positions, expected)

        violations = run_drc(path)
        by_type = {}
        for v in violations:
            by_type.setdefault(v['type'], []).append(v)
        report = {t: len(vs) for t, vs in by_type.items()}
        print('\nKiCad DRC after placement:', report)

        self.assertNotIn('courtyards_overlap', by_type, by_type.get('courtyards_overlap'))
        self.assertNotIn('items_not_allowed', by_type, by_type.get('items_not_allowed'))
        for t in ('clearance', 'creepage'):
            between = [v['description'] + ' | ' + ' / '.join(i['description'] for i in v['items'])
                       for v in by_type.get(t, []) if not same_footprint(v)]
            # KiCad 10 also measures creepage from pads to rule-area outlines
            # (rule areas have no net class, so "!B.hasNetclass('HV')" matches).
            # A rule area is not a conductor; the placer does not model this.
            vs_rule_area = [b for b in between if 'Rule area' in b]
            print(f'{t}: {len(vs_rule_area)} vs rule areas (KiCad quirk):', vs_rule_area[:5])
            between = [b for b in between if 'Rule area' not in b]
            self.assertEqual(between, [], f'{t} violations between footprints')
        # The opto (U2) is too small for 6 mm: KiCad and the optimizer both
        # flag it as a footprint problem, not a placement one.
        intra_kicad = [v for v in by_type.get('creepage', []) if same_footprint(v)]
        self.assertTrue(intra_kicad)
        intra_ours = [v for v in list_violations(model.isolation, model.footprints) if v.fp_a == v.fp_b]
        self.assertTrue(intra_ours)
        self.assertTrue(all(model.footprints[v.fp_a].reference in ('U2', 'T1', 'U1', 'Q1', 'R4', 'C3',
                                                                    'R1', 'J1', 'C1')
                            for v in intra_ours))
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
