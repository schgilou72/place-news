"""place-news KiCad Action Plugin — entry point.

place-news is derived from CadMust-Neo by Remi Blokker (MIT licence).
"""
import os
import pcbnew
import wx
import time

from .board_model import extract_board_model
from .cost_function import CostState
from .annealer import run_sa, SAResult
from .placement import save_original_positions, restore_original_positions, apply_model_to_board


def _write_debug_log(model, verify_model, model_cs, verify_cs):
    """Write diagnostic log when model HPWL doesn't match re-extracted HPWL."""
    try:
        lines = [
            f"Model HPWL:    {model_cs.hpwl / 1e6:.3f} mm",
            f"Verified HPWL: {verify_cs.hpwl / 1e6:.3f} mm",
            f"Diff:          {(model_cs.hpwl - verify_cs.hpwl) / 1e6:.3f} mm",
            f"Model overlap: {model_cs._overlap_penalty / 1e6:.3f} mm",
            f"Verify overlap:{verify_cs._overlap_penalty / 1e6:.3f} mm",
            "",
            "Overlapping pairs:",
        ]
        fps = model.footprints
        for i in range(len(fps)):
            gi = model.fp_to_group.get(i)
            for j in range(i + 1, len(fps)):
                if gi is not None and gi == model.fp_to_group.get(j):
                    continue  # same group — not penalized
                x1min, y1min, x1max, y1max = fps[i].bbox
                x2min, y2min, x2max, y2max = fps[j].bbox
                ox = max(0, min(x1max, x2max) - max(x1min, x2min))
                oy = max(0, min(y1max, y2max) - max(y1min, y2min))
                if ox > 0 and oy > 0:
                    penalty = (ox + oy) * 50.0 / 1e6
                    lines.append(
                        f"  {fps[i].reference} vs {fps[j].reference}: "
                        f"ox={ox/1e6:.3f}mm oy={oy/1e6:.3f}mm penalty={penalty:.3f}mm"
                    )
        lines += [
            "",
            "Position comparison (model vs re-extracted):",
        ]
        for mfp, vfp in zip(model.footprints, verify_model.footprints):
            dx = mfp.x - vfp.x
            dy = mfp.y - vfp.y
            da = mfp.angle_deg - vfp.angle_deg
            if abs(dx) > 0 or abs(dy) > 0 or abs(da) > 0.001:
                lines.append(
                    f"  {mfp.reference}: model=({mfp.x},{mfp.y},{mfp.angle_deg:.1f})"
                    f" kicad=({vfp.x},{vfp.y},{vfp.angle_deg:.1f})"
                    f" delta=({dx},{dy},{da:.1f})"
                )

        lines.append("")
        lines.append("Per-net HPWL comparison:")
        for nc in sorted(model_cs.net_hpwl.keys()):
            mh = model_cs.net_hpwl.get(nc, 0)
            vh = verify_cs.net_hpwl.get(nc, 0)
            if mh != vh:
                net_name = ""
                if nc in model.nets:
                    net_name = model.nets[nc].net_name
                lines.append(f"  net {nc} ({net_name}): model={mh/1e6:.3f} verify={vh/1e6:.3f} diff={(mh-vh)/1e6:.3f}")

        lines.append("")
        lines.append("Pad offset comparison (first 10 mismatches):")
        pad_mismatches = 0
        for mfp, vfp in zip(model.footprints, verify_model.footprints):
            for mp, vp in zip(mfp.pads, vfp.pads):
                if mp.offset_x != vp.offset_x or mp.offset_y != vp.offset_y:
                    if pad_mismatches < 10:
                        mwx, mwy = mp.abs_position(mfp.x, mfp.y, mfp.angle_deg)
                        vwx, vwy = vp.abs_position(vfp.x, vfp.y, vfp.angle_deg)
                        lines.append(
                            f"  {mfp.reference} angle={mfp.angle_deg:.1f}: "
                            f"model net={mp.net_code} off=({mp.offset_x},{mp.offset_y}) world=({mwx},{mwy}) | "
                            f"verify net={vp.net_code} off=({vp.offset_x},{vp.offset_y}) world=({vwx},{vwy})"
                        )
                    pad_mismatches += 1
        lines.append(f"Total pad offset mismatches: {pad_mismatches}")

        # Show a specific net with large HPWL diff — all pad world positions
        worst_net = None
        worst_diff = 0
        for nc in model_cs.net_hpwl:
            d = abs(model_cs.net_hpwl.get(nc, 0) - verify_cs.net_hpwl.get(nc, 0))
            if d > worst_diff:
                worst_diff = d
                worst_net = nc
        if worst_net is not None:
            net_name = model.nets[worst_net].net_name if worst_net in model.nets else "?"
            lines.append(f"\nWorst net detail: net {worst_net} ({net_name})")
            lines.append("  Model pads:")
            for fi, pi in model.nets[worst_net].pad_refs:
                fp = model.footprints[fi]
                pad = fp.pads[pi]
                wx, wy = pad.abs_position(fp.x, fp.y, fp.angle_deg)
                lines.append(f"    {fp.reference}[{pi}] net={pad.net_code} off=({pad.offset_x},{pad.offset_y}) angle={fp.angle_deg:.1f} world=({wx},{wy})")
            lines.append("  Verify pads:")
            for fi, pi in verify_model.nets[worst_net].pad_refs:
                fp = verify_model.footprints[fi]
                pad = fp.pads[pi]
                wx, wy = pad.abs_position(fp.x, fp.y, fp.angle_deg)
                lines.append(f"    {fp.reference}[{pi}] net={pad.net_code} off=({pad.offset_x},{pad.offset_y}) angle={fp.angle_deg:.1f} world=({wx},{wy})")

        log_path = os.path.join(os.path.dirname(__file__), 'debug.log')
        with open(log_path, 'w') as f:
            f.write('\n'.join(lines))
    except Exception as e:
        # Write the exception itself so we know what went wrong
        try:
            log_path = os.path.join(os.path.dirname(__file__), 'debug_error.log')
            with open(log_path, 'w') as f:
                f.write(str(e))
        except Exception:
            pass


