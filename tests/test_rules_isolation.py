"""Tests for custom-rule parsing, isolation requirements, keep-out sides /
polygons and placement areas — no KiCad dependency."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from plugin.rules import (PadInfo, parse_rules, parse_length, compile_condition,
                          UnsupportedRule)
from plugin.isolation import build_isolation_model, list_violations, box_gap
from plugin.board_model import (BoardModel, Footprint, Pad, Net, KeepOut, PlacementArea,
                                ComponentGroup, polygon_is_rectangle, area_members,
                                AREA_SHEET, AREA_COMPONENT_CLASS, AREA_GROUP)
from plugin.cost_function import CostState, keepout_overlap, rect_intersects_polygon
from plugin.moves import (do_translate, do_swap, do_rotate, do_median, revert_move,
                          affected_indices)
from plugin.annealer import run_sa, SAConfig

MM = 1_000_000

HV = PadInfo(net_name='HV_IN', netclasses=('HV', 'Default'), nc_clearance=3 * MM)
LV = PadInfo(net_name='LV_OUT', netclasses=('Default',), nc_clearance=int(0.2 * MM))
GND = PadInfo(net_name='GND', netclasses=('Default',), nc_clearance=int(0.2 * MM))

CREEPAGE_RULES = '''
(version 1)
# HV to anything else
(rule "HV creepage"
  (constraint creepage (min 6mm))
  (condition "A.hasNetclass('HV') && !B.hasNetclass('HV')"))
'''


class TestUnits(unittest.TestCase):
    def test_lengths(self):
        self.assertEqual(parse_length('6mm'), 6 * MM)
        self.assertEqual(parse_length('0.5 mm'), MM // 2)
        self.assertEqual(parse_length('20mil'), 508_000)
        self.assertEqual(parse_length('0.1in'), 2_540_000)
        self.assertEqual(parse_length('2'), 2 * MM)        # unitless = mm
        with self.assertRaises(ValueError):
            parse_length('3furlongs')


class TestConditions(unittest.TestCase):
    def ev(self, src, a, b):
        fn, _ = compile_condition(src)
        return bool(fn(a, b))

    def test_netclass_equality_matches_constituent(self):
        # KiCad: A.NetClass == 'HV' is true for the composite 'HV,Default'
        self.assertTrue(self.ev("A.NetClass == 'HV'", HV, LV))
        self.assertFalse(self.ev("A.NetClass == 'HV'", LV, HV))
        self.assertTrue(self.ev("A.NetClass != 'HV'", LV, HV))

    def test_has_netclass_and_negation(self):
        self.assertTrue(self.ev("A.hasNetclass('HV') && !B.hasNetclass('HV')", HV, LV))
        self.assertFalse(self.ev("A.hasNetclass('HV') && !B.hasNetclass('HV')", HV, HV))

    def test_and_binds_less_than_or(self):
        # KiCad grammar: '&&' has LOWER precedence than '||' (checked with
        # kicad-cli: this rule never fires because B is never in class 'XX').
        src = "A.NetClass == 'Default' || A.NetClass == 'HV' && B.NetClass == 'XX'"
        self.assertFalse(self.ev(src, LV, GND))

    def test_wildcards_case_insensitive(self):
        self.assertTrue(self.ev("A.NetName == 'hv_*'", HV, LV))
        self.assertTrue(self.ev("A.NetName == 'HV_IN'", HV, LV))
        self.assertFalse(self.ev("A.NetName == 'HV_?X'", HV, LV))

    def test_sheet_and_classes(self):
        a = PadInfo(sheet='/Power/LLC/', component_classes=('HV_PARTS',), fp_ref='Q3',
                    groups=('Bridge',))
        self.assertTrue(self.ev("A.memberOfSheetOrChildren('/Power/')", a, LV))
        self.assertFalse(self.ev("A.memberOfSheet('/Power/')", a, LV))
        self.assertTrue(self.ev("A.memberOfSheet('/Power/LLC')", a, LV))
        self.assertTrue(self.ev("A.hasComponentClass('HV_PARTS')", a, LV))
        self.assertTrue(self.ev("A.memberOfFootprint('Q*')", a, LV))
        self.assertTrue(self.ev("A.memberOfFootprint('${Class:HV_PARTS}')", a, LV))
        self.assertTrue(self.ev("A.memberOfGroup('Bridge')", a, LV))

    def test_pad_type_and_layer(self):
        tht = PadInfo(pad_type='tht', layers=('*.Cu',))
        self.assertTrue(self.ev("A.Type == 'Pad'", tht, LV))
        self.assertTrue(self.ev("A.Pad_Type == 'Through-hole'", tht, LV))
        self.assertTrue(self.ev("A.isPlated()", tht, LV))
        self.assertTrue(self.ev("A.existsOnLayer('B.Cu')", tht, LV))
        self.assertFalse(self.ev("A.existsOnLayer('B.Cu')", LV, tht))
        self.assertTrue(self.ev("A.Layer == 'F.Cu'", LV, tht))

    def test_unsupported_is_reported_not_guessed(self):
        for src in ("A.Width > 1mm", "A.insideArea('X')", "A.Hole_Size == 1mm", "C.NetName == 'x'"):
            with self.assertRaises(UnsupportedRule):
                compile_condition(src)


class TestRuleSet(unittest.TestCase):
    def test_netclass_clearance_when_no_rule(self):
        rs = parse_rules('(version 1)')
        self.assertEqual(rs.requirement(HV, LV), 3 * MM)
        self.assertEqual(rs.requirement(LV, GND), int(0.2 * MM))

    def test_creepage_rule_applies_both_orders(self):
        rs = parse_rules(CREEPAGE_RULES)
        self.assertEqual(rs.requirement(HV, LV), 6 * MM)
        self.assertEqual(rs.requirement(LV, HV), 6 * MM)
        # HV to HV: rule does not match, net-class clearance applies
        self.assertEqual(rs.requirement(HV, HV), 3 * MM)

    def test_last_rule_wins_and_overrides_netclass(self):
        rs = parse_rules('''(version 1)
            (rule "big" (constraint clearance (min 5mm)) (condition "A.hasNetclass('HV')"))
            (rule "small" (constraint clearance (min 1mm)) (condition "A.hasNetclass('HV')"))''')
        self.assertEqual(rs.requirement(HV, LV), 1 * MM)

    def test_board_minimum_is_a_floor(self):
        rs = parse_rules('''(version 1)
            (rule "tiny" (constraint clearance (min 0.05mm)))''', board_min_clearance=int(0.15 * MM))
        self.assertEqual(rs.requirement(LV, GND), int(0.15 * MM))

    def test_severity_ignore_disables(self):
        rs = parse_rules('''(version 1)
            (rule "off" (severity ignore) (constraint creepage (min 9mm)))''')
        self.assertEqual(rs.requirement(HV, LV), 3 * MM)

    def test_unsupported_rule_is_listed(self):
        rs = parse_rules('''(version 1)
            (rule "w" (constraint clearance (min 2mm)) (condition "A.Width > 1mm"))
            (rule "ok" (constraint creepage (min 4mm)) (condition "A.hasNetclass('HV')"))
            (rule "silk" (constraint silk_clearance (min 0.1mm)))''')
        self.assertEqual([r.name for r in rs.rules], ['ok'])
        self.assertEqual(rs.unsupported[0][0], 'w')
        self.assertEqual(rs.other_rules, 1)


def iso_footprint(ref, idx, x, y, pads, w=4 * MM, h=2 * MM, locked=False):
    fp = Footprint(reference=ref, index=idx, x=x, y=y, angle_deg=0.0, width=w, height=h,
                   locked=locked, uuid=f'uuid-{idx}',
                   pads=[Pad(net_code=nc, net_name=name, offset_x=ox, offset_y=0)
                         for (nc, name, ox, _info) in pads],
                   net_codes={nc for (nc, _n, _o, _i) in pads if nc})
    return fp


def make_iso_model(rules_text=CREEPAGE_RULES, x2=10 * MM):
    """Two 2-pad parts: R1 (HV_IN / GND) at x=10mm and R2 (LV_OUT / GND) at x=x2."""
    r1 = [(1, 'HV_IN', -1250_000, HV), (3, 'GND', 1250_000, GND)]
    r2 = [(2, 'LV_OUT', -1250_000, LV), (3, 'GND', 1250_000, GND)]
    fps = [iso_footprint('R1', 0, 10 * MM, 10 * MM, r1),
           iso_footprint('R2', 1, x2, 10 * MM, r2)]
    nets = {1: Net(1, 'HV_IN', [(0, 0)]), 2: Net(2, 'LV_OUT', [(1, 0)]),
            3: Net(3, 'GND', [(0, 1), (1, 1)])}
    rules = parse_rules(rules_text)
    pads = []
    for fi, pl in enumerate((r1, r2)):
        for pi, (nc, _name, ox, info) in enumerate(pl):
            pads.append((fi, pi, nc, info, ox, 0, MM // 2, 750_000))   # 1 x 1.5 mm pads
    iso = build_isolation_model(pads, rules, threshold=MM // 2)
    return BoardModel(footprints=fps, nets=nets, outline_xmin=0, outline_ymin=0,
                      outline_xmax=60 * MM, outline_ymax=30 * MM,
                      moveable_indices=[0, 1], isolation=iso)


class TestIsolationModel(unittest.TestCase):
    def test_box_gap(self):
        self.assertEqual(box_gap((0, 0, 1, 1), (5, 0, 1, 1)), 3)
        self.assertAlmostEqual(box_gap((0, 0, 1, 1), (5, 6, 1, 1)), 5.0)
        self.assertEqual(box_gap((0, 0, 1, 1), (1, 1, 1, 1)), 0.0)

    def test_requirements_and_reach(self):
        model = make_iso_model()
        iso = model.isolation
        self.assertTrue(iso.active)
        self.assertEqual(iso.max_req, 6 * MM)
        self.assertEqual(iso.fp_reach[1], 6 * MM)      # LV part must keep away from HV too

    def test_violation_matches_kicad_drc(self):
        # Same geometry as the kicad-cli check: R2 centre 6 mm right of R1 →
        # HV_IN pad edge to LV_OUT pad edge = 5.0 mm < 6 mm creepage, and the
        # HV_IN pad to its own GND pad (1.5 mm) is an intra-footprint problem.
        model = make_iso_model(x2=16 * MM)
        v = list_violations(model.isolation, model.footprints)
        pairs = {(x.fp_a, x.pad_a, x.fp_b, x.pad_b): (round(x.gap), x.required) for x in v}
        self.assertEqual(pairs[(0, 0, 1, 0)], (5 * MM, 6 * MM))
        self.assertEqual(pairs[(0, 0, 0, 1)], (1500_000, 6 * MM))
        # HV_IN to R2's GND pad is 7.5 mm: fine. Default-to-Default pairs
        # (0.2 mm) are below the threshold and never listed.
        self.assertNotIn((0, 0, 1, 1), pairs)
        self.assertEqual(len(pairs), 2)

    def test_cost_penalises_and_ignores_same_footprint(self):
        model = make_iso_model(x2=16 * MM)
        cs = CostState(model, quiet=True)
        self.assertGreater(cs._iso_penalty, 0.0)
        model.footprints[1].x = 40 * MM
        cs2 = CostState(model, quiet=True)
        # Only the R1-internal HV/GND pair remains, which placement cannot fix.
        self.assertEqual(cs2._iso_penalty, 0.0)

    def test_incremental_matches_full(self):
        random.seed(7)
        model = make_iso_model(x2=16 * MM)
        cs = CostState(model, quiet=True)
        for _ in range(300):
            undo = random.choice([lambda: do_translate(model, 0.5, 8 * MM),
                                  lambda: do_rotate(model),
                                  lambda: do_median(model, 0.5, MM),
                                  lambda: do_swap(model)])()
            if undo is None:
                continue
            ai = affected_indices(undo)
            snap = cs.snapshot(ai)
            cs.incremental_update(ai)
            if random.random() < 0.5:
                revert_move(model, undo)
                cs.restore(snap)
            full = CostState(model, quiet=True)
            self.assertAlmostEqual(cs._iso_penalty, full._iso_penalty, delta=1.0)
            self.assertAlmostEqual(cs.normalized_cost, full.normalized_cost, delta=5.0)

    def test_sa_separates_hv_from_lv(self):
        random.seed(3)
        model = make_iso_model(x2=13 * MM)
        run_sa(model, SAConfig(max_iterations=60, reheat_count=1))
        cs = CostState(model, quiet=True)
        self.assertAlmostEqual(cs._iso_penalty, 0.0, delta=1.0)
        self.assertAlmostEqual(cs._overlap_penalty, 0.0, delta=1.0)

    def test_locked_pair_is_not_penalised(self):
        model = make_iso_model(x2=16 * MM)
        for fp in model.footprints:
            fp.locked = True
        cs = CostState(model, quiet=True)
        self.assertEqual(cs._iso_penalty, 0.0)


class TestKeepOutsAndAreas(unittest.TestCase):
    def test_rectangle_detection(self):
        def mm(pts):
            return [(x * MM, y * MM) for x, y in pts]
        self.assertTrue(polygon_is_rectangle(mm([(0, 0), (10, 0), (10, 5), (0, 5)])))
        self.assertTrue(polygon_is_rectangle(mm([(0, 0), (5, 0), (10, 0), (10, 5), (0, 5)])))
        self.assertFalse(polygon_is_rectangle(mm([(1, 0), (9, 0), (10, 1), (10, 9), (9, 10),
                                                  (1, 10), (0, 9), (0, 1)])))   # chamfered
        self.assertFalse(polygon_is_rectangle(mm([(5, 0), (10, 5), (5, 10), (0, 5)])))  # diamond
        self.assertFalse(polygon_is_rectangle(mm([(0, 0), (10, 0), (10, 10), (5, 10),
                                                  (5, 5), (0, 5)])))             # L shape

    def test_keepout_side(self):
        ko = KeepOut(0, 0, 10, 10, sides=frozenset({'B'}))
        self.assertEqual(keepout_overlap(ko, 'F', 2, 2, 4, 4), (0, 0))
        self.assertEqual(keepout_overlap(ko, 'B', 2, 2, 4, 4), (2, 2))

    def test_keepout_polygon(self):
        # Triangle in the lower-left half of its 10x10 bbox
        ko = KeepOut(0, 0, 10, 10, polygon=[(0, 0), (0, 10), (10, 10)])
        self.assertEqual(keepout_overlap(ko, 'F', 7, 1, 9, 3), (0, 0))   # upper-right: outside
        self.assertNotEqual(keepout_overlap(ko, 'F', 1, 7, 3, 9), (0, 0))
        self.assertTrue(rect_intersects_polygon(4, 4, 6, 6, [(0, 0), (0, 10), (10, 10)]))

    def test_area_members(self):
        fps = [Footprint('Q1', 0, 0, 0, 0, 1, 1, False, sheet='/Power/LLC/', component_classes=('HV',)),
               Footprint('U1', 1, 0, 0, 0, 1, 1, False, sheet='/MCU/', groups=('G1',)),
               Footprint('Q2', 2, 0, 0, 0, 1, 1, False, sheet='/Power/')]
        self.assertEqual(area_members(fps, AREA_SHEET, '/Power/'), {0, 2})
        self.assertEqual(area_members(fps, AREA_SHEET, '/Power/LLC'), {0})
        self.assertEqual(area_members(fps, AREA_COMPONENT_CLASS, 'HV'), {0})
        self.assertEqual(area_members(fps, AREA_GROUP, 'G1'), {1})
        self.assertIsNone(area_members(fps, 3, 'block'))

    def _area_model(self, exclusive=True):
        fps = [Footprint('Q1', 0, 40 * MM, 10 * MM, 0, 4 * MM, 4 * MM, False),
               Footprint('U1', 1, 5 * MM, 5 * MM, 0, 4 * MM, 4 * MM, False)]
        area = PlacementArea(0, 0, 20 * MM, 20 * MM, members={0}, name='Power',
                             exclusive=exclusive)
        return BoardModel(footprints=fps, nets={}, outline_xmin=0, outline_ymin=0,
                          outline_xmax=60 * MM, outline_ymax=40 * MM,
                          moveable_indices=[0, 1], placement_areas=[area])

    def test_member_outside_and_intruder_penalised(self):
        cs = CostState(self._area_model(), quiet=True)
        self.assertGreater(cs._fp_area.get(0, 0), 0)       # Q1 outside its area
        self.assertGreater(cs._fp_area.get(1, 0), 0)       # U1 inside an exclusive area
        self.assertEqual(len(cs.area_violations()), 2)
        cs2 = CostState(self._area_model(exclusive=False), quiet=True)
        self.assertNotIn(1, cs2._fp_area)

    def test_sa_moves_members_into_area(self):
        random.seed(5)
        model = self._area_model()
        run_sa(model, SAConfig(max_iterations=60, reheat_count=1))
        cs = CostState(model, quiet=True)
        self.assertEqual(cs.area_violations(), [])


if __name__ == '__main__':
    unittest.main()
