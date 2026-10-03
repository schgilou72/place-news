# place-news

KiCad action plugins for boards where **isolation matters** (switch-mode power
supplies, mains and high-voltage circuits):

- **place-news** — automatic component placement (simulated annealing) that
  keeps parts as far apart as your clearance and creepage rules demand, and
  honours KiCad rule areas (keep-outs, placement areas).
- **place-news — Route (Freerouting)** — autorouting with
  [Freerouting](https://github.com/freerouting/freerouting), with your net-class
  clearances and custom `.kicad_dru` rules (clearance, creepage) passed to the
  router.

KiCad's own DRC stays the judge: both actions can finish with a DRC run, and the
rules are always applied on the safe side (straight-line distance, never longer
than KiCad's creepage path).

place-news is derived from [CadMust-Neo](https://github.com/remiblokker/CadMust-Neo)
by Remi Blokker (MIT licence).

## Requirements

- KiCad 10.0 (tested with 10.0.6). Uses the SWIG Python API, which KiCad 10
  still ships (it is planned to be removed in KiCad 11).
- For routing: **Java 25 or later** and **Freerouting 2.4 or later**
  (`freerouting-2.x.x-executable.jar`, from the
  [Freerouting releases](https://github.com/freerouting/freerouting/releases)
  or installed by KiCad's Freerouting plugin). Java: e.g.
  [Eclipse Temurin](https://adoptium.net/temurin/releases/).
- Nothing else: pure Python, no extra packages.

## Installation

**Plugin and Content Manager (recommended):** in KiCad, *Plugin and Content
Manager → Install from File…* and choose `place-news-<version>-pcm.zip`.

**Manual:** in the PCB editor, *Tools → External Plugins → Open Plugin
Directory*, copy the `plugin/` folder there as `place_news/`, then *Tools →
External Plugins → Refresh Plugins*.

Two toolbar buttons appear: placement (green grid) and routing (trace with a
dashed isolation line).

To try it: open `examples/flyback/flyback.kicad_pcb` (an isolated flyback
with HV / PRI / SEC net classes and 6 mm creepage rules, parts still piled up)
and run both actions.

## Placement

1. Draw the board outline on Edge.Cuts; lock connectors, mounting holes and
   anything mechanically fixed.
2. Optional: rule areas with *Keep out footprints / pads* reserve space; rule
   areas with *Placement* enabled (sheet, component class or group) keep their
   parts inside — and, with *Exclusive placement areas*, other parts outside.
3. Click **place-news**, choose a preset, **Optimize**.
4. The result dialog lists wirelength, overlaps, isolation, placement areas,
   keep-outs, alignment and silkscreen. *Reject* (or *Edit → Undo*) restores the
   previous placement.

### Tidy layout

- **Rows and columns** (*Align parts of the same type in rows / columns*, on by
  default): parts with the same footprint and orientation that sit roughly in
  a row or a column are put on one line through their centres, and lines of
  three or more are evenly spaced. A locked part of the same type serves as
  the anchor. A move is kept only if no rule gets worse (overlap, isolation,
  keep-outs, placement areas, board edge) and the wirelength grows by 3 % at
  most.
- **Readable references**: each reference is centred on its part, horizontal
  or vertical reading bottom to top (never upside down nor top to bottom),
  along the long side of the part. It never touches a pad or another
  reference: when pads take the centre, it goes beside the part, centred on
  its axis. References of locked parts are left alone.

### Isolation during placement

For every pair of pads on different nets, the required distance is the larger
of:

- the clearance KiCad applies: net-class clearance (the larger of the two), or
  the last matching custom `clearance` rule, never below the board minimum;
- `physical_clearance` rules;
- `creepage` rules. KiCad evaluates creepage **between nets** (with a stand-in
  track of each net on every copper layer), so creepage conditions can use net
  names, net classes and layers, not footprint properties.

Non-plated holes (mounting holes) count for creepage only. Pairs inside one
footprint cannot be fixed by placement; they are reported separately ("inside
footprints"), as are pairs between two locked parts.

### Supported rule conditions

`A.`/`B.` with `NetClass`, `NetName`, `Type`, `Pad_Type`, `Layer`,
`hasNetclass()`, `hasExactNetclass()`, `hasComponentClass()`,
`memberOfSheet()`, `memberOfSheetOrChildren()`, `memberOfFootprint()`
(reference, `lib:footprint` or `${Class:…}`), `memberOfGroup()`,
`existsOnLayer()`, `isPlated()`, combined with `!`, `&&`, `||`, `==`, `!=` and
parentheses. Semantics follow KiCad: last matching rule wins, a rule matches if
its condition holds for (A, B) or (B, A), `&&` binds less tightly than `||`,
string `==` is case-insensitive and accepts `*`/`?` in a right-hand literal.
A rule using anything else is listed as ignored in the dialogs — never guessed.

Example (`<project>.kicad_dru`):

```
(version 1)
(rule "isolation creepage"
  (constraint creepage (min 6mm))
  (condition "(A.hasNetclass('HV') || A.hasNetclass('PRI')) && B.hasNetclass('SEC')"))
(rule "isolation clearance"
  (constraint clearance (min 4mm))
  (condition "(A.hasNetclass('HV') || A.hasNetclass('PRI')) && B.hasNetclass('SEC')"))
```

## Routing with Freerouting

1. Place the parts (with place-news or by hand) and save the board.
2. Click **place-news — Route (Freerouting)**. The dialog finds the
   Freerouting jar and Java (or lets you pick them) and shows the isolation
   distances that will be given to the router.
3. **Route**. Freerouting runs in the background (progress, *Cancel*), the
   routes are imported, then KiCad's DRC runs on a copy of the board and the
   summary shows unrouted connections and clearance / creepage results.
4. *Edit → Undo* reverts the routing. Refill zones (*B*) afterwards.

What place-news changes in the Specctra file before routing:

- **Rule areas**: KiCad exports every rule area as a full routing keep-out,
  even areas that only keep out footprints or pads, and placement areas. Each
  area now follows its own *Keep out tracks / vias* settings.
- **Isolation rules**: Freerouting knows one clearance per pair of net
  classes. Each class gets the clearance its own nets need between them, and a
  `class_class` rule is written for every pair of classes that needs another
  value (e.g. 6 mm between primary and secondary classes). Creepage becomes a
  straight-line clearance.
- KiCad's `smd_smd` pad-to-pad allowance is dropped when it would let the
  default class bypass these rules; Freerouting may then report pad-to-pad
  "violations" inside fine-pitch footprints (pads are fixed; KiCad's DRC is
  what counts).
- Freerouting ≥ 2.4 may fan out SMD pins with the default class's vias when a
  class's own via does not fit; that is disabled, since those vias would carry
  the default clearance.
- Parts never move: the session's placement section is ignored on import.

Freerouting runs with `--usage_and_diagnostic_data.disable_analytics=true`.
It still checks GitHub for a newer version when it starts.

### Known KiCad behaviour

KiCad's creepage check treats rule areas as items without a net: with a rule
such as `A.hasNetclass('HV') && !B.hasNetclass('HV')`, it reports "creepage"
between HV pads and nearby rule-area outlines. Writing the rule against an
explicit class (`B.hasNetclass('SEC')`) avoids it. The route summary counts
these separately.

## Tips

- Give the high-voltage and the isolated sides their own net classes (e.g. HV,
  PRI, SEC) and write isolation rules between classes.
- Parts that bridge the barrier (transformers, optocouplers, Y capacitors) must
  have pin spacing at least the creepage distance; otherwise their pins are
  reported "inside footprints" and the router cannot leave those pins legally.
- "Move selected only" reworks one area without touching the rest.
- Power nets (GND, VCC, …) are excluded from wirelength; adjust the list in
  Normal / Expert mode.

## Tests

```bash
python3 -m unittest tests.test_optimizer tests.test_rules_isolation tests.test_routing -v
```

These need no KiCad. With KiCad's Python (`pcbnew`, `kicad-cli`) the
end-to-end tests build an isolated flyback board, place it, check it with
KiCad's DRC, then route it through Freerouting and check again:

```bash
PLACE_NEWS_FREEROUTING_JAR=/path/to/freerouting-2.4.1-executable.jar \
  python3 -m unittest tests.e2e_kicad -v
xvfb-run -a python3 tests/gui_smoke.py          # placement action, dialogs included
xvfb-run -a python3 tests/gui_route_smoke.py    # routing action, dialogs included
```

## Licence

[MIT](LICENSE). Freerouting is a separate program (GPL-3.0), not included.
