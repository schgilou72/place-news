"""End-to-end check against a real KiCad installation (pcbnew + kicad-cli).

Builds a small isolated flyback board (HV / primary / secondary net classes,
reinforced-isolation creepage and clearance rules, placement area, keep-outs,
duplicate references), scrambles the placement, runs the optimizer, then asks
KiCad's own DRC whether the result respects courtyards, clearance, creepage
and keep-outs. When Java 25+ and a Freerouting jar are available, the placed
board is also routed through place-news' Specctra round trip and checked
again.

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
            {"name": "HV", "clearance": 0.6, "track_width": 0.5, "via_diameter": 0.8,
             "via_drill": 0.4, "priority": 0},
            {"name": "PRI", "clearance": 0.25, "track_width": 0.3, "via_diameter": 0.6,
             "via_drill": 0.3, "priority": 1},
            {"name": "SEC", "clearance": 0.2, "track_width": 0.4, "via_diameter": 0.6,
             "via_drill": 0.3, "priority": 2},
        ],
        "netclass_patterns": (
            # HV: the rectified bus and the switch node; the primary return
            # (HV_BUS-) is the reference of the primary-side control circuit.
            [{"netclass": "HV", "pattern": p} for p in ("HV_BUS+", "SW")]
            + [{"netclass": "PRI", "pattern": p} for p in ("HV_BUS-", "GATE*", "PRI_*", "FB_P",
                                                            "CS", "RT")]
            + [{"netclass": "SEC", "pattern": p} for p in ("SEC_*", "VOUT", "GND", "FB_S")]),
    },
}

DRU = """(version 1)
# Reinforced isolation between the primary side (HV, PRI) and the secondary side
(rule "isolation creepage"
  (constraint creepage (min 6mm))
  (condition "(A.hasNetclass('HV') || A.hasNetclass('PRI')) && B.hasNetclass('SEC')"))
(rule "isolation clearance"
  (constraint clearance (min 4mm))
  (condition "(A.hasNetclass('HV') || A.hasNetclass('PRI')) && B.hasNetclass('SEC')"))
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
    # Wide-body optocoupler: 10 mm between the input and output pin rows.
    'OPTO4W': _fp('OPTO4W', _crtyd_rect(-6.3, -2.6, 6.3, 2.6)
                  + _smd(1, -5.0, -1.27, 1.5, 0.8) + _smd(2, -5.0, 1.27, 1.5, 0.8)
                  + _smd(3, 5.0, 1.27, 1.5, 0.8) + _smd(4, 5.0, -1.27, 1.5, 0.8)),
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
    ('U2', 'OPTO4W', '/Primary/', {1: 'FB_P', 2: 'HV_BUS-', 3: 'GND', 4: 'FB_S'}),
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


