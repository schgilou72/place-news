"""Tests for the Specctra round trip helpers, the rule semantics they rely on
(item kinds, net-level creepage) and the routing report — no KiCad needed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from plugin.rules import (PadInfo, parse_rules, compile_condition, track_item, via_item,  # noqa: E402
                          set_board_layers)
from plugin.isolation import build_isolation_model, list_violations  # noqa: E402
from plugin import specctra  # noqa: E402
from plugin.specctra import (NetRouteInfo, RuleAreaInfo, apply_isolation_classes,  # noqa: E402
                             fix_rule_area_keepouts, parse_dsn, write_dsn, _find, _find_all,
                             _drop_section, NO_NET)
from plugin.route_report import (class_label, describe_class_rules, progress_text,  # noqa: E402
                                 summarize_result)

MM = 1_000_000

HV = PadInfo(net_name='HV_IN', netclasses=('HV', 'Default'), nc_clearance=1 * MM)
LV = PadInfo(net_name='LV', netclasses=('Default',), nc_clearance=200_000)

DSN = '''(pcb /tmp/x.dsn
  (parser
    (string_quote ")
    (space_in_quoted_tokens on)
    (host_cad "KiCad's Pcbnew")
    (host_version "10.0.6")
  )
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu (type signal) (property (index 0)))
    (layer B.Cu (type signal) (property (index 1)))
    (boundary (path pcb 0  0 0  100000 0  100000 -60000  0 -60000  0 0))
    (keepout "" (polygon F.Cu 0  66000 -2000  98000 -2000  98000 -58000  66000 -58000  66000 -2000))
    (wire_keepout "" (polygon B.Cu 0  0 0  9000 0  9000 -9000  0 -9000  0 0))
    (keepout "" (polygon F.Cu 0  10000 -10000  20000 -10000  20000 -20000  10000 -20000  10000 -10000))
    (via "Via[0-1]_600:300_um")
    (rule
      (width 250)
      (clearance 200)
      (clearance 50 (type smd_smd))
    )
  )
  (network
    (net HV_IN (pins J1-1 U1-1))
    (net "HV-" (pins J1-2))
    (net LV (pins U1-2 J2-1))
    (net GND (pins U1-3 J2-2))
    (class kicad_default GND LV
      (circuit (use_via "Via[0-1]_600:300_um"))
      (rule (width 250) (clearance 200))
    )
    (class HV,Default HV_IN "HV-"
      (circuit (use_via "Via[0-1]_800:400_um"))
      (rule (width 500) (clearance 1000))
    )
  )
)
'''

CREEPAGE = '''(version 1)
(rule "HV creepage" (constraint creepage (min 6mm))
  (condition "A.hasNetclass('HV') && !B.hasNetclass('HV')"))
'''


def nets_for(rules_attrs=()):
    pad = lambda name, ncs, clr: PadInfo(net_name='', netclasses=ncs, nc_clearance=clr)  # noqa: E731
    return {
        'HV_IN': NetRouteInfo('HV_IN', ('HV', 'Default'), MM, {pad('HV_IN', ('HV', 'Default'), MM)}),
        'HV-': NetRouteInfo('HV-', ('HV', 'Default'), MM, {pad('HV-', ('HV', 'Default'), MM)}),
        'LV': NetRouteInfo('LV', ('Default',), 200_000, {pad('LV', ('Default',), 200_000)}),
        'GND': NetRouteInfo('GND', ('Default',), 200_000, {pad('GND', ('Default',), 200_000)}),
    }


class TestItemKinds(unittest.TestCase):
    def ev(self, src, a, b):
        fn, _ = compile_condition(src)
        return bool(fn(a, b))

    def test_type_of_tracks_and_vias(self):
        t = track_item('HV_IN', ('HV', 'Default'), MM, 'B.Cu')
        v = via_item('HV_IN', ('HV', 'Default'), MM)
        self.assertTrue(self.ev("A.Type == 'Track'", t, LV))
        self.assertTrue(self.ev("A.Type == 'Via'", v, LV))
        self.assertTrue(self.ev("A.Type == 'Pad'", HV, LV))
        self.assertTrue(self.ev("A.isPlated()", v, LV))
        self.assertTrue(self.ev("A.existsOnLayer('B.Cu')", t, LV))
        self.assertFalse(self.ev("A.existsOnLayer('F.Cu')", t, LV))

    def test_missing_property_is_neither_equal_nor_different(self):
        t = track_item('X', ('Default',), 0)
        self.assertFalse(self.ev("A.Pad_Type == 'SMD'", t, LV))
        self.assertFalse(self.ev("A.Pad_Type != 'SMD'", t, LV))
        self.assertTrue(self.ev("A.Pad_Type == 'SMD'", LV, t))

    def test_footprint_functions_false_for_tracks(self):
        t = track_item('X', ('Default',), 0)
        self.assertFalse(self.ev("A.memberOfSheet('/')", t, LV))
        self.assertTrue(self.ev("A.memberOfSheet('/')", PadInfo(sheet='/'), LV))
        self.assertFalse(self.ev("A.memberOfFootprint('*')", t, LV))
        self.assertFalse(self.ev("A.hasComponentClass('X')", t, LV))

    def test_wildcards_and_case(self):
        a = PadInfo(net_name='HV_IN', sheet='/Power/LLC/', fp_ref='T1',
                    fp_lib_id='Magnetics:EE25', component_classes=('HV_PARTS',),
                    groups=('Bridge',))
        self.assertTrue(self.ev("A.NetName == 'hv_*'", a, LV))
        self.assertFalse(self.ev("'HV*' == A.NetName", a, LV))     # wildcard only on the right
        self.assertTrue(self.ev("A.memberOfSheet('/Power/*')", a, LV))
        self.assertFalse(self.ev("A.memberOfSheet('/power/llc')", a, LV))   # case-sensitive
        self.assertTrue(self.ev("A.memberOfSheetOrChildren('/Pow*')", a, LV))
        self.assertTrue(self.ev("A.memberOfFootprint('Magnetics:EE*')", a, LV))
        self.assertTrue(self.ev("A.memberOfFootprint('${CLASS:HV_PARTS}')", a, LV))
        self.assertFalse(self.ev("A.hasComponentClass('hv_parts')", a, LV))
        self.assertTrue(self.ev("A.memberOfGroup('Br*')", a, LV))


class TestNetLevelCreepage(unittest.TestCase):
    def test_creepage_rules_see_tracks(self):
        # KiCad evaluates creepage with stand-in tracks: a pad-only condition never fires.
        rs = parse_rules('''(version 1)
            (rule "pads" (constraint creepage (min 6mm))
              (condition "A.Type == 'Pad' && A.hasNetclass('HV')"))
            (rule "fp" (constraint creepage (min 7mm)) (condition "A.memberOfFootprint('T1')"))''')
        hv_t1 = PadInfo(net_name='HV_IN', netclasses=('HV', 'Default'), nc_clearance=MM, fp_ref='T1')
        self.assertEqual(rs.requirement(hv_t1, LV), MM)

    def test_clearance_rules_see_the_real_items(self):
        rs = parse_rules('''(version 1)
            (rule "fp" (constraint clearance (min 4mm)) (condition "A.memberOfFootprint('T1')"))''')
        hv_t1 = PadInfo(net_name='HV_IN', netclasses=('HV', 'Default'), nc_clearance=MM, fp_ref='T1')
        self.assertEqual(rs.requirement(hv_t1, LV), 4 * MM)
        self.assertEqual(rs.requirement(track_item('HV_IN', ('HV', 'Default'), MM), LV), MM)

    def test_creepage_takes_the_worst_copper_layer(self):
        rs = parse_rules('''(version 1)
            (rule "back" (constraint creepage (min 5mm)) (condition "A.Layer == 'B.Cu'"))''')
        self.assertEqual(rs.requirement(HV, LV), 5 * MM)
        rs1 = parse_rules('''(version 1)
            (rule "back" (constraint creepage (min 5mm)) (condition "A.Layer == 'B.Cu'"))''',
                          copper_layers=('F.Cu',))
        self.assertEqual(rs1.requirement(HV, LV), MM)

    def test_non_plated_hole_only_counts_for_creepage(self):
        hole = PadInfo(net_name='', netclasses=('Default',), nc_clearance=200_000, pad_type='npth',
                       layers=('*.Cu',))
        self.assertEqual(parse_rules(CREEPAGE).requirement(HV, hole), 6 * MM)
        clearance_only = parse_rules('''(version 1)
            (rule "c" (constraint clearance (min 4mm)) (condition "A.hasNetclass('HV')"))''')
        self.assertEqual(clearance_only.requirement(HV, hole), 0)


class TestDsn(unittest.TestCase):
    def test_round_trip(self):
        tree = parse_dsn(DSN)
        again = parse_dsn(write_dsn(tree))
        self.assertEqual(tree, again)
        self.assertIn('(string_quote ")', write_dsn(tree))
        self.assertIn('(net "HV-"', write_dsn(tree))

    def test_rule_area_keepouts(self):
        tree = parse_dsn(DSN)
        areas = [
            RuleAreaInfo(('F.Cu',), (66 * MM, 2 * MM, 98 * MM, 58 * MM), False, False, 'SECONDARY'),
            RuleAreaInfo(('B.Cu',), (0, 0, 9 * MM, 9 * MM), True, False, 'KO'),
        ]
        stats = fix_rule_area_keepouts(tree, areas)
        self.assertEqual(stats, {'kept': 0, 'wire_only': 1, 'via_only': 0, 'removed': 1, 'unmatched': 1})
        structure = _find(tree, 'structure')
        self.assertEqual(len(_find_all(structure, 'keepout')), 1)
        self.assertEqual(len(_find_all(structure, 'wire_keepout')), 1)

    def test_same_outline_areas_keep_their_own_keepout(self):
        # A placement area and a "no tracks" area with the same outline: KiCad
        # writes a keepout and a wire_keepout; only the first one goes.
        tree = parse_dsn(DSN.replace(
            '(via "Via[0-1]_600:300_um")',
            '(wire_keepout "" (polygon F.Cu 0  66000 -2000  98000 -2000  98000 -58000  66000 -58000'
            '  66000 -2000))\n    (via "Via[0-1]_600:300_um")'))
        box = (66 * MM, 2 * MM, 98 * MM, 58 * MM)
        areas = [RuleAreaInfo(('F.Cu',), box, False, False, 'PLACEMENT'),
                 RuleAreaInfo(('F.Cu',), box, True, False, 'NO_TRACKS')]
        stats = fix_rule_area_keepouts(tree, areas)
        self.assertEqual(stats['removed'], 1)
        self.assertEqual(stats['wire_only'], 1)
        structure = _find(tree, 'structure')
        wk = _find_all(structure, 'wire_keepout')
        self.assertTrue(any(w[2][1] == 'F.Cu' for w in wk))

    def test_isolation_classes(self):
        tree = parse_dsn(DSN)
        rules = parse_rules(CREEPAGE)
        rep = apply_isolation_classes(tree, nets_for(), rules)
        self.assertEqual(rep.pairs, {('kicad_default', 'HV,Default'): 6 * MM})
        self.assertEqual(rep.classes, {})
        self.assertTrue(rep.removed_smd_rule)
        network = _find(tree, 'network')
        cc = _find_all(network, 'class_class')
        self.assertEqual(cc, [['class_class', ['classes', 'kicad_default', 'HV,Default'],
                               ['rule', ['clearance', '6000']]]])
        struct_rule = _find(_find(tree, 'structure'), 'rule')
        self.assertNotIn(['clearance', '50', ['type', 'smd_smd']], struct_rule)
        # Applying twice does not stack rules.
        apply_isolation_classes(tree, nets_for(), rules)
        self.assertEqual(len(_find_all(network, 'class_class')), 1)

    def test_same_class_rule_raises_class_clearance(self):
        tree = parse_dsn(DSN)
        rules = parse_rules('''(version 1)
            (rule "hv-hv" (constraint clearance (min 2mm))
              (condition "A.hasNetclass('HV') && B.hasNetclass('HV')"))''')
        rep = apply_isolation_classes(tree, nets_for(), rules)
        self.assertEqual(rep.classes, {'HV,Default': 2 * MM})
        hv = [c for c in _find_all(_find(tree, 'network'), 'class') if c[1] == 'HV,Default'][0]
        self.assertIn(['clearance', '2000'], _find(hv, 'rule'))
        # Freerouting would now use max(class clearances) = 2 mm between HV and
        # Default; KiCad only asks 1 mm there, so a class_class rule says so.
        self.assertEqual(rep.pairs, {('kicad_default', 'HV,Default'): MM})
        self.assertTrue(rep.removed_smd_rule)

    def test_no_rules_no_change(self):
        tree = parse_dsn(DSN)
        rep = apply_isolation_classes(tree, nets_for(), parse_rules('(version 1)'))
        self.assertFalse(rep.changed)
        self.assertEqual(tree, parse_dsn(DSN))

    def test_drop_placement_from_session(self):
        ses = ('(session x (base_design x)\n  (placement (resolution um 10)\n'
               '    (component "R 1" (place R1 1 2 front 0) (place "MH_1" 3 4 front 0)))\n'
               '  (routes (resolution um 10) (network_out (net A (wire (path F.Cu 250 0 0 10 10))))))')
        out = _drop_section(ses, 'placement')
        self.assertNotIn('placement', out)
        self.assertIn('(routes', out)
        self.assertEqual(out.count('('), out.count(')'))


class TestLayers(unittest.TestCase):
    def tearDown(self):
        set_board_layers(('F.Cu', 'B.Cu'))

    def ev(self, src, a, b):
        fn, _ = compile_condition(src)
        return bool(fn(a, b))

    def test_wildcards_in_rules_match_item_layers(self):
        set_board_layers(('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'))
        t = track_item('X', ('Default',), 0, 'F.Cu')
        tht = PadInfo(pad_type='tht', layers=('*.Cu',))
        self.assertTrue(self.ev("A.Layer == '?.Cu'", t, LV))
        self.assertFalse(self.ev("A.existsOnLayer('In*.Cu')", t, LV))
        self.assertTrue(self.ev("A.existsOnLayer('In*.Cu')", tht, LV))
        self.assertFalse(self.ev("A.Layer == 'f.cu'", t, LV))         # case-sensitive, as KiCad

    def test_user_layer_names(self):
        set_board_layers(('F.Cu', 'B.Cu'), {'Top': 'F.Cu'})
        self.assertTrue(self.ev("A.Layer == 'Top'", track_item('X', ('Default',), 0, 'F.Cu'), LV))
        self.assertFalse(self.ev("A.Layer == 'Top'", track_item('X', ('Default',), 0, 'B.Cu'), LV))

    def test_layer_clause_limits_a_rule_to_its_layers(self):
        text = ('(version 1)\n'
                '(rule "general" (constraint clearance (min 2mm)))\n'
                '(rule "inner relaxed" (layer inner) (constraint clearance (min 0.1mm)))')
        two = parse_rules(text, copper_layers=('F.Cu', 'B.Cu'))
        self.assertEqual(two.requirement(HV, LV), 2 * MM)              # outer layers only
        four = parse_rules(text, copper_layers=('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'))
        inner_a = track_item('A', ('Default',), 0, 'In1.Cu')
        inner_b = track_item('B', ('Default',), 0, 'In1.Cu')
        self.assertEqual(four.requirement(inner_a, inner_b), 100_000)
        # Through-hole items meet on every layer: the outer layers decide.
        tht_a = PadInfo(net_name='A', pad_type='tht', layers=('*.Cu',))
        tht_b = PadInfo(net_name='B', pad_type='tht', layers=('*.Cu',))
        self.assertEqual(four.requirement(tht_a, tht_b), 2 * MM)


class TestNetSignatures(unittest.TestCase):
    def test_nets_grouped_by_the_names_rules_mention(self):
        rs = parse_rules('(version 1)\n(rule "bus" (constraint clearance (min 3mm)) '
                         '(condition "A.NetName == \'HV_*\'"))')
        self.assertEqual(rs.netname_literals, frozenset({('HV_*', True)}))
        canon = rs.net_canonicalizer()
        self.assertEqual(canon('HV_A'), canon('HV_B'))
        self.assertNotEqual(canon('HV_A'), canon('LV'))
        self.assertEqual(canon('LV'), canon('GND'))

    def test_other_netname_uses_keep_nets_apart(self):
        rs = parse_rules('(version 1)\n(rule "x" (constraint clearance (min 3mm)) '
                         '(condition "A.NetName == B.NetName"))')
        self.assertIsNone(rs.netname_literals)
        canon = rs.net_canonicalizer()
        self.assertNotEqual(canon('LV'), canon('GND'))


class TestNetlessItems(unittest.TestCase):
    def test_pads_without_net_get_isolation_in_the_router(self):
        # Every net is HV; only unconnected pins sit in the default class.
        tree = parse_dsn(DSN.replace('(class kicad_default GND LV', '(class kicad_default'))
        nets = {k: v for k, v in nets_for().items() if k.startswith('HV')}
        nets[NO_NET] = NetRouteInfo(NO_NET, ('Default',), 200_000,
                                    {PadInfo(netclasses=('Default',), nc_clearance=200_000)},
                                    routable=False)
        rules = parse_rules('(version 1)\n(rule "HV clearance" (constraint clearance (min 4mm))\n'
                            '  (condition "A.hasNetclass(\'HV\') && !B.hasNetclass(\'HV\')"))')
        rep = apply_isolation_classes(tree, nets, rules)
        self.assertEqual(rep.pairs, {('kicad_default', 'HV,Default'): 4 * MM})

    def test_no_creepage_between_two_holes(self):
        # KiCad never tests creepage between two items without net.
        from plugin.board_model import Footprint, Pad
        rules = parse_rules('(version 1) (rule "c" (constraint creepage (min 3mm)))')
        hole = PadInfo(net_name='', pad_type='npth', layers=('*.Cu',))
        fp = Footprint(reference='J1', index=0, x=10 * MM, y=10 * MM, angle_deg=0.0,
                       width=8 * MM, height=4 * MM, locked=False, uuid='u0',
                       pads=[Pad(net_code=0, net_name='', offset_x=-MM, offset_y=0),
                             Pad(net_code=0, net_name='', offset_x=MM, offset_y=0)])
        iso = build_isolation_model([(0, 0, 0, hole, -MM, 0, 500_000, 500_000),
                                     (0, 1, 0, hole, MM, 0, 500_000, 500_000)], rules, 100_000)
        self.assertEqual(list_violations(iso, [fp]), [])


class TestDrcGuards(unittest.TestCase):
    def test_unsaved_board_is_not_checked(self):
        class Board:
            def GetFileName(self):
                return ''
        d = specctra.drc_summary(Board(), '/usr/bin/kicad-cli')
        self.assertIn('never been saved', d.error)
        text = '\n'.join(summarize_result(specctra.RouteResult(ok=True, ses='', log=[]),
                                          None, d, 1.0, True))
        self.assertIn('never been saved', text)


class TestReport(unittest.TestCase):
    def test_labels_and_progress(self):
        self.assertEqual(class_label('kicad_default'), 'Default')
        self.assertEqual(class_label('HV,Default'), 'HV')
        self.assertEqual(class_label('A,B,Default'), 'A,B')
        line = ('2026-10-04 00:01:38.086 INFO   [375443\\B29231] Auto-routing pass #3 on board '
                "'x' was completed in 0.57 seconds with score 999.99 (2 unrouted and 1 violations)")
        self.assertEqual(progress_text(line), 'Routing pass 3: 2 unrouted, 1 violations')
        self.assertEqual(progress_text('2026-10-04 00:01:38.086 INFO   Hello'), 'Hello')

    def test_summary(self):
        rep = specctra.ClassRulesReport(pairs={('SEC,Default', 'HV,Default'): 6 * MM},
                                        original={'HV,Default': MM})
        self.assertEqual(describe_class_rules(rep), ['HV ↔ SEC: 6.00 mm'])
        result = specctra.RouteResult(ok=True, ses='x.ses', log=[], unrouted=0, violations=0)
        export = specctra.ExportReport(dsn='x.dsn', keepouts={'removed': 2}, classes=rep,
                                       rules=parse_rules('(version 1)'))
        drc = specctra.DrcSummary(counts={}, between_parts={}, inside_parts={'creepage': 2},
                                  rule_area={}, unconnected=0, report_path='')
        text = '\n'.join(summarize_result(result, export, drc, 12.0, True))
        self.assertIn('Connections left unrouted: 0', text)
        self.assertIn('HV ↔ SEC: 6.00 mm', text)
        self.assertIn('inside footprints', text)
        self.assertIn('Edit > Undo', text)


if __name__ == '__main__':
    unittest.main()
