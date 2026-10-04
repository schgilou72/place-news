"""Tests for the aesthetic check, the cost criteria (layers, vias, ratsnest),
the learning loop, the report, the pour geometry and the layer trial — no
KiCad dependency."""
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from plugin import aesthetic_check as ac  # noqa: E402
from plugin.aesthetic_check import (BoardGeom, PadGeom, PartGeom, SegGeom, ViaGeom,  # noqa: E402
                                    check, eic_density, eic_layers, estimate_layers)
from plugin.aesthetic_learning import (DEFAULT_WEIGHTS, POLICY_RANGE, Learner,  # noqa: E402
                                       changed_state)
from plugin.aesthetic_report import build_html, render_svg  # noqa: E402
from plugin.aesthetics import Ratsnest, untangle  # noqa: E402
from plugin.cost_function import CostState  # noqa: E402
from plugin.pour import convex_hull, is_ground_name  # noqa: E402
from plugin import specctra  # noqa: E402

from tests.test_aesthetics import model_of, part  # noqa: E402

MM = 1_000_000


def two_pad(ref, idx, x, y, nets, lib='R_0805', locked=False):
    return part(ref, idx, x * MM, y * MM, lib=lib, nets=nets, locked=locked)


def pin(ref, idx, x, y, net, lib='TP'):
    """A locked one-pad part (a connector pin)."""
    from plugin.board_model import Footprint, Pad
    return Footprint(reference=ref, index=idx, x=int(x * MM), y=int(y * MM), angle_deg=0.0,
                     width=MM, height=MM, locked=True,
                     pads=[Pad(net_code=net, net_name=f'N{net}', offset_x=0, offset_y=0)],
                     net_codes={net}, uuid=f'p{idx}', lib_id=f'Lib:{lib}')


# ---------------------------------------------------------------------------
# Untangling the ratsnest
# ---------------------------------------------------------------------------

class TestUntangle(unittest.TestCase):
    def test_two_pad_part_turns_round(self):
        # R1's left pad goes up-right, its right pad up-left: the lines cross.
        fps = [two_pad('R1', 0, 30, 20, (1, 2)), pin('J1', 1, 20, 10, 2), pin('J2', 2, 40, 10, 1)]
        model = model_of(fps)
        self.assertEqual(Ratsnest(model).crossings(), 1)
        cs = CostState(model, quiet=True)
        rep = untangle(model, cs)
        self.assertEqual((rep.crossings_before, rep.crossings_after), (1, 0))
        self.assertEqual(rep.flips, 1)
        self.assertEqual(int(round(model.footprints[0].angle_deg)) % 360, 180)
        self.assertLessEqual(rep.hpwl_after, rep.hpwl_before)

    def test_identical_parts_swap_places(self):
        fps = [two_pad('R1', 0, 20, 20, (1, 2)), two_pad('R2', 1, 40, 20, (3, 4)),
               pin('J1', 2, 39, 30, 1), pin('J2', 3, 41, 30, 2),
               pin('J3', 4, 19, 30, 3), pin('J4', 5, 21, 30, 4)]
        model = model_of(fps)
        self.assertEqual(Ratsnest(model).crossings(), 4)
        cs = CostState(model, quiet=True)
        rep = untangle(model, cs)
        self.assertEqual(rep.crossings_after, 0)
        self.assertEqual(rep.swaps, 1)
        self.assertEqual(cs._overlap_penalty, 0)
        self.assertGreater(model.footprints[0].x, model.footprints[1].x)   # R1 now on the right

    def test_no_move_without_a_gain(self):
        fps = [two_pad('R1', 0, 20, 20, (1, 2)), pin('J1', 1, 10, 20, 1), pin('J2', 2, 30, 20, 2)]
        model = model_of(fps)
        rep = untangle(model, CostState(model, quiet=True))
        self.assertEqual((rep.crossings_before, rep.swaps, rep.flips), (0, 0, 0))

    def test_rules_still_win(self):
        # The swap would untangle everything, but R1 belongs to an exclusive
        # placement area: R1 may not leave it, R2 may not enter it.
        from plugin.board_model import PlacementArea
        fps = [two_pad('R1', 0, 20, 20, (1, 2)), two_pad('R2', 1, 40, 20, (3, 4)),
               pin('J1', 2, 39, 30, 1), pin('J2', 3, 41, 30, 2),
               pin('J3', 4, 19, 30, 3), pin('J4', 5, 21, 30, 4)]
        model = model_of(fps)
        model.placement_areas = [PlacementArea(15 * MM, 15 * MM, 25 * MM, 25 * MM, members={0},
                                               exclusive=True)]
        cs = CostState(model, quiet=True)
        rep = untangle(model, cs)
        self.assertEqual(rep.swaps, 0)
        self.assertEqual(rep.crossings_after, 4)
        self.assertEqual(cs._area_penalty, 0)

    def test_swap_within_budget_only_when_it_shortens(self):
        fps = [two_pad('R1', 0, 20, 20, (1, 2)), two_pad('R2', 1, 40, 20, (3, 4)),
               pin('J1', 2, 39, 30, 1), pin('J2', 3, 41, 30, 2),
               pin('J3', 4, 19, 30, 3), pin('J4', 5, 21, 30, 4)]
        model = model_of(fps)
        cs = CostState(model, quiet=True)
        h0 = cs.hpwl
        rep = untangle(model, cs, budget_ratio=0.0)
        self.assertEqual(rep.crossings_after, 0)     # shorter wires: no budget needed
        self.assertLess(cs.hpwl, h0)