class _TempGradientBar(wx.Panel):
    """A horizontal gradient bar from red (hot) to blue (cold) with a needle."""

    BAR_HEIGHT = 28
    NEEDLE_WIDTH = 3

    def __init__(self, parent):
        super().__init__(parent, size=(-1, self.BAR_HEIGHT + 16))
        self._position = 1.0  # 0.0 = cold (right), 1.0 = hot (left)
        self.SetMinSize(wx.Size(300, self.BAR_HEIGHT + 16))
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def set_position(self, temperature, t0):
        """Set needle position from temperature and T0."""
        if t0 > 0 and temperature > 0:
            # Use log scale for smoother visual movement
            import math
            self._position = max(0.0, min(1.0,
                math.log(temperature + 1) / math.log(t0 + 1)))
        else:
            self._position = 0.0
        self.Refresh()

    def _on_paint(self, evt):
        dc = wx.PaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return

        w, h = self.GetClientSize()
        bar_y = 4
        bar_h = self.BAR_HEIGHT
        bar_w = w - 8
        bar_x = 4

        # Draw gradient: red (left/hot) → yellow → cyan → blue (right/cold)
        if bar_w > 0:
            for i in range(bar_w):
                frac = i / max(bar_w - 1, 1)  # 0.0 = left (hot), 1.0 = right (cold)
                if frac < 0.33:
                    # Red → Yellow
                    t = frac / 0.33
                    r, g, b = 220, int(40 + 180 * t), int(20 * (1 - t))
                elif frac < 0.66:
                    # Yellow → Cyan
                    t = (frac - 0.33) / 0.33
                    r, g, b = int(220 * (1 - t)), int(220 - 40 * t), int(180 * t)
                else:
                    # Cyan → Blue
                    t = (frac - 0.66) / 0.34
                    r, g, b = int(20 * (1 - t)), int(180 * (1 - t)), int(180 + 60 * t)
                gc.SetPen(wx.Pen(wx.Colour(r, g, b), 1))
                gc.StrokeLine(bar_x + i, bar_y, bar_x + i, bar_y + bar_h)

        # Draw border around the bar
        gc.SetPen(wx.Pen(wx.Colour(100, 100, 100), 1))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.DrawRectangle(bar_x, bar_y, bar_w, bar_h)

        # Draw needle — position 1.0 = left (hot), 0.0 = right (cold)
        needle_x = bar_x + int((1.0 - self._position) * (bar_w - 1))
        gc.SetPen(wx.Pen(wx.Colour(0, 0, 0), self.NEEDLE_WIDTH))
        gc.StrokeLine(needle_x, bar_y - 2, needle_x, bar_y + bar_h + 2)
        # Small triangle on top
        path = gc.CreatePath()
        path.MoveToPoint(needle_x - 4, bar_y - 2)
        path.AddLineToPoint(needle_x + 4, bar_y - 2)
        path.AddLineToPoint(needle_x, bar_y + 3)
        path.CloseSubpath()
        gc.SetBrush(wx.Brush(wx.Colour(0, 0, 0)))
        gc.SetPen(wx.NullPen)
        gc.FillPath(path)

        # Labels
        dc.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        dc.SetTextForeground(wx.Colour(120, 120, 120))
        dc.DrawText("HOT", bar_x, bar_y + bar_h + 1)
        cold_w, _ = dc.GetTextExtent("COLD")
        dc.DrawText("COLD", bar_x + bar_w - cold_w, bar_y + bar_h + 1)


