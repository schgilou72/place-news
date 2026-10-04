"""Visual report of the aesthetic check: a self-contained HTML page with a
drawing of the board (SVG) and numbered markers on every issue.
Pure Python, no external resources."""
from __future__ import annotations

import html
from typing import List, Optional, Sequence

from .aesthetic_check import CRITERIA, BoardGeom, CheckResult, Issue

MM = 1_000_000
LAYER_COLORS = {'F.Cu': '#c83737', 'B.Cu': '#3a6fd8'}
INNER_COLOR = '#c99a2e'
GROUP_COLORS = {'placement': '#f08c00', 'routing': '#d6336c', 'cost': '#1c7ed6'}


def _f(v: float) -> str:
    return f'{v / MM:.3f}'


def _layer_color(layer: str) -> str:
    return LAYER_COLORS.get(layer, INNER_COLOR)


def render_svg(geom: BoardGeom, issues: Sequence[Issue], max_width_px: int = 1100) -> str:
    """The board seen from the top, with numbered issue markers."""
    x1, y1, x2, y2 = geom.outline
    pad = 3 * MM
    vx, vy = x1 - pad, y1 - pad
    vw, vh = (x2 - x1) + 2 * pad, (y2 - y1) + 2 * pad
    w_px = max_width_px
    h_px = int(w_px * vh / max(vw, 1))
    out: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_f(vx)} {_f(vy)} {_f(vw)} {_f(vh)}" '
        f'width="{w_px}" height="{h_px}" style="max-width:100%;height:auto" '
        f'font-family="DejaVu Sans, Arial, sans-serif">',
        f'<rect x="{_f(x1)}" y="{_f(y1)}" width="{_f(x2 - x1)}" height="{_f(y2 - y1)}" '
        f'fill="#f3f7f1" stroke="#777" stroke-width="0.15"/>',
    ]
    # zone fills (pours, planes), back first
    for layer, _net, rings in sorted(geom.fills, key=lambda f: 0 if f[0] == 'B.Cu' else 2 if f[0] == 'F.Cu' else 1):
        d = ' '.join('M ' + ' L '.join(f'{_f(x)} {_f(y)}' for x, y in ring) + ' Z'
                     for ring in rings if len(ring) >= 3)
        if d:
            out.append(f'<path d="{d}" fill="{_layer_color(layer)}" fill-opacity="0.18" '
                       f'fill-rule="evenodd" stroke="none"/>')
    # back layer first, then inner, then front
    order = sorted(geom.segs, key=lambda s: (0 if s.layer == 'B.Cu' else 2 if s.layer == 'F.Cu' else 1))
    for s in order:
        out.append(f'<line x1="{_f(s.a[0])}" y1="{_f(s.a[1])}" x2="{_f(s.b[0])}" y2="{_f(s.b[1])}" '
                   f'stroke="{_layer_color(s.layer)}" stroke-width="{_f(s.width)}" '
                   f'stroke-linecap="round" opacity="0.85"/>')
    for p in geom.parts:
        b = p.box
        out.append(f'<rect x="{_f(b[0])}" y="{_f(b[1])}" width="{_f(b[2] - b[0])}" '
                   f'height="{_f(b[3] - b[1])}" fill="none" stroke="#c46ac4" stroke-width="0.08"/>')
    for pd in geom.pads:
        b = pd.box
        color = '#222' if pd.hole else '#c83737' if 'F' in pd.sides else '#3a6fd8'
        if pd.round:
            r = (b[2] - b[0]) / 2
            out.append(f'<circle cx="{_f(pd.center[0])}" cy="{_f(pd.center[1])}" r="{_f(r)}" '
                       f'fill="{color}"/>')
        else:
            out.append(f'<rect x="{_f(b[0])}" y="{_f(b[1])}" width="{_f(b[2] - b[0])}" '
                       f'height="{_f(b[3] - b[1])}" fill="{color}"/>')
    for v in geom.vias:
        out.append(f'<circle cx="{_f(v.center[0])}" cy="{_f(v.center[1])}" r="{_f(v.diameter / 2)}" '
                   f'fill="#8a8a8a"/><circle cx="{_f(v.center[0])}" cy="{_f(v.center[1])}" '
                   f'r="{_f(v.diameter / 5)}" fill="#fff"/>')
    for p in geom.parts:
        if not p.ref_visible or p.ref_box is None:
            continue
        b = p.ref_box
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        vertical = abs(((p.ref_angle + 45) % 180) - 45) > 45
        size = (b[2] - b[0]) if vertical else (b[3] - b[1])
        fs = max(0.4 * MM, 0.55 * size)
        rot = f' transform="rotate(-90 {_f(cx)} {_f(cy)})"' if vertical else ''
        out.append(f'<text x="{_f(cx)}" y="{_f(cy)}" font-size="{_f(fs)}" fill="#b89400" '
                   f'text-anchor="middle" dominant-baseline="central"{rot}>{html.escape(p.ref)}</text>')
    for n, iss in enumerate(issues, start=1):
        color = GROUP_COLORS.get(CRITERIA[iss.criterion][0], '#d6336c')
        out.append(f'<g class="mk" id="mk{n}"><circle cx="{_f(iss.x)}" cy="{_f(iss.y)}" r="1.3" '
                   f'fill="none" stroke="{color}" stroke-width="0.25"/>'
                   f'<circle cx="{_f(iss.x + 1.1 * MM)}" cy="{_f(iss.y - 1.1 * MM)}" r="0.75" '
                   f'fill="{color}"/><text x="{_f(iss.x + 1.1 * MM)}" y="{_f(iss.y - 1.1 * MM)}" '
                   f'font-size="0.8" fill="#fff" text-anchor="middle" dominant-baseline="central">'
                   f'{n}</text><title>{n}. {html.escape(iss.message)}</title></g>')
    out.append('</svg>')
    return '\n'.join(out)