# ---------------------------------------------------------------------------
# Aesthetic check on synthetic geometry
# ---------------------------------------------------------------------------

def geom_with(parts=(), pads=(), segs=(), vias=(), **kw):
    g = BoardGeom(parts=list(parts), pads=list(pads), segs=list(segs), vias=list(vias),
                  outline=(0, 0, 50 * MM, 40 * MM))
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def rpart(ref, x, y, angle=0.0, type_name='R_0805', side='F', ref_box=None, ref_angle=0.0):
    box = (int((x - 1.5) * MM), int((y - 0.75) * MM), int((x + 1.5) * MM), int((y + 0.75) * MM))
    if ref_box is None:
        ref_box = (int((x - 0.6) * MM), int((y - 0.4) * MM), int((x + 0.6) * MM), int((y + 0.4) * MM))
    return PartGeom(ref=ref, uuid=ref, type_name=type_name, angle=angle, box=box, side=side,
                    ref_box=ref_box, ref_angle=ref_angle)


def pad_at(part, net, x, y, w=1.0, h=1.2, smd=True):
    c = (int(x * MM), int(y * MM))
    return PadGeom(part=part, net=net, center=c,
                   box=(c[0] - int(w * MM / 2), c[1] - int(h * MM / 2),
                        c[0] + int(w * MM / 2), c[1] + int(h * MM / 2)), smd=smd)


def seg(net, a, b, width=0.25, layer='F.Cu'):
    return SegGeom(net, layer, (int(a[0] * MM), int(a[1] * MM)), (int(b[0] * MM), int(b[1] * MM)),
                   int(width * MM))


class TestPlacementCriteria(unittest.TestCase):
    def test_alignment(self):
        r = check(geom_with(parts=[rpart('R1', 10, 20), rpart('R2', 16, 20.3)]))
        self.assertEqual(r.features['alignment'], 1.0)
        r = check(geom_with(parts=[rpart('R1', 10, 20), rpart('R2', 16, 20)]))
        self.assertEqual(r.features['alignment'], 0.0)

    def test_orientation(self):
        parts = [rpart('R1', 10, 10), rpart('R2', 20, 10), rpart('R3', 30, 10, angle=90)]
        r = check(geom_with(parts=parts))
        self.assertAlmostEqual(r.features['orientation'], 1 / 3)
        self.assertIn('R3', [i.message.split()[0] for i in r.issues if i.criterion == 'orientation'])

    def test_references(self):
        far = (int(30 * MM), int(30 * MM), int(31 * MM), int(31 * MM))
        parts = [rpart('R1', 10, 10, ref_angle=45), rpart('R2', 20, 10, ref_box=far)]
        pads = [pad_at(0, 'A', 9, 10), pad_at(0, 'B', 11, 10)]
        r = check(geom_with(parts=parts, pads=pads))
        self.assertEqual(r.features['ref_readable'], 0.5)       # R1 at 45°
        self.assertEqual(r.features['ref_position'], 0.5)       # R2 far away
        self.assertEqual(r.features['ref_clear'], 0.5)          # R1's text over its pads


