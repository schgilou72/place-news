"""Text for the routing dialogs (no KiCad / wx dependency)."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from . import specctra


def _mm(nm: float) -> str:
    return f'{nm / 1e6:.2f} mm'


def class_label(dsn_class: str) -> str:
    """'kicad_default' -> 'Default', 'HV,Default' -> 'HV'."""
    if dsn_class == specctra.DSN_DEFAULT_CLASS:
        return 'Default'
    parts = [p for p in dsn_class.split(',') if p]
    if len(parts) > 1 and parts[-1] == 'Default':
        parts = parts[:-1]
    return ','.join(parts) or dsn_class


def describe_class_rules(report: specctra.ClassRulesReport) -> List[str]:
    lines = []
    for (a, b), v in sorted(report.pairs.items(), key=lambda kv: (-kv[1], kv[0])):
        la, lb = sorted((class_label(a), class_label(b)))
        lines.append(f'{la} ↔ {lb}: {_mm(v)}')
    for c, v in sorted(report.classes.items()):
        lines.append(f'{class_label(c)} between its own nets: {_mm(v)} '
                     f'(net class: {_mm(report.original.get(c, 0))})')
    return lines


def describe_keepouts(stats: Dict[str, int]) -> List[str]:
    lines = []
    if stats.get('removed'):
        lines.append(f"{stats['removed']} rule area(s) that only restrict footprints/pads "
                     f"or place parts no longer block routing")
    if stats.get('wire_only'):
        lines.append(f"{stats['wire_only']} rule area(s) keep out tracks only (vias allowed)")
    if stats.get('via_only'):
        lines.append(f"{stats['via_only']} rule area(s) keep out vias only (tracks allowed)")
    if stats.get('kept'):
        lines.append(f"{stats['kept']} rule area(s) keep out tracks and vias")
    if stats.get('unmatched'):
        lines.append(f"{stats['unmatched']} other keep-out(s) left as KiCad exported them")
    return lines


_PASS_RE = re.compile(r'Auto-routing pass #(\d+).*?\((\d+) unrouted and (\d+) violations\)')
_LOG_PREFIX_RE = re.compile(r'^\S+ \S+ +[A-Z]+ +(\[[^\]]*\] )?')


def progress_text(line: str) -> str:
    """Short status for the progress dialog from a Freerouting log line."""
    m = _PASS_RE.search(line)
    if m:
        return f'Routing pass {m.group(1)}: {m.group(2)} unrouted, {m.group(3)} violations'
    if 'Optimiz' in line:
        return 'Optimizing the routes...'
    if 'Fanout' in line:
        return 'Fanning out SMD pads...'
    if 'Loading board' in line:
        return 'Loading the board...'
    if 'Saving' in line:
        return 'Writing the result...'
    text = _LOG_PREFIX_RE.sub('', line).strip()
    return (text[:77] + '...') if len(text) > 80 else (text or 'Routing...')


def summarize_result(result: specctra.RouteResult, export: Optional[specctra.ExportReport],
                     drc: Optional[specctra.DrcSummary], elapsed: float,
                     drc_requested: bool, zones_refilled: bool = False) -> List[str]:
    lines = [f'Routing finished in {elapsed:.0f} s.']
    if result.unrouted is not None:
        lines.append(f'  Connections left unrouted: {result.unrouted}')
        lines.append(f'  Freerouting clearance violations: {result.violations}')
    if zones_refilled:
        lines.append('  Zones refilled.')
    if export is not None:
        rules = describe_class_rules(export.classes)
        if export.rules is not None:
            lines.append('')
            if rules:
                lines.append('Isolation distances given to the router:')
                lines += ['  ' + r for r in rules]
            else:
                lines.append('Isolation: net-class clearances only (no custom rule '
                             'needs more between classes).')
            for name, reason in export.rules.unsupported[:5]:
                lines.append(f"  Rule '{name}' ignored: {reason}")
        ko = describe_keepouts(export.keepouts)
        if ko:
            lines.append('')
            lines.append('Rule areas:')
            lines += ['  ' + k for k in ko]
    lines.append('')
    if drc is not None and not drc.error:
        between = sum(drc.between_parts.values())
        inside = sum(drc.inside_parts.values())
        area = sum(drc.rule_area.values())
        lines.append("KiCad DRC (on a copy of the board, with the project settings saved on disk):")
        lines.append(f'  Clearance / creepage violations: {between}')
        if inside:
            lines.append(f'  ... inside footprints (the part itself): {inside}')
        if area:
            lines.append(f'  ... against rule-area outlines (KiCad counts them as copper '
                         f'for creepage): {area}')
        lines.append(f'  Unconnected items: {drc.unconnected}')
    elif drc is not None:
        lines.append(f"KiCad's DRC could not be run: {drc.error}.")
        lines.append('Run Inspect > Design Rules Checker to check the result.')
    elif drc_requested:
        lines.append("KiCad's DRC could not be run. Run Inspect > Design Rules Checker.")
    else:
        lines.append('Run Inspect > Design Rules Checker to check the result.')
    lines.append('Edit > Undo reverts the routing.')
    return lines
