# place-news

KiCad action plugins for boards where **isolation matters** (switch-mode power
supplies, mains and high-voltage circuits):

- **place-news** — automatic component placement (simulated annealing) that
  keeps parts as far apart as your clearance and creepage rules demand, and
  honours KiCad rule areas (keep-outs, placement areas). Then a tidy-up: parts
  in rows and columns, readable references, and an untangled ratsnest (fewer
  vias).
- **place-news — Route (Freerouting)** — autorouting with
  [Freerouting](https://github.com/freerouting/freerouting), with your net-class
  clearances and custom `.kicad_dru` rules (clearance, creepage) passed to the
  router; fewer vias, a try on the two outer layers when the board has more,
  and a ground plane on the top layer that keeps the creepage distances.
- **place-news — Aesthetic check** — scores the look and the cost of the
  placement and routing, marks every issue on the board and in a visual
  report, estimates how many layers the board needs, and learns your taste
  from your opinion and from your own corrections.
- **place-news — Sourcing (BOM)** — stock, prices, minimum orders, lifecycle
  and replacements of every BOM line at DigiKey, Mouser, Farnell, TME and
  LCSC; the chosen parts written into the components' fields, a BOM with the
  designators, unit prices and minimum orders, and one order list per
  distributor.

KiCad's own DRC stays the judge: placement and routing can finish with a DRC run, and the
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
- For sourcing: an internet connection, and the free API keys of the
  distributors you use (LCSC needs none).
- Nothing else: pure Python, no extra packages.

## Installation

**Plugin and Content Manager (recommended):** in KiCad, *Plugin and Content
Manager → Install from File…* and choose `place-news-<version>-pcm.zip`.

**Manual:** in the PCB editor, *Tools → External Plugins → Open Plugin
Directory*, copy the `plugin/` folder there as `place_news/`, then *Tools →
External Plugins → Refresh Plugins*.

Four toolbar buttons appear: placement (green grid), routing (trace with a
dashed isolation line), the aesthetic check (magnifier) and sourcing (cart).

To try it: open `examples/flyback/flyback.kicad_pcb` (an isolated flyback
with HV / PRI / SEC net classes and 6 mm creepage rules, parts still piled up)
and run the placement and the routing.

## Placement

1. Draw the board outline on Edge.Cuts; lock connectors, mounting holes and
   anything mechanically fixed.
2. Optional: rule areas with *Keep out footprints / pads* reserve space; rule
   areas with *Placement* enabled (sheet, component class or group) keep their
   parts inside — and, with *Exclusive placement areas*, other parts outside.
3. Click **place-news**, choose a preset, **Optimize**.
4. The result dialog lists wirelength, overlaps, isolation, placement areas,
   keep-outs, alignment, ratsnest crossings and silkscreen. *Reject* (or
   *Edit → Undo*) restores the previous placement.

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
- **Untangled ratsnest** (*Reduce cost: untangle the ratsnest*, on by
  default): every crossing of two ratsnest lines tends to cost a via. Identical
  parts swap places with their nearest twins and two-pad parts turn round
  (180°) when that removes crossings; rows, columns and orientation axes stay.
  Power nets are left out (they go to a plane or a pour). Same safeguards as
  above, 2 % of wirelength at most. On KiCad's demo boards, with their
  hand-made placements: complex_hierarchy 28 → 18 crossings, pic_programmer
  22 → 17.

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
3. Cost options: **Fewer vias** (Freerouting's via cost × 3; if that leaves
   connections unrouted, the board is routed again at the usual cost and the
   more complete result is kept) and, on boards with more than two copper
   layers, **Try the 2 outer layers first** (the inner layers become planes
   for Freerouting; if every connection is made, that result is kept and the
   summary says the inner layers can go, unless they are planes).
4. **Ground plane**: *Pour on the top layer (F.Cu)*, with the ground net
   (GND, PGND, 0V… found by name). See below.
5. **Route**. Freerouting runs in the background (progress, *Cancel*), the
   routes are imported, the pour is made, then KiCad's DRC runs on a copy of
   the board and the summary shows unrouted connections, vias, the routing
   room, the pour and the clearance / creepage results.
6. *Edit → Undo* reverts the routing and the pour.

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

### Ground plane on the top layer

The chosen net is poured over the whole top layer (zones named
`place-news pour`; running again replaces them). KiCad's zone filler keeps
the clearances, but not the creepage distances — and a pour runs along the
board surface, the very path creepage is measured on. So every copper item on
the top layer whose required distance to the pour's net (clearance, physical
clearance or creepage, from the net classes and the custom rules) is larger
than the plain clearance is kept clear by that distance (+ 20 µm for the
fill's rounding). Each cluster of such items — a primary side, say — gets one
convex cut-out: a clean isolation boundary, no copper fingers between the
primary parts. Islands are removed; pads connect with thermal reliefs. On the
example flyback, KiCad's DRC finds no clearance, creepage or unconnected item
with the pour.

### Known KiCad behaviour

KiCad's creepage check treats rule areas as items without a net: with a rule
such as `A.hasNetclass('HV') && !B.hasNetclass('HV')`, it reports "creepage"
between HV pads and nearby rule-area outlines. Writing the rule against an
explicit class (`B.hasNetclass('SEC')`) avoids it. The route summary counts
these separately.

## Aesthetic check

**place-news — Aesthetic check** scores the board out of 100 in three groups
and numbers every issue on the *User.Comments* layer (one group, easy to
delete) and in an HTML report next to the board (`<board>-aesthetic-check.html`:
drawing of the board, markers, criteria, issues; hover a row to find its
marker).

- **Placement**: same-type parts aligned, one orientation per part type,
  references readable (0° / 90°), on their part or its axis, clear of pads.
- **Routing**: 45° steps, no acute angles, no small zigzags, no long detours
  (routed length against the shortest 45° tree), tracks leaving pads straight.
- **Cost**: few vias, untangled ratsnest, few drill sizes, no drill under
  0.3 mm, no track under 0.15 mm, parts on one side, no empty inner layer,
  layer count fitting the routing room, board not larger than needed.

**Learning.** The dialog asks what you think (👍 / 👎, and optionally what
bothers you most): the criteria you name gain weight. When you edit a board
place-news produced and run the check again, the criteria your edits improved
gain weight, those you let degrade lose some, and the knobs place-news turned
(tidy-up budgets, via cost) are rewarded or penalised by how much you had to
fix — a small evolution-strategies loop: each run tries the knobs a little
differently and keeps what you liked. Everything stays in
`place-news/place-news-aesthetics.json` in KiCad's settings folder.

### How many layers?

The routing room per net — the rule of thumb

    (board area × routing layers − parts area) / number of nets

— is compared with what an average net needs: its ratsnest length (45°
spanning tree, × 1.3 for detours) times its track pitch (net-class width +
clearance), over the share of free area tracks can really use (60 %). That is
Coors' "wiring demand / routing capacity per layer". Parts with pads in a grid
(BGA) also need layers to escape: rings of pads ÷ (tracks between pads + 1).
Inner layers used as planes are not routing room, and never a cost (power,
EMC). Fewer layers are suggested only with 30 % of room to spare.

Calibrated on the boards shipped with KiCad: the estimated track area matches
the routed one within about 10 %. Quick rules of thumb are shown next to it
for comparison; they ask for far too many layers on fine-pitch SMD boards:

| Board | Layers | Pin density (in² per 14 pins) | Parts / board | Pins per cm² | place-news |
|---|---|---|---|---|---|
| pic_programmer (through-hole) | 2 | 1.46 → 2 | 49 % | 1.5 | 2 fit |
| interf_u | 2 | 0.71 → 2 + planes | 52 % | 3.1 | 2 fit |
| sonde xilinx | 2 | 0.70 → 2 + planes | 80 % → "6–8" | 3.1 | 2 fit |
| StickHub (fine-pitch SMD) | 2 | 0.05 → 10 signal | 152 % → "6–8" | 45 → "4+" | 2 just fit |
| video | 2 + 2 planes | 0.30 → 8 signal | 74 % → "6–8" | 7.3 | 2 fit |
| vme-wren (FPGA BGA) | 12 | 0.11 → 10 signal | 63 % | 19 | 8 routing (BGA escape) |

## Sourcing (BOM)

**place-news — Sourcing (BOM)** asks the distributors, for every line of the
bill of materials, for the stock, the price breaks, the minimum order and the
multiple, the lifecycle (active, NRND, end of life, obsolete) and the
replacements; then it writes the chosen parts into the components and saves a
BOM and one order list per distributor.

### Keys

Each distributor has a free API for its catalogue. Create your own keys once
and enter them in the dialog; the *Test* button next to each one tells at once
whether it works.

| Distributor | What to create (free) | Where |
|---|---|---|
| DigiKey | an app with *Product Information v4*, client credentials: client ID and secret | [developer.digikey.com](https://developer.digikey.com) |
| Mouser | a *Search API* key | [mouser.com/api-hub](https://www.mouser.com/api-hub/) |
| Farnell / element14 | a *Product Search API* key (store: `fr.farnell.com` by default) | [partner.element14.com](https://partner.element14.com) |
| TME (beta) | an API v2 application: token and secret | [developers.tme.eu](https://developers.tme.eu) |
| LCSC | nothing: JLCPCB / LCSC public catalogue | — |

The keys stay in your KiCad settings folder (`place-news/sourcing.json`,
readable by you only) and only go to the distributor they belong to. Answers
are kept one day (`place-news/sourcing-cache.json`): running again is quick
and spares the daily quotas (Mouser: 1000 calls a day, 30 a minute — place-news
paces its calls).

### What is searched

Footprints with the same value, footprint, MPN, manufacturer and ratings
fields (Voltage, Tolerance, Power, Dielectric…) make one BOM line. DNP parts,
parts excluded from the BOM, mounting holes and fiducials are left out (and
listed in the report). For each line:

- **with an MPN field** (`MPN`, `Manufacturer_Part_Number`, `MFR_PN`, KiCost's
  `manf#`… any usual spelling): that part at every distributor;
- **with a distributor part number** (fields `DigiKey`, `Mouser`, `Farnell`,
  `TME`, `LCSC`, e.g. `LCSC = C25804`): that part, then its MPN at the others;
- **no MPN, but a value that is a part name** (`IRF840`, `UC3843BD1R2G`):
  searched as a part number;
- **no MPN, a resistor, capacitor or inductor**: searched by value, package
  and ratings. Only parts that fit are *proposed*: same package (read from the
  footprint: chip size, SOT / TO / SOIC with their pin count and isolated
  tab, radial diameter and pitch, axial length, …), same value, voltage,
  current and power at least as high, tolerance at most as wide, dielectric
  as good (C0G > X7R > X5R), safety class as good (Y1 > Y2 > X1 > X2). Without a
  tolerance in the schematic, resistors are 1 % and chip capacitors X5R or
  better (settings). A package or a rating that cannot be read is not
  accepted: a proposal is never a guess — give an MPN for such parts;
- any other part without a part number is reported "not found: needs a part
  number".

The package is checked for parts found by part number too: one MPN can be
sold in several package variants (LCSC lists PC817C in DIP-4 and in SMD-4P),
so the variant that fits the footprint is bought, and a part whose package
does not fit is flagged *check package*. Same pin count is not enough where
it does not fix the size: QFN / DFN / QFP bodies (QFN-32 5×5 or 7×7) and DIP
row spacings (300 or 600 mil) are compared when both sides give them.

### What is bought

- **Quantity**: per board × number of boards + spare parts (10 % by default,
  rounded up), then raised to the **minimum order**, rounded to the
  **multiple** (reels, cut tape) and priced at the **price break** reached.
  Lines buying the same part share one order (one minimum, one multiple).
- **Where**: the first distributor of *your list* (drag to reorder, untick to
  skip) that has the stock for the quantity actually bought — or, as an
  option, wherever the line total is the lowest. Never an obsolete part when
  there is another.
- **No surprise**: each line shows the unit price, the minimum order and the
  multiple, the line total, and the money spent beyond the need because of
  minimum orders; the totals are per distributor and per board. Prices are
  dated, in euros, excluding VAT and shipping; prices in another currency
  (LCSC: US dollars) are converted at the ECB rate of the day.
- **Replacements**: when a part is short, at the end of its life or not
  found, the distributors' own suggestions (DigiKey substitutes, Mouser
  suggested replacement, TME similar products, LCSC alternatives) and a
  search by parameters at every distributor give replacements with the same
  package and the ratings kept, in stock for the quantity bought.

The results dialog colours each line by status (OK, short stock, end of life,
proposed, not found, check package). Select a line to see every offer,
proposal and replacement; double-click one to use it.

### What you get

- **Write into the components**: fields `MPN`, `Manufacturer`, the part
  number at each distributor that carries the part (`DigiKey`, `Mouser`,
  `Farnell`, `TME`, `LCSC`) and the key parameters (`Voltage`, `Current`,
  `Power`, `Tolerance`, `Dielectric`, `Safety class` — only where the field is
  empty: your figures are kept). New fields are hidden, on the fabrication
  layer. To bring them into the schematic: *Tools → Update Schematic from
  PCB*, tick *Other fields*. *Edit → Undo* removes them from the board.
- **Save the BOM and the order lists**, next to the board:
  - `<board>-bom.csv` — designators, quantity per board / needed / ordered,
    value, package, footprint, manufacturer, MPN, distributor and part
    number, unit price, minimum order, multiple, line total, extra due to the
    minimum order, stock, lifecycle, lead time, status, replacements, link
    (`;` and decimal commas: opens as is in a French Excel);
  - `<board>-order-<distributor>.csv` — part number, quantity, customer
    reference (the designators; DigiKey and Mouser print it on each bag's
    label), MPN, manufacturer, unit price, minimum, multiple, total: upload it
    in the distributor's BOM / list tool (map the columns once);
  - `<board>-sourcing.html` — the report: totals, statuses and every line with
    its choices.

Tried live with LCSC; DigiKey, Mouser, Farnell and TME are tested on answers
shaped like their documented APIs — use the *Test* buttons with your keys.
TME's API v2 is new: its support is marked beta.

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
python3 -m unittest tests.test_optimizer tests.test_rules_isolation tests.test_routing \
  tests.test_aesthetics tests.test_cost tests.test_sourcing -v
```

These need no KiCad and no network (the distributors' answers are canned). With KiCad's Python (`pcbnew`, `kicad-cli`) the
end-to-end tests build an isolated flyback board, place it, check it with
KiCad's DRC, route it through Freerouting, pour GND on the top layer and check
again, and try the outer layers of a 4-layer version:

```bash
PLACE_NEWS_FREEROUTING_JAR=/path/to/freerouting-2.4.1-executable.jar \
  python3 -m unittest tests.e2e_kicad -v
xvfb-run -a python3 tests/gui_smoke.py          # placement action, dialogs included
xvfb-run -a python3 tests/gui_route_smoke.py    # routing action, dialogs included
xvfb-run -a python3 tests/gui_check_smoke.py    # aesthetic check and learning
xvfb-run -a python3 tests/gui_sourcing_smoke.py # sourcing with LCSC (network)
```

(The GUI scripts need the Python that KiCad's wxPython was built for.)

## Licence

[MIT](LICENSE). Freerouting is a separate program (GPL-3.0), not included.
