"""Tests for the row / column tidy-up — no KiCad dependency."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from plugin.board_model import BoardModel, Footprint, Pad, Net  # noqa: E402
from plugin.cost_function import CostState  # noqa: E402
from plugin.aesthetics import align_rows, body_center, type_key  # noqa: E402

MM = 1_000_000


def part(ref, idx, x, y, lib='R_0805', w=3 * MM, h=int(1.5 * MM), angle=0.0, nets=(), locked=False):
    pads = [Pad(net_code=nc, net_name=f'N{nc}', offset_x=(-1 if k == 0 else 1) * MM, offset_y=0)
            for k, nc in enumerate(nets)]
    return Footprint(reference=ref, index=idx, x=int(x), y=int(y), angle_deg=angle, width=w,
                     height=h, locked=locked, pads=pads, net_codes={nc for nc in nets if nc},
                     uuid=f'u{idx}', lib_id=f'Lib:{lib}')


def model_of(fps, nets=None):
    nets = nets or {}
    for fi, fp in enumerate(fps):
        for pi, p in enumerate(fp.pads):
            if p.net_code:
                nets.setdefault(p.net_code, Net(p.net_code, p.net_name)).pad_refs.append((fi, pi))
    return BoardModel(footprints=fps, nets=nets, outline_xmin=0, outline_ymin=0,
                      outline_xmax=60 * MM, outline_ymax=40 * MM,
                      moveable_indices=[f.index for f in fps if not f.locked])


class TestRows(unittest.TestCase):
    def test_row_aligned_on_centres_and_evenly_spaced(self):
        ys = (20.0, 20.4, 19.7, 20.2)
        xs = (10, 15, 21, 25)
        fps = [part(f'R{k + 1}', k, xs[k] * MM, ys[k] * MM, nets=(k + 1, k + 2))
               for k in range(4)]
        model = model_of(fps)
        cs = CostState(model, quiet=True)
        rep = align_rows(model, cs)
        centres = [body_center(f) for f in model.footprints]
        self.assertEqual(len({c[1] for c in centres}), 1, centres)       # one line
        gaps = [centres[k + 1][0] - centres[k][0] for k in range(3)]
        self.assertLessEqual(max(gaps) - min(gaps), 2, gaps)              # even spacing
        self.assertGreaterEqual(rep.lines, 1)
        self.assertEqual(cs._overlap_penalty, 0)

    def test_columns_too(self):
        fps = [part(f'C{k + 1}', k, (30 + 0.3 * (k % 2)) * MM, (8 + 5 * k) * MM, lib='C_0805',
                    w=int(1.5 * MM), h=3 * MM) for k in range(3)]
        model = model_of(fps)
        align_rows(model, CostState(model, quiet=True))
        self.assertEqual(len({body_center(f)[0] for f in model.footprints}), 1)

    def test_different_types_are_left_alone(self):
        fps = [part('R1', 0, 10 * MM, 20 * MM), part('C1', 1, 15 * MM, 20.4 * MM, lib='C_0805')]
        model = model_of(fps)
        rep = align_rows(model, CostState(model, quiet=True))
        self.assertEqual(rep.parts_moved, 0)
        self.assertNotEqual(type_key(fps[0]), type_key(fps[1]))

    def test_never_creates_an_overlap(self):
        a = part('R1', 0, 10 * MM, 20.6 * MM)
        b = part('R2', 1, 16 * MM, 20.0 * MM)
        # A different part right above R2: lifting R2 to R1's line would hit it.
        c = part('U1', 2, 16 * MM, 21.6 * MM, lib='SOT23', w=4 * MM, h=1 * MM)
        model = model_of([a, b, c])
        cs = CostState(model, quiet=True)
        rep = align_rows(model, cs)
        self.assertEqual(rep.parts_moved, 0)
        self.assertEqual(cs._overlap_penalty, 0)

    def test_locked_part_is_the_anchor(self):
        fixed = part('R1', 0, 10 * MM, 20.0 * MM, locked=True)
        free = part('R2', 1, 16 * MM, 20.5 * MM)
        model = model_of([fixed, free])
        align_rows(model, CostState(model, quiet=True))
        self.assertEqual(body_center(model.footprints[1])[1], 20 * MM)
        self.assertEqual(body_center(model.footprints[0])[1], 20 * MM)

    def test_wirelength_budget(self):
        # Aligning would stretch a long net a lot: refused with a tiny budget.
        fps = [part('R1', 0, 10 * MM, 20 * MM, nets=(1,)), part('R2', 1, 16 * MM, 20.8 * MM, nets=(2,)),
               part('J1', 2, 16 * MM, 39 * MM, lib='CONN', nets=(2,))]
        model = model_of(fps)
        cs = CostState(model, quiet=True)
        rep = align_rows(model, cs, budget_ratio=0.0)
        # The 2 mm minimum budget still allows this 0.8 mm move...
        self.assertEqual(rep.parts_moved, 1)


if __name__ == '__main__':
    unittest.main()