class _CostBar(wx.Panel):
    """Bar showing start/current/best HPWL with needles and green improvement zone."""

    BAR_HEIGHT = 28
    NEEDLE_WIDTH = 3

    def __init__(self, parent, initial_hpwl):
        super().__init__(parent, size=(-1, self.BAR_HEIGHT + 18))
        self._initial = max(initial_hpwl, 1.0)
        self._current_frac = 1.0
        self._best_frac = 1.0
        # Scale: left = 0, right = 2× initial (start in the middle)
        self._scale_max = 2.0
        self.SetMinSize(wx.Size(300, self.BAR_HEIGHT + 18))
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def set_values(self, current_hpwl, best_hpwl):
        """Update HPWL positions as fractions of initial."""
        self._current_frac = current_hpwl / self._initial
        self._best_frac = best_hpwl / self._initial
        # Expand right edge to keep all needles visible (with 5% margin)
        max_frac = max(self._current_frac, self._best_frac, 1.0)
        needed = max_frac * 1.05
        if needed > self._scale_max:
            self._scale_max = needed
        self.Refresh()

    def _draw_needle(self, gc, x, bar_y, bar_h, colour, from_top=True):
        """Draw a needle with triangle pointer, like the temperature bar."""
        gc.SetPen(wx.Pen(colour, self.NEEDLE_WIDTH))
        gc.StrokeLine(x, bar_y - 2, x, bar_y + bar_h + 2)
        path = gc.CreatePath()
        if from_top:
            path.MoveToPoint(x - 4, bar_y - 2)
            path.AddLineToPoint(x + 4, bar_y - 2)
            path.AddLineToPoint(x, bar_y + 3)
        else:
            path.MoveToPoint(x - 4, bar_y + bar_h + 2)
            path.AddLineToPoint(x + 4, bar_y + bar_h + 2)
            path.AddLineToPoint(x, bar_y + bar_h - 3)
        path.CloseSubpath()
        gc.SetBrush(wx.Brush(colour))
        gc.SetPen(wx.NullPen)
        gc.FillPath(path)

    def _on_paint(self, evt):
        dc = wx.PaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return

        w, h = self.GetClientSize()
        bar_y = 4
        bar_h = self.BAR_HEIGHT
        bar_w = w - 8
        bar_x = 4
        # Scale: left = 0 wirelength, right adapts to show all needles
        scale_min = 0.0
        scale_range = self._scale_max

        def _x(frac):
            pos = (frac - scale_min) / scale_range
            return bar_x + max(0, min(int(pos * bar_w), bar_w - 1))

        # Background (light grey)
        gc.SetBrush(wx.Brush(wx.Colour(230, 230, 230)))
        gc.SetPen(wx.NullPen)
        gc.DrawRectangle(bar_x, bar_y, bar_w, bar_h)

        # Green improvement zone between best and start
        best_x = _x(self._best_frac)
        start_x = _x(1.0)
        current_x = _x(self._current_frac)
        if best_x < start_x:
            gc.SetBrush(wx.Brush(wx.Colour(180, 230, 180)))
            gc.SetPen(wx.NullPen)
            gc.DrawRectangle(best_x, bar_y, start_x - best_x, bar_h)

        # Border
        gc.SetPen(wx.Pen(wx.Colour(100, 100, 100), 1))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.DrawRectangle(bar_x, bar_y, bar_w, bar_h)

        # Needles: start (grey, from top), best (green, from top), current (orange, from bottom)
        self._draw_needle(gc, start_x, bar_y, bar_h,
                          wx.Colour(140, 140, 140), from_top=True)
        self._draw_needle(gc, current_x, bar_y, bar_h,
                          wx.Colour(255, 152, 0), from_top=False)
        self._draw_needle(gc, best_x, bar_y, bar_h,
                          wx.Colour(46, 140, 50), from_top=True)

        # Static legend below bar: colored lines with labels
        dc.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                           wx.FONTWEIGHT_NORMAL))
        ly = bar_y + bar_h + 3
        lx = bar_x
        for colour, label in [
            (wx.Colour(140, 140, 140), "start"),
            (wx.Colour(46, 140, 50), "best"),
            (wx.Colour(255, 152, 0), "current"),
        ]:
            dc.SetPen(wx.Pen(colour, 2))
            dc.DrawLine(lx, ly + 5, lx + 14, ly + 5)
            dc.SetTextForeground(colour)
            dc.DrawText(label, lx + 17, ly)
            tw, _ = dc.GetTextExtent(label)
            lx += 17 + tw + 12