def run_drc(path, with_unconnected=False):
    out = path + '.drc.json'
    subprocess.run([KICAD_CLI, 'pcb', 'drc', '--format', 'json', '--units', 'mm',
                    '--severity-all', '-o', out, path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(out) as f:
        data = json.load(f)
    if with_unconnected:
        return data['violations'], data.get('unconnected_items', [])
    return data['violations']


def same_footprint(v):
    """True when both items of a violation belong to the same footprint."""
    owners = []
    for item in v.get('items', []):
        d = item.get('description', '')
        owners.append(d.rsplit(' of ', 1)[-1].split(' on ')[0] if ' of ' in d else d)
    return len(owners) == 2 and owners[0] == owners[1]


def place_board(workdir):
    """Build the test board, run the optimizer and the silkscreen pass, save."""
    from plugin.board_model import extract_board_model
    from plugin.annealer import run_sa, SAConfig
    from plugin.placement import apply_model_to_board
    from plugin.silkscreen import extract_silkscreen_model, place_silkscreen, apply_silkscreen

    random.seed(11)
    board, path = build_board(workdir)
    model = extract_board_model(board)
    model.sa_result = run_sa(model, SAConfig(max_iterations=120, reheat_count=2, align_rows=True,
                                             untangle=True))
    apply_model_to_board(board, model)
    verify = extract_board_model(board)
    silk = extract_silkscreen_model(board, verify)
    apply_silkscreen(board, silk, place_silkscreen(silk), board_model=verify)
    pcbnew.SaveBoard(path, board, True)
    return board, path, model


def isolation_violations(violations):
    return [v['description'] + ' | ' + ' / '.join(i['description'] for i in v['items'])
            for v in violations if v['type'] in ('clearance', 'creepage')]


def _freerouting():
    """(java, jar) when Freerouting can run here, else None."""
    from plugin import specctra
    jar = os.environ.get('PLACE_NEWS_FREEROUTING_JAR') or specctra.find_freerouting_jar()
    java, version = specctra.find_java()
    if jar and os.path.isfile(jar) and java and version >= specctra.MIN_JAVA:
        return java, jar
    return None


@unittest.skipUnless(HAVE_PCBNEW and KICAD_CLI, 'needs KiCad (pcbnew module and kicad-cli)')
class TestEndToEnd(unittest.TestCase):
    def test_smps_board(self):
        from plugin.board_model import extract_board_model
        from plugin.cost_function import CostState
        from plugin.isolation import list_violations

        workdir = tempfile.mkdtemp(prefix='place-news-e2e-')
        board, path = build_board(workdir)
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
        # Every part can meet the rules by itself (wide-body opto, split transformer).
        self.assertEqual([v for v in list_violations(model.isolation, model.footprints)
                          if v.fp_a == v.fp_b], [])

        board, path, model = place_board(workdir)
        cs = CostState(model, quiet=True)
        self.assertEqual(cs.area_violations(), [])
        untangled = model.sa_result.untangle
        self.assertIsNotNone(untangled)
        self.assertLessEqual(untangled.crossings_after, untangled.crossings_before)
        print(f'\nRatsnest crossings: {untangled.crossings_before} -> {untangled.crossings_after} '
              f'({untangled.swaps} swaps, {untangled.flips} turned)')
        self.assertAlmostEqual(cs._keepout_penalty, 0.0, delta=1.0)
        self.assertEqual(list_violations(model.isolation, model.footprints), [])

        # The two 'MH' footprints must both have been placed (UUID mapping).
        reloaded = pcbnew.LoadBoard(path)
        mh = [fp for fp in reloaded.GetFootprints() if fp.GetReference() == 'MH']
        positions = {(fp.GetPosition().x, fp.GetPosition().y) for fp in mh}
        expected = {(int(f.x), int(f.y)) for f in model.footprints if f.reference == 'MH'}
        self.assertEqual(positions, expected)

        violations = run_drc(path)
        report = {}
        for v in violations:
            report[v['type']] = report.get(v['type'], 0) + 1
        print('\nKiCad DRC after placement:', report)
        self.assertNotIn('courtyards_overlap', report)
        self.assertNotIn('items_not_allowed', report)
        self.assertEqual(isolation_violations(violations), [])
        shutil.rmtree(workdir, ignore_errors=True)

    def test_route_with_isolation(self):
        from plugin import specctra
        tools = _freerouting()
        if not tools:
            self.skipTest('needs Java 25+ and a Freerouting jar (PLACE_NEWS_FREEROUTING_JAR)')
        java, jar = tools
        workdir = tempfile.mkdtemp(prefix='place-news-route-')
        board, path, _model = place_board(workdir)
        dsn = os.path.join(workdir, 'smps.dsn')
        ses = os.path.join(workdir, 'smps.ses')
        rep = specctra.export_dsn(board, dsn, use_isolation=True)
        # Footprint keep-outs and the placement area must not block routing.
        self.assertEqual(rep.keepouts['removed'], 4)
        self.assertEqual(rep.keepouts['unmatched'], 0)
        # DSN class names are the effective net classes, e.g. 'HV,Default'.
        pairs = {frozenset(n.split(',')[0] for n in k): v for k, v in rep.classes.pairs.items()}
        self.assertEqual(pairs.get(frozenset(('HV', 'SEC'))), 6_000_000)
        self.assertEqual(pairs.get(frozenset(('PRI', 'SEC'))), 6_000_000)
        result = specctra.run_freerouting(java, jar, dsn, ses, passes=10)
        self.assertTrue(result.ok, '\n'.join(result.log[-20:]))
        self.assertTrue(specctra.import_ses(board, ses))
        pcbnew.SaveBoard(path, board, True)
        self.assertGreater(len(board.GetTracks()), 20)
        violations = run_drc(path)
        report = {}
        for v in violations:
            report[v['type']] = report.get(v['type'], 0) + 1
        print('\nFreerouting:', result.unrouted, 'unrouted;', 'KiCad DRC after routing:', report)
        self.assertEqual(isolation_violations(violations), [])
        # Blocked rule areas used to leave 5+ connections unrouted on this board.
        self.assertIsNotNone(result.unrouted)
        self.assertLessEqual(result.unrouted, 1)

        # Ground plane on the top layer: the HV side keeps its 6 mm of creepage,
        # also from an HV copper zone (the zone filler alone would keep 2 mm).
        from plugin import pour
        hv_pad = next(p for fp in board.GetFootprints() for p in fp.Pads()
                      if str(p.GetNetname()) == 'HV_BUS+' and p.IsOnLayer(pcbnew.F_Cu))
        c = hv_pad.GetPosition()
        hv = pcbnew.ZONE(board)
        hv.SetLayer(pcbnew.F_Cu)
        hv.SetNetCode(hv_pad.GetNetCode())
        hv.SetZoneName('HV copper')
        ol = hv.Outline()
        ol.NewOutline()
        for dx, dy in ((-2, -2), (2, -2), (2, 2), (-2, 2)):
            ol.Append(c.x + pcbnew.FromMM(dx), c.y + pcbnew.FromMM(dy))
        board.Add(hv)
        net = pour.default_pour_net(board)
        self.assertEqual(net, 'GND')
        made = pour.add_pour(board, net, 'F.Cu')
        self.assertEqual(made.error, '')
        self.assertTrue(made.filled)
        self.assertGreater(made.cutouts, 10)
        self.assertEqual(made.widest, 6_000_000)
        poured = [z for z in board.Zones() if str(z.GetZoneName()) == pour.POUR_NAME]
        self.assertTrue(poured and all(z.IsFilled() for z in poured))
        self.assertGreater(sum(z.CalculateFilledArea() for z in poured), 1000 * 1e12)   # > 1000 mm²
        # Running again replaces the pour.
        again = pour.add_pour(board, net, 'F.Cu')
        self.assertEqual(again.replaced, len(poured))
        # Routing again: the old pour must not reach Freerouting as a GND plane
        # (it would skip the GND tracks), and the board keeps it meanwhile.
        dsn2 = os.path.join(workdir, 'again.dsn')
        specctra.export_dsn(board, dsn2, use_isolation=True)
        with open(dsn2) as f:
            self.assertIn('(plane GND', f.read())
        pour.export_dsn_without_pours(board, dsn2, use_isolation=True)
        with open(dsn2) as f:
            self.assertNotIn('(plane GND', f.read())
        self.assertEqual(pour.count_pours(board), len(poured))
        pcbnew.SaveBoard(path, board, True)
        violations, unconnected = run_drc(path, with_unconnected=True)
        print('KiCad DRC with the GND pour:',
              {t: sum(1 for v in violations if v['type'] == t) for t in {v['type'] for v in violations}},
              '| unconnected:', len(unconnected))
        self.assertEqual(isolation_violations(violations), [])
        self.assertLessEqual(len(unconnected), result.unrouted)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_layer_estimate_and_outer_layers_trial(self):
        from plugin import specctra
        from plugin.aesthetic_check import estimate_layers, extract_geometry
        workdir = tempfile.mkdtemp(prefix='place-news-layers-')
        board, path, _model = place_board(workdir)
        est = estimate_layers(extract_geometry(board))
        self.assertEqual(est.layers, 2)
        self.assertLessEqual(est.needed_layers, 2)        # 1 when the ratsnest is planar
        self.assertEqual(est.suggested_layers, 2)
        self.assertGreater(est.ratio, 2.0)            # a roomy power board
        dsn = os.path.join(workdir, 'smps.dsn')
        specctra.export_dsn(board, dsn, use_isolation=True)
        # Two layers: nothing to take away.
        self.assertEqual(specctra.outer_layers_only(dsn, dsn + '.outer'), [])

        # The same board in 4 layers: the estimate says 2 should do, and the
        # trial on the outer layers routes everything without the inner ones.
        board.SetCopperLayerCount(4)
        enabled = board.GetEnabledLayers()
        enabled.AddLayerSet(pcbnew.LSET.AllCuMask(4))
        board.SetEnabledLayers(enabled)
        est4 = estimate_layers(extract_geometry(board))
        self.assertEqual(est4.layers, 4)
        self.assertEqual(est4.suggested_layers, 2)
        self.assertIn('2 routing layers should do', est4.verdict())
        tools = _freerouting()
        if not tools:
            self.skipTest('needs Java 25+ and a Freerouting jar (PLACE_NEWS_FREEROUTING_JAR)')
        java, jar = tools
        specctra.export_dsn(board, dsn, use_isolation=True)
        outer = dsn + '.outer.dsn'
        self.assertEqual(specctra.outer_layers_only(dsn, outer), ['In1.Cu', 'In2.Cu'])
        ses = os.path.join(workdir, 'smps.ses')
        result = specctra.run_freerouting(java, jar, outer, ses, passes=10)
        self.assertTrue(result.ok)
        self.assertEqual(result.unrouted, 0)
        self.assertTrue(specctra.import_ses(board, ses))
        layers = {board.GetLayerName(t.GetLayer()) for t in board.GetTracks()
                  if t.GetClass() != 'PCB_VIA'}
        self.assertTrue(layers and layers <= {'F.Cu', 'B.Cu'}, layers)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
