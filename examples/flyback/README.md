# Example: isolated flyback

A small isolated flyback converter as it looks right after *Update PCB from
Schematic*: every part piled up in the middle of a 100 x 60 mm board.

- Net classes: **HV** (rectified bus, switch node: 0.6 mm), **PRI** (primary
  control: 0.25 mm), **SEC** (secondary: 0.2 mm).
- `flyback.kicad_dru`: 6 mm creepage and 4 mm clearance between the primary
  side (HV, PRI) and the secondary side (SEC).
- A placement area keeps the `/Secondary/` sheet on the right; rule areas
  keep footprints / pads out of a corner and of two back-side regions.

Open `flyback.kicad_pcb` in KiCad 10, run **place-news**, then
**place-news — Route (Freerouting)**, then the DRC.