class TestRoutingCriteria(unittest.TestCase):
    def test_angles_and_acute(self):
        pads = [pad_at(-1, 'A', 0, 0), pad_at(-1, 'A', 20, 0)]
        segs = [seg('A', (0, 0), (10, 5.77)), seg('A', (10, 5.77), (20, 0))]   # 30° legs
        r = check(geom_with(pads=pads, segs=segs))
        self.assertEqual(r.features['angles'], 1.0)
        self.assertEqual(r.features['acute'], 0.0)              # 120° between the legs
        segs = [seg('A', (0, 0), (10, 0)), seg('A', (10, 0), (2, 3))]          # sharp turn back
        r = check(geom_with(pads=pads, segs=segs))
        self.assertEqual(r.features['acute'], 1.0)

    def test_jog(self):
        pads = [pad_at(-1, 'A', 0, 0), pad_at(-1, 'A', 20, 0.2)]
        segs = [seg('A', (0, 0), (10, 0)), seg('A', (10, 0), (10, 0.2)), seg('A', (10, 0.2), (20, 0.2))]
        r = check(geom_with(pads=pads, segs=segs))
        self.assertGreater(r.features['jogs'], 0.0)

    def test_detour_and_vias(self):
        pads = [pad_at(-1, 'A', 0, 0), pad_at(-1, 'A', 10, 0)]
        segs = [seg('A', (0, 0), (0, 10)), seg('A', (0, 10), (10, 10)), seg('A', (10, 10), (10, 0))]
        vias = [ViaGeom('A', (0, 10 * MM), int(0.6 * MM))]
        r = check(geom_with(pads=pads, segs=segs, vias=vias))
        self.assertAlmostEqual(r.features['detours'], 3.0 - 1.3, places=3)
        self.assertEqual(r.features['vias'], 1.0)


class TestCostCriteria(unittest.TestCase):
    def test_crossings_drills_tracks_sides(self):
        parts = [rpart('R1', 10, 10), rpart('R2', 20, 10, side='B')]
        pads = [pad_at(0, 'A', 0, 0), pad_at(0, 'A', 10, 10), pad_at(1, 'B', 0, 10), pad_at(1, 'B', 10, 0)]
        holes = [((0, 0), d) for d in (200_000, 300_000, 400_000, 800_000, 1_000_000)]
        segs = [seg('A', (0, 0), (10, 10), width=0.1)]
        r = check(geom_with(parts=parts, pads=pads, segs=segs, holes=holes))
        self.assertEqual(r.features['crossings'], 0.5)          # 1 crossing, 2 ratsnest lines
        self.assertAlmostEqual(r.features['drill_sizes'], 2 / 3)
        self.assertAlmostEqual(r.features['small_drills'], 1 / 5)
        self.assertEqual(r.features['fine_tracks'], 1.0)
        self.assertEqual(r.features['two_sides'], 0.5)

    def test_planes_are_left_out_of_the_ratsnest(self):
        pads = [pad_at(-1, 'GND', 0, 0), pad_at(-1, 'GND', 10, 10),
                pad_at(-1, 'B', 0, 10), pad_at(-1, 'B', 10, 0)]
        r = check(geom_with(pads=pads, plane_nets=('GND',)))
        self.assertEqual(r.features['crossings'], 0.0)

    def test_empty_inner_layers(self):
        g = geom_with(copper_layers=('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'), zone_layers=('In1.Cu',),
                      segs=[seg('A', (0, 0), (5, 0))], pads=[pad_at(-1, 'A', 0, 0), pad_at(-1, 'A', 5, 0)])
        r = check(g)
        self.assertEqual(r.features['inner_layers'], 0.25)       # In2.Cu carries nothing