def _bar(q: Optional[float]) -> str:
    if q is None:
        return '<span class="na">n/a</span>'
    pct = 100.0 * q
    color = '#2f9e44' if pct >= 80 else '#f08c00' if pct >= 50 else '#e03131'
    return (f'<span class="bar"><span style="width:{pct:.0f}%;background:{color}"></span></span>'
            f' {pct:.0f}%')


def build_html(title: str, geom: BoardGeom, result: CheckResult, issues: Sequence[Issue],
               learning_lines: Sequence[str] = ()) -> str:
    score = result.score
    rows = []
    for k, (group, label, _scale) in CRITERIA.items():
        n = sum(1 for i in issues if i.criterion == k)
        rows.append(f'<tr><td>{group}</td><td>{html.escape(label)}</td>'
                    f'<td>{_bar(result.qualities[k])}</td><td class="num">{n}</td>'
                    f'<td class="num">{result.weights.get(k, 1.0):.2f}</td></tr>')
    issue_rows = []
    for n, iss in enumerate(issues, start=1):
        group = CRITERIA[iss.criterion][0]
        issue_rows.append(f'<tr data-mk="mk{n}"><td class="num">{n}</td>'
                          f'<td><span class="dot {group}"></span>{html.escape(CRITERIA[iss.criterion][1])}'
                          f'</td><td>{html.escape(iss.message)}</td>'
                          f'<td class="num">{iss.x / MM:.1f}, {iss.y / MM:.1f}</td></tr>')

    def fmt(v: Optional[float]) -> str:
        return '—' if v is None else f'{v:.0f}'

    learning = ''.join(f'<li>{html.escape(line)}</li>' for line in learning_lines)
    room = ''
    if result.layers is not None:
        quick = '<br>'.join(html.escape(line) for line in result.layers.quick_rules())
        room = (f'<p class="room">{html.escape(result.layers.describe())}<br>'
                f'<small>For comparison, quick rules of thumb (they ask for too many layers on '
                f'fine-pitch SMD boards):<br>{quick}</small></p>')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --fg:#1d2326; --muted:#5f6b70; --line:#dfe5e8; --bg:#ffffff; --panel:#f6f8f9; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:20px 16px 40px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .sub {{ color:var(--muted); margin:0 0 18px; }}
.scores {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:18px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 16px; min-width:150px; }}
.card b {{ display:block; font-size:30px; }} .card span {{ color:var(--muted); font-size:13px; }}
table {{ border-collapse:collapse; width:100%; margin:8px 0 22px; }}
th, td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }}
th {{ font-size:13px; color:var(--muted); font-weight:600; }}
td.num, th.num {{ text-align:right; white-space:nowrap; }}
.bar {{ display:inline-block; width:120px; height:9px; background:#e9ecef; border-radius:5px; overflow:hidden; vertical-align:middle; }}
.bar span {{ display:block; height:100%; }} .na {{ color:var(--muted); }}
.board {{ border:1px solid var(--line); border-radius:10px; padding:8px; background:#fff; overflow:auto; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }}
.dot.placement {{ background:#f08c00; }} .dot.routing {{ background:#d6336c; }} .dot.cost {{ background:#1c7ed6; }}
.room {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 14px; margin:0 0 18px; }}
tr[data-mk]:hover {{ background:#fff4e6; cursor:pointer; }}
.mk.hl circle {{ stroke-width:0.5; }}
h2 {{ font-size:17px; margin:22px 0 6px; }}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="sub">place-news aesthetic check · orange: placement · pink: routing · blue: cost · hover a row to find its marker</p>
<div class="scores">
 <div class="card"><b>{fmt(score)}</b><span>overall / 100</span></div>
 <div class="card"><b>{fmt(result.group_score('placement'))}</b><span>placement</span></div>
 <div class="card"><b>{fmt(result.group_score('routing'))}</b><span>routing</span></div>
 <div class="card"><b>{fmt(result.group_score('cost'))}</b><span>cost</span></div>
 <div class="card"><b>{len(issues)}</b><span>issues marked</span></div>
</div>
{room}
<div class="board">{render_svg(geom, issues)}</div>
<h2>Criteria</h2>
<table><tr><th>Group</th><th>Criterion</th><th>Quality</th><th class="num">Issues</th><th class="num">Weight</th></tr>
{''.join(rows)}</table>
<h2>Issues</h2>
<table><tr><th class="num">#</th><th>Criterion</th><th>Detail</th><th class="num">x, y (mm)</th></tr>
{''.join(issue_rows) or '<tr><td colspan="4">No issue found.</td></tr>'}</table>
<h2>What place-news has learned</h2><ul>{learning or '<li>Nothing yet.</li>'}</ul>
</main>
<script>
document.querySelectorAll('tr[data-mk]').forEach(function (row) {{
  var mk = document.getElementById(row.dataset.mk);
  if (!mk) return;
  row.addEventListener('mouseenter', function () {{ mk.classList.add('hl'); }});
  row.addEventListener('mouseleave', function () {{ mk.classList.remove('hl'); }});
  row.addEventListener('click', function () {{ mk.scrollIntoView({{block: 'center', inline: 'center'}}); }});
}});
</script>
</body></html>
'''