class _ResultsDialog(wx.Dialog):
    """Results dialog with metrics, verdict, and Accept/Reject buttons."""

    # Unicode indicators
    _CHECK = "\u2714"  # ✔
    _CROSS = "\u2718"  # ✘
    _DASH = "\u2014"   # —

    def __init__(self, parent, *, hpwl_before_mm, hpwl_after_mm, hpwl_change_pct,
                 n_overlaps, n_keepout, n_silk_moved, elapsed, total_moves,
                 accepted_moves, iso_checked=False, iso_lines=(), n_iso_internal=0,
                 internal_refs=(), n_iso_fixed=0, n_areas=0, n_area_violations=0,
                 alignment=None, untangle=None):
        super().__init__(parent, title="place-news \u2014 Results",
                         style=wx.DEFAULT_DIALOG_STYLE)

        sizer = wx.BoxSizer(wx.VERTICAL)

        # --- Verdict ---
        hpwl_good = hpwl_change_pct >= 0
        overlaps_good = n_overlaps == 0
        keepout_good = n_keepout == 0 and len(iso_lines) == 0 and n_area_violations == 0

        if hpwl_good and overlaps_good and keepout_good:
            verdict_text = "Placement improved"
            verdict_colour = wx.Colour(34, 139, 34)   # forest green
        elif not hpwl_good and overlaps_good and keepout_good:
            verdict_text = "Placement worsened"
            verdict_colour = wx.Colour(200, 40, 40)    # red
        else:
            verdict_text = "Mixed results"
            verdict_colour = wx.Colour(200, 140, 0)    # amber

        verdict_label = wx.StaticText(self, label=verdict_text)
        font = verdict_label.GetFont()
        font.SetPointSize(font.GetPointSize() + 4)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        verdict_label.SetFont(font)
        verdict_label.SetForegroundColour(verdict_colour)
        sizer.Add(verdict_label, 0, wx.ALL | wx.ALIGN_CENTER, 12)

        # --- Metrics ---
        metrics_sizer = wx.FlexGridSizer(cols=3, vgap=6, hgap=10)

        def add_metric(indicator, colour, label, value):
            ind = wx.StaticText(self, label=indicator)
            ind_font = ind.GetFont()
            ind_font.SetPointSize(ind_font.GetPointSize() + 2)
            ind.SetFont(ind_font)
            ind.SetForegroundColour(colour)
            metrics_sizer.Add(ind, 0, wx.ALIGN_CENTER_VERTICAL)

            lbl = wx.StaticText(self, label=label)
            metrics_sizer.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)

            val = wx.StaticText(self, label=value)
            val_font = val.GetFont()
            val_font.SetWeight(wx.FONTWEIGHT_BOLD)
            val.SetFont(val_font)
            metrics_sizer.Add(val, 0, wx.ALIGN_CENTER_VERTICAL)

        green = wx.Colour(34, 139, 34)
        red = wx.Colour(200, 40, 40)
        grey = wx.Colour(120, 120, 120)

        # Wirelength
        if hpwl_change_pct >= 0:
            add_metric(self._CHECK, green, "Wirelength",
                       f"{hpwl_before_mm:.1f} \u2192 {hpwl_after_mm:.1f} mm  ({hpwl_change_pct:.1f}% shorter)")
        else:
            add_metric(self._CROSS, red, "Wirelength",
                       f"{hpwl_before_mm:.1f} \u2192 {hpwl_after_mm:.1f} mm  ({-hpwl_change_pct:.1f}% longer)")

        # Overlaps
        if n_overlaps == 0:
            add_metric(self._CHECK, green, "Overlaps", "none")
        else:
            add_metric(self._CROSS, red, "Overlaps", f"{n_overlaps} pairs")

        amber = wx.Colour(200, 140, 0)

        # Isolation (clearance / creepage) between parts
        if iso_checked:
            if not iso_lines:
                add_metric(self._CHECK, green, "Isolation", "all pad pairs meet the rules")
            else:
                worst = iso_lines[0]
                more = f"  (+{len(iso_lines) - 1} more)" if len(iso_lines) > 1 else ""
                add_metric(self._CROSS, red, "Isolation", f"{worst}{more}")
            if n_iso_internal:
                refs = ", ".join(list(internal_refs)[:6]) + ("…" if len(internal_refs) > 6 else "")
                add_metric(self._DASH, amber, "Inside footprints",
                           f"{n_iso_internal} pad pairs too close ({refs}) — footprint choice")
            if n_iso_fixed:
                add_metric(self._DASH, amber, "Locked parts",
                           f"{n_iso_fixed} pad pairs too close between locked parts")

        # Placement areas
        if n_areas:
            if n_area_violations == 0:
                add_metric(self._CHECK, green, "Placement areas", "respected")
            else:
                add_metric(self._CROSS, red, "Placement areas",
                           f"{n_area_violations} part(s) outside or intruding")

        # Keep-out violations
        if n_keepout == 0:
            add_metric(self._CHECK, green, "Keep-out violations", "none")
        else:
            add_metric(self._CROSS, red, "Keep-out violations", f"{n_keepout}")

        # Rows / columns tidy-up (informational)
        if alignment is not None:
            if alignment.parts_moved:
                extra = (alignment.hpwl_after - alignment.hpwl_before) / 1e6
                align_msg = (f"{alignment.parts_moved} parts on {alignment.lines} line(s)"
                             f"  ({extra:+.1f} mm wire)")
            else:
                align_msg = "nothing to align"
            add_metric(self._DASH, grey, "Rows / columns", align_msg)
            if getattr(alignment, 'rotated', 0):
                add_metric(self._DASH, grey, "Orientation",
                           f"{alignment.rotated} part(s) turned like their siblings")

        # Untangled ratsnest (cost: fewer vias)
        if untangle is not None:
            if untangle.swaps or untangle.flips:
                moves = []
                if untangle.swaps:
                    moves.append(f"{untangle.swaps} swap(s)")
                if untangle.flips:
                    moves.append(f"{untangle.flips} part(s) turned round")
                extra = (untangle.hpwl_after - untangle.hpwl_before) / 1e6
                add_metric(self._CHECK, green, "Ratsnest crossings",
                           f"{untangle.crossings_before} \u2192 {untangle.crossings_after}  "
                           f"({', '.join(moves)}, {extra:+.1f} mm wire)")
            else:
                add_metric(self._DASH, grey, "Ratsnest crossings",
                           f"{untangle.crossings_before} (no swap found that removes one)"
                           if untangle.crossings_before else "none")

        # Silkscreen (neutral — always informational)
        silk_msg = (f"{n_silk_moved} refs centred / repositioned" if n_silk_moved > 0
                    else "no changes needed")
        add_metric(self._DASH, grey, "Silkscreen", silk_msg)

        sizer.Add(metrics_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        # --- Stats (smaller, grey) ---
        stats_text = (
            f"Moves: {total_moves:,} total, {accepted_moves:,} accepted  |  "
            f"Time: {elapsed:.1f}s"
        )
        stats_label = wx.StaticText(self, label=stats_text)
        stats_label.SetForegroundColour(wx.Colour(140, 140, 140))
        stats_font = stats_label.GetFont()
        stats_font.SetPointSize(stats_font.GetPointSize() - 1)
        stats_label.SetFont(stats_font)
        sizer.Add(stats_label, 0, wx.LEFT | wx.RIGHT, 16)

        sizer.AddSpacer(16)

        # --- Separator ---
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        sizer.AddSpacer(8)

        # --- Accept / Reject buttons ---
        btn_sizer = wx.StdDialogButtonSizer()
        accept_btn = wx.Button(self, wx.ID_OK, "Accept")
        accept_btn.SetDefault()
        reject_btn = wx.Button(self, wx.ID_CANCEL, "Reject")
        btn_sizer.AddButton(accept_btn)
        btn_sizer.AddButton(reject_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_CENTER, 8)

        self.SetSizer(sizer)
        self.Fit()
        self.CentreOnScreen()


class _ProgressDialog(wx.Dialog):
    """Custom progress dialog with temperature gradient bar and wirelength bar."""

    def __init__(self, parent, initial_hpwl):
        super().__init__(parent, title="place-news \u2014 Optimizer",
                         style=wx.DEFAULT_DIALOG_STYLE)
        self.cancelled = False
        self._initial_hpwl = initial_hpwl
        self._start_time = time.time()

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Phase label
        self._phase_label = wx.StaticText(self, label="Calibrating...")
        font = self._phase_label.GetFont()
        font.SetPointSize(font.GetPointSize() + 2)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        self._phase_label.SetFont(font)
        sizer.Add(self._phase_label, 0, wx.ALL, 10)

        # Temperature gradient bar
        temp_label = wx.StaticText(self, label="Temperature:")
        sizer.Add(temp_label, 0, wx.LEFT | wx.TOP, 10)
        self._temp_bar = _TempGradientBar(self)
        sizer.Add(self._temp_bar, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        sizer.AddSpacer(8)

        # Wirelength bar (HPWL — not affected by penalty scaling)
        wl_label = wx.StaticText(self, label="Wirelength:")
        sizer.Add(wl_label, 0, wx.LEFT | wx.TOP, 10)
        self._cost_bar = _CostBar(self, initial_hpwl)
        sizer.Add(self._cost_bar, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        sizer.AddSpacer(8)

        # Overall progress bar (wx.Gauge)
        prog_label = wx.StaticText(self, label="Overall progress:")
        sizer.Add(prog_label, 0, wx.LEFT | wx.TOP, 10)
        self._gauge = wx.Gauge(self, range=100, size=(-1, 18))
        sizer.Add(self._gauge, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        sizer.AddSpacer(12)

        # HPWL improvement display
        self._hpwl_label = wx.StaticText(
            self, label="HPWL improvement:  0.0%")
        font2 = self._hpwl_label.GetFont()
        font2.SetPointSize(font2.GetPointSize() + 1)
        self._hpwl_label.SetFont(font2)
        sizer.Add(self._hpwl_label, 0, wx.LEFT | wx.RIGHT, 10)

        self._hpwl_detail = wx.StaticText(
            self, label=f"{initial_hpwl / 1e6:.1f} \u2192 ? mm")
        self._hpwl_detail.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(self._hpwl_detail, 0, wx.LEFT | wx.RIGHT, 10)

        sizer.AddSpacer(12)

        # Cancel button
        btn = wx.Button(self, wx.ID_CANCEL, "Cancel")
        btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        sizer.Add(btn, 0, wx.ALL | wx.ALIGN_CENTER, 8)

        self.SetSizer(sizer)
        self.SetMinSize(wx.Size(380, -1))
        self.Fit()
        self.CentreOnScreen()

        # Escape key and window close → set cancelled flag (don't destroy mid-run)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

        # Timer drives a manual bounce animation at 50 Hz during greedy refinement.
        # Gauge.Pulse() is NOT used — on macOS the native NSProgressIndicator
        # animates at the OS rate (~2 Hz) regardless of how often Pulse() is called.
        # Instead we manually step a determinate gauge value back and forth.
        self._pulse_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_pulse_tick, self._pulse_timer)
        self._pulse_val = 0
        self._pulse_dir = 1  # +1 = forward, -1 = backward

    def _on_cancel(self, evt):
        self.cancelled = True

    def _on_close(self, evt):
        """Window close button → cancel, don't destroy yet."""
        self.cancelled = True

    def _on_key(self, evt):
        """Escape key → cancel."""
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.cancelled = True
        else:
            evt.Skip()

    def _on_pulse_tick(self, evt):
        # Manual bounce: step gauge value 0→100→0 at ~3 units/tick (50 Hz → ~1.7s cycle)
        self._pulse_val += self._pulse_dir * 3
        if self._pulse_val >= 100:
            self._pulse_val = 100
            self._pulse_dir = -1
        elif self._pulse_val <= 0:
            self._pulse_val = 0
            self._pulse_dir = 1
        self._gauge.SetValue(self._pulse_val)

    def update_state(self, phase, temperature, t0, improvement, hpwl_mm, pct,
                     current_hpwl, best_hpwl):
        """Update all display elements."""
        self._phase_label.SetLabel(phase)
        self._temp_bar.set_position(temperature, t0)
        self._cost_bar.set_values(current_hpwl, best_hpwl)
        if phase == "Refining":
            if not self._pulse_timer.IsRunning():
                self._pulse_timer.Start(20)  # 50 Hz
        else:
            if self._pulse_timer.IsRunning():
                self._pulse_timer.Stop()
            self._gauge.SetValue(min(pct, 99))
        self._hpwl_label.SetLabel(f"HPWL improvement:  {improvement:.1f}%")
        self._hpwl_detail.SetLabel(
            f"{self._initial_hpwl / 1e6:.1f} \u2192 {hpwl_mm:.1f} mm")


def _build_board_info(model, cs):
    """Build the board_info dict passed to SettingsDialog.

    net_list contains:
      - All auto-detected power nets (is_excluded=True), regardless of size
      - Signal nets with >= _SIGNAL_THRESHOLD pads (high-fanout bus/clock nets)
    Sorted by pad count descending so the most dominant nets appear first.
    """
    _SIGNAL_THRESHOLD = 8

    net_list = []
    for net in model.nets.values():
        fanout = len(net.pad_refs)
        if net.is_excluded:
            net_list.append((net.net_name, fanout, True))
        elif fanout >= _SIGNAL_THRESHOLD:
            net_list.append((net.net_name, fanout, False))
    net_list.sort(key=lambda x: -x[1])  # descending fanout

    signal_nets = sum(
        1 for net in model.nets.values()
        if not net.is_excluded and len(net.pad_refs) >= 2
    )
    n_moveable = len(model.moveable_indices)
    n_locked = sum(1 for fp in model.footprints if fp.locked)

    iso = model.isolation
    if iso is None:
        iso_summary = "Isolation rules not read."
    else:
        counts = iso.rules.summary() if iso.rules else {}
        n_rules = len(iso.rules.rules) if iso.rules else 0
        rules_txt = (f"{n_rules} custom rule(s): " + ", ".join(
            f"{v} {k}" for k, v in counts.items() if v)) if n_rules else "no custom rule"
        if iso.active:
            iso_summary = (f"Up to {iso.max_req / 1e6:.2f} mm between pads of different nets "
                           f"· {rules_txt}")
        else:
            iso_summary = f"Nothing above {iso.threshold / 1e6:.2f} mm · {rules_txt}"

    return {
        'moveable': n_moveable,
        'locked': n_locked,
        'signal_nets': signal_nets,
        'hpwl_mm': cs.hpwl / 1e6,
        'net_list': net_list,
        'iso_summary': iso_summary,
        'n_areas': len(model.placement_areas),
        'n_keepouts': len(model.keepouts),
        'warnings': list(model.warnings),
    }


def _pad_label(model, fi, pi):
    fp = model.footprints[fi]
    num = fp.pads[pi].number if pi < len(fp.pads) else ''
    return f"{fp.reference}.{num}" if num else fp.reference


def _isolation_report(model):
    """Split isolation violations into placement ones (between footprints,
    at least one moveable), footprint-internal ones and locked-locked ones."""
    from .isolation import list_violations
    iso = model.isolation
    if iso is None or not iso.active:
        return [], [], []
    placement, internal, fixed = [], [], []
    for v in list_violations(iso, model.footprints):
        ga = model.fp_to_group.get(v.fp_a)
        if v.fp_a == v.fp_b:
            internal.append(v)
        elif (model.footprints[v.fp_a].locked and model.footprints[v.fp_b].locked) or \
                (ga is not None and ga == model.fp_to_group.get(v.fp_b)):
            fixed.append(v)
        else:
            placement.append(v)
    return placement, internal, fixed


class PlaceNewsAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "place-news"
        self.category = "Layout"
        self.description = "Optimize component placement using simulated annealing"
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), 'icon.png')
        self.dark_icon_file_name = os.path.join(os.path.dirname(__file__), 'icon_dark.png')

    def Run(self):
        board = pcbnew.GetBoard()

        moveable_count = sum(1 for fp in board.GetFootprints() if not fp.IsLocked())
        if moveable_count < 2:
            wx.MessageBox(
                "Need at least 2 unlocked footprints to optimize.",
                "place-news", wx.OK | wx.ICON_WARNING,
            )
            return

        # Quick board scan for dialog preview (selected_only=False — just for stats)
        preview_model = extract_board_model(board)
        preview_cs = CostState(preview_model, quiet=True)
        board_info = _build_board_info(preview_model, preview_cs)

        # Show settings dialog with board info
        from .settings_dialog import SettingsDialog
        dlg = SettingsDialog(None, board_info=board_info)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        config = dlg.build_config()
        # Learned aesthetic policy (with a little exploration)
        learner, policy_eps = _apply_learned_policy(config)
        selected_only = dlg.move_selected_only
        excluded_net_names = dlg.excluded_net_names
        use_isolation = dlg.use_isolation
        exclusive_areas = dlg.exclusive_areas
        dlg.Destroy()

        # If "move selected only" is on, verify something is selected
        if selected_only:
            n_selected = sum(
                1 for fp in board.GetFootprints()
                if fp.IsSelected() and not fp.IsLocked()
            )
            if n_selected == 0:
                wx.MessageBox(
                    "No unlocked components are selected.\n\n"
                    "Select the components you want to optimize in the PCB editor first,\n"
                    "or uncheck 'Move selected components only'.",
                    "place-news", wx.OK | wx.ICON_WARNING,
                )
                return

        # Save original positions for undo
        original_positions = save_original_positions(board)

        # Extract board model into pure Python structures
        model = extract_board_model(board, selected_only=selected_only,
                                    use_isolation=use_isolation,
                                    exclusive_areas=exclusive_areas)

        # Apply user's net exclusion choices (overrides auto-detection)
        for nc, net in model.nets.items():
            net.is_excluded = net.net_name in excluded_net_names

        # Compute initial HPWL for display
        initial_cs = CostState(model)
        initial_hpwl = initial_cs.hpwl
        initial_cost = initial_cs.total_cost

        # Custom progress dialog with temperature gradient bar and wirelength bar
        progress = _ProgressDialog(None, initial_hpwl)
        progress.Show()

        cancelled = False

        def progress_callback(step, total_steps, temperature,
                              current_cost, best_cost, best_hpwl, t0,
                              current_hpwl):
            nonlocal cancelled
            if initial_hpwl > 0:
                improvement = max(0.0, (1.0 - best_hpwl / initial_hpwl) * 100)
            else:
                improvement = 0.0

            pct = min(int(step / max(total_steps, 1) * 100), 99)

            # Determine phase
            if temperature == 0.0:
                phase = "Refining"
            elif total_steps > 0 and step / total_steps > 0.67:
                phase = "Reheating"
            else:
                phase = "Annealing"

            progress.update_state(
                phase=phase,
                temperature=temperature,
                t0=t0,
                improvement=improvement,
                hpwl_mm=best_hpwl / 1e6,
                pct=pct,
                current_hpwl=current_hpwl,
                best_hpwl=best_hpwl,
            )
            wx.Yield()
            if progress.cancelled:
                cancelled = True
                return False
            return True

        # Run SA (includes greedy refinement at the end)
        start_time = time.time()
        result = run_sa(model, config=config, progress_callback=progress_callback)
        elapsed = time.time() - start_time

        progress._pulse_timer.Stop()
        progress.Destroy()

        if cancelled:
            # Compute HPWL improvement (model has best positions restored by run_sa)
            cancel_cs = CostState(model)
            cancel_hpwl_pct = max(0.0, (initial_hpwl - cancel_cs.hpwl) / initial_hpwl * 100) \
                if initial_hpwl > 0 else 0.0
            answer = wx.MessageBox(
                f"Optimization cancelled.\n\n"
                f"Best HPWL improvement so far: {cancel_hpwl_pct:.1f}%\n"
                f"Apply partial result?",
                "place-news",
                wx.YES_NO | wx.ICON_QUESTION,
            )
            if answer != wx.YES:
                restore_original_positions(board, original_positions)
                return

        # Apply optimized positions to the board
        apply_model_to_board(board, model)

        # Verify: re-extract model from KiCad and compare HPWL
        # This catches any position application issues
        verify_model = extract_board_model(board, selected_only=selected_only,
                                           use_isolation=use_isolation,
                                           exclusive_areas=exclusive_areas)
        verify_cs = CostState(verify_model)
        iso_placement, iso_internal, iso_fixed = _isolation_report(verify_model)
        area_violations = verify_cs.area_violations()

        # Auto-place silkscreen reference designators
        from .silkscreen import extract_silkscreen_model, place_silkscreen, apply_silkscreen
        silk_model = extract_silkscreen_model(board, verify_model)
        silk_positions = place_silkscreen(silk_model)
        n_silk_moved = apply_silkscreen(board, silk_model, silk_positions, board_model=verify_model)

        # Use verified HPWL (matches what next run will see)
        hpwl_before_mm = initial_hpwl / 1e6
        hpwl_after_mm = verify_cs.hpwl / 1e6

        # Debug: always log model vs re-extracted comparison
        model_cs = CostState(model)
        _write_debug_log(model, verify_model, model_cs, verify_cs)

        if initial_hpwl > 0:
            hpwl_change_pct = (initial_hpwl - verify_cs.hpwl) / initial_hpwl * 100
        else:
            hpwl_change_pct = 0.0

        # Count overlapping pairs
        n_overlaps = sum(1 for v in verify_cs._pair_overlaps.values() if v > 0)

        # Count keepout violations
        n_keepout = sum(1 for v in verify_cs._fp_keepout.values() if v > 0)

        # Build result summary for logging
        if hpwl_change_pct >= 0:
            hpwl_msg = f"HPWL: {hpwl_before_mm:.1f} mm \u2192 {hpwl_after_mm:.1f} mm ({hpwl_change_pct:.1f}% shorter)"
        else:
            hpwl_msg = f"HPWL: {hpwl_before_mm:.1f} mm \u2192 {hpwl_after_mm:.1f} mm ({-hpwl_change_pct:.1f}% longer)"
        def _viol_text(v):
            return (f"{_pad_label(verify_model, v.fp_a, v.pad_a)} ↔ "
                    f"{_pad_label(verify_model, v.fp_b, v.pad_b)}: "
                    f"{v.gap / 1e6:.2f} / {v.required / 1e6:.2f} mm")

        iso_lines = [_viol_text(v) for v in iso_placement]
        internal_refs = sorted({verify_model.footprints[v.fp_a].reference for v in iso_internal})
        result_summary = (
            f"Optimization complete!\n\n"
            f"{hpwl_msg}\n"
            f"Overlaps: {n_overlaps} pairs\n"
            f"Isolation (between parts): {len(iso_placement)} pad pairs\n"
            + "".join(f"  - {line}\n" for line in iso_lines[:20])
            + f"Isolation inside footprints: {len(iso_internal)} pad pairs "
              f"({', '.join(internal_refs)})\n"
            f"Isolation between locked parts: {len(iso_fixed)} pad pairs\n"
            f"Placement areas: {len(area_violations)} part(s) outside / intruding\n"
            f"Keep-out violations: {n_keepout}\n"
            f"Silkscreen: {n_silk_moved} refs repositioned\n"
            + (f"Ratsnest crossings: {result.untangle.crossings_before} -> "
               f"{result.untangle.crossings_after} ({result.untangle.swaps} swaps, "
               f"{result.untangle.flips} turned)\n" if getattr(result, 'untangle', None) else "")
            + f"Moves: {result.total_moves} total, {result.accepted_moves} accepted\n"
            f"Time: {elapsed:.1f}s"
        )

        # Append results to profile.log so they persist even if dialog is dismissed
        try:
            log_path = os.path.join(os.path.dirname(__file__), 'profile.log')
            with open(log_path, 'a') as f:
                f.write(f"\n{result_summary}\n")
        except Exception:
            pass

        # Show results dialog with Accept/Reject
        rdlg = _ResultsDialog(
            None,
            hpwl_before_mm=hpwl_before_mm,
            hpwl_after_mm=hpwl_after_mm,
            hpwl_change_pct=hpwl_change_pct,
            n_overlaps=n_overlaps,
            n_keepout=n_keepout,
            n_silk_moved=n_silk_moved,
            elapsed=elapsed,
            total_moves=result.total_moves,
            accepted_moves=result.accepted_moves,
            iso_checked=bool(verify_model.isolation and verify_model.isolation.active),
            iso_lines=iso_lines,
            n_iso_internal=len(iso_internal),
            internal_refs=internal_refs,
            n_iso_fixed=len(iso_fixed),
            n_areas=len(verify_model.placement_areas),
            n_area_violations=len(area_violations),
            alignment=getattr(result, 'alignment', None),
            untangle=getattr(result, 'untangle', None),
        )
        accepted = rdlg.ShowModal() == wx.ID_OK
        rdlg.Destroy()

        if not accepted:
            restore_original_positions(board, original_positions)
        _learn_from_run(learner, board, policy_eps, accepted)


def _apply_learned_policy(config):
    """Set the tidy-up knobs from what was learned about the user's taste."""
    try:
        from .aesthetic_learning import Learner, PLACEMENT_KNOBS
        learner = Learner()
        values, eps = learner.sample(PLACEMENT_KNOBS)
        if config.align_rows:
            config.align_budget = values['align_budget']
        else:
            eps.pop('align_budget', None)
        config.orient_parts = learner.orientation_enabled
        if config.orient_parts:
            config.orient_budget = values['orient_budget']
        else:
            eps.pop('orient_budget', None)
        if config.untangle:
            config.untangle_budget = values['untangle_budget']
        else:
            eps.pop('untangle_budget', None)
        return learner, eps
    except Exception:
        return None, {}


def _learn_from_run(learner, board, eps, accepted: bool) -> None:
    """Remember the placement just produced (to learn from later edits); a
    rejected result is a mild negative reward for the knobs tried."""
    if learner is None:
        return
    try:
        if not accepted:
            learner._reinforce(eps, -0.3)
            learner.save()
            return
        from .aesthetic_check import check, extract_geometry
        from .aesthetic_learning import board_state
        result = check(extract_geometry(board), learner.weights)
        learner.record_run(str(board.GetFileName() or ''), 'placement', result.qualities,
                           board_state(board), eps)
        learner.save()
    except Exception:
        pass