class TestLayerEstimate(unittest.TestCase):
    def board(self, n_nets=20, size=50, layers=2, spread=30):
        rng = random.Random(3)
        pads = []
        for n in range(n_nets):
            for _ in range(3):
                pads.append(pad_at(-1, f'N{n}', rng.uniform(5, 5 + spread), rng.uniform(5, 5 + spread)))
        parts = [rpart(f'R{k}', 10 + 3 * (k % 10), 10 + 3 * (k // 10)) for k in range(20)]
        g = geom_with(parts=parts, pads=pads,
                      copper_layers=tuple(['F.Cu'] + [f'In{i}.Cu' for i in range(1, layers - 1)] + ['B.Cu']))
        g.outline = (0, 0, size * MM, size * MM)
        return g

    def test_formula(self):
        g = self.board()
        e = estimate_layers(g)
        parts_area = 20 * 3.0 * 1.5
        self.assertEqual(e.nets, 20)
        self.assertAlmostEqual(e.area_per_net(2), (2500 * 2 - parts_area) / 20, places=6)
        self.assertAlmostEqual(e.area_per_net(4), (2500 * 4 - parts_area) / 20, places=6)

    def test_room_against_need(self):
        roomy = estimate_layers(self.board(size=100))
        self.assertEqual(roomy.needed_layers, 2)
        self.assertIn('fit', roomy.verdict())
        dense = estimate_layers(self.board(n_nets=120, size=12, spread=10))
        self.assertGreater(dense.needed_layers, 2)
        self.assertIn('short', dense.verdict())
        self.assertGreater(check(self.board(n_nets=120, size=12, spread=10)).features['layer_count'], 0)

    def test_extra_routing_layers_cost(self):
        g = self.board(size=100, layers=6)
        r = check(g)
        self.assertGreater(r.features['layer_count'], 0.0)
        self.assertTrue(any(i.criterion == 'layer_count' for i in r.issues))
        # Inner layers holding planes are not routing room, nor a cost.
        g.plane_layers = ('In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu')
        self.assertEqual(check(g).features['layer_count'], 0.0)

    def test_classic_pin_density_table(self):
        self.assertAlmostEqual(eic_density(645.16, 14), 1.0)
        self.assertEqual(eic_layers(1.46), (2, 2))
        self.assertEqual(eic_layers(1.0), (2, 4))
        self.assertEqual(eic_layers(0.5), (4, 6))
        self.assertEqual(eic_layers(0.35), (6, 8))
        self.assertEqual(eic_layers(0.25), (8, 12))
        self.assertEqual(eic_layers(0.05), (10, 14))
        e = estimate_layers(self.board())
        self.assertIn('pin-density', e.describe_eic())

    def test_bga_escape(self):
        # 10 x 10 balls at 0.8 mm: 5 rings, no track between the balls -> 5 layers.
        pads = []
        for i in range(10):
            for j in range(10):
                pads.append(pad_at(0, f'S{(10 * i + j) // 2}', 20 + 0.8 * i, 20 + 0.8 * j,
                                   w=0.4, h=0.4))
        part = PartGeom(ref='U1', uuid='U1', type_name='BGA100', angle=0.0,
                        box=(int(19 * MM), int(19 * MM), int(28.5 * MM), int(28.5 * MM)))
        g = geom_with(parts=[part], pads=pads)
        e = estimate_layers(g)
        self.assertEqual(len(e.grids), 1)
        self.assertEqual(e.grids[0].rings, 5)
        self.assertEqual(e.grids[0].layers, 5)
        self.assertGreaterEqual(e.needed_layers, 6)
        self.assertIn('escape', e.describe())

    def test_perimeter_packages_are_not_grids(self):
        pads = []
        for k in range(10):           # a QFP-like ring of 40 pads
            pads += [pad_at(0, f'A{k}', 20 + 0.5 * k, 20, w=0.3, h=1.0),
                     pad_at(0, f'A{k}', 20 + 0.5 * k, 26, w=0.3, h=1.0),
                     pad_at(0, f'C{k}', 19, 20.5 + 0.5 * k, w=1.0, h=0.3),
                     pad_at(0, f'C{k}', 25.5, 20.5 + 0.5 * k, w=1.0, h=0.3)]
        part = PartGeom(ref='U1', uuid='U1', type_name='QFP40', angle=0.0,
                        box=(int(18 * MM), int(19 * MM), int(27 * MM), int(27 * MM)))
        self.assertEqual(estimate_layers(geom_with(parts=[part], pads=pads)).grids, [])


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

class TestLearning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='place-news-learn-')
        self.path = os.path.join(self.dir, 'store.json')

    def test_every_criterion_has_a_weight(self):
        lr = Learner(self.path, random.Random(1))
        self.assertEqual(set(lr.weights), set(ac.CRITERIA))
        self.assertTrue(set(DEFAULT_WEIGHTS) <= set(ac.CRITERIA))

    def test_feedback_and_persistence(self):
        lr = Learner(self.path, random.Random(1))
        q = {k: 1.0 for k in ac.CRITERIA}
        q['vias'] = 0.3
        before = lr.weights['vias']
        notes = lr.feedback('b.kicad_pcb', q, liked=False, bothered=['vias'])
        self.assertGreater(lr.weights['vias'], before)
        self.assertTrue(notes)
        again = Learner(self.path, random.Random(1))
        self.assertAlmostEqual(again.weights['vias'], lr.weights['vias'])
        self.assertEqual(again.feedback_count, 1)

    def test_policy_sampling_stays_in_range(self):
        lr = Learner(self.path, random.Random(2))
        for _ in range(50):
            values, eps = lr.sample(('align_budget', 'untangle_budget', 'via_cost'))
            for k, v in values.items():
                lo, hi = POLICY_RANGE[k]
                self.assertTrue(math.exp(lo) - 1e-9 <= v <= math.exp(hi) + 1e-9)

    def test_learning_from_edits(self):
        lr = Learner(self.path, random.Random(3))
        q0 = {k: 0.5 for k in ac.CRITERIA}
        state0 = {'parts': {'u1': [0, 0, 0.0, 0, 0, 0.0]}, 'tracks': [0, 0]}
        lr.record_run('b.kicad_pcb', 'placement', q0, state0, {'align_budget': 0.3})
        q1 = dict(q0, alignment=0.9)              # the user straightened the rows
        state1 = {'parts': {'u1': [5_000_000, 0, 0.0, 0, 0, 0.0]}, 'tracks': [0, 0]}
        self.assertTrue(changed_state(state0, state1))
        w0, p0 = lr.weights['alignment'], lr.policy['align_budget']
        notes = lr.learn_from_edits('b.kicad_pcb', q1, state1)
        self.assertGreater(lr.weights['alignment'], w0)
        self.assertNotEqual(lr.policy['align_budget'], p0)    # the policy that produced it moved
        self.assertTrue(notes)
        self.assertEqual(lr.learn_from_edits('b.kicad_pcb', q1, state1), [])   # once per change


# ---------------------------------------------------------------------------
# Report, pour geometry, layer trial, Freerouting log
# ---------------------------------------------------------------------------

class TestReportAndTools(unittest.TestCase):
    def test_report(self):
        g = TestLayerEstimate().board()
        r = check(g)
        page = build_html('demo — aesthetic check', g, r, r.issues[:5], ['learned line'])
        self.assertIn('<svg', page)
        self.assertIn('>cost<', page)
        self.assertIn('Routing room', page)
        self.assertIn('learned line', page)
        self.assertIn('<svg', render_svg(g, r.issues[:3]))

    def test_convex_hull(self):
        pts = [(0, 0), (10, 0), (10, 10), (0, 10), (5, 5), (5, 0)]
        self.assertEqual(sorted(convex_hull(pts)), [(0, 0), (0, 10), (10, 0), (10, 10)])

    def test_ground_names(self):
        for n in ('GND', '/PGND', 'AGND', 'GNDA', 'GND_ISO', '0V', 'VSS'):
            self.assertTrue(is_ground_name(n), n)
        for n in ('VOUT', 'SIGNAL', 'GATE', 'HV_BUS-', 'PE', 'COM', 'EARTH', 'VSSA_EN'):
            self.assertFalse(is_ground_name(n), n)

    def test_outer_layers_only(self):
        dsn = ('(pcb b (structure (layer F.Cu (type signal) (property (index 0)))'
               ' (layer In1.Cu (type signal) (property (index 1)))'
               ' (layer In2.Cu (type signal) (property (index 2)))'
               ' (layer B.Cu (type signal) (property (index 3)))))')
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, 'a.dsn'), os.path.join(d, 'b.dsn')
        with open(src, 'w') as f:
            f.write(dsn)
        self.assertEqual(specctra.outer_layers_only(src, dst), ['In1.Cu', 'In2.Cu'])
        with open(dst) as f:
            tree = specctra.parse_dsn(f.read())
        layers = specctra._find_all(specctra._find(tree, 'structure'), 'layer')
        kinds = [specctra._find(l, 'type')[1] for l in layers]
        self.assertEqual(kinds, ['signal', 'power', 'power', 'signal'])

    def test_score_line_singular(self):
        m = specctra._SCORE_RE.search('final score: 998.41 (0 unrouted and 1 violation)')
        self.assertEqual(m.groups(), ('0', '1'))


if __name__ == '__main__':
    unittest.main()
