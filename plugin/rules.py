"""KiCad custom design rules (.kicad_dru): parser and evaluator.

Only the subset needed to derive pad-to-pad isolation requirements for
placement (and for the Specctra export) is supported:

* constraints: ``clearance``, ``creepage`` and ``physical_clearance``
  (value taken from ``min``; ``(severity ignore)`` disables the rule);
* conditions built from ``A.``/``B.`` properties and functions:
  ``NetClass``, ``NetName``, ``Type``, ``Pad_Type``, ``Layer``,
  ``hasNetclass()``, ``hasExactNetclass()``, ``hasComponentClass()``,
  ``memberOfSheet()``, ``memberOfSheetOrChildren()``, ``memberOfFootprint()``,
  ``memberOfGroup()``, ``existsOnLayer()``, ``isPlated()``,
  combined with ``!``, ``&&``, ``||``, ``==``, ``!=`` and parentheses.

Rules using anything else are reported as unsupported and ignored, never
guessed. Semantics follow KiCad's DRC engine:

* the last matching rule in the file wins for a given constraint;
* a rule matches a pair if its condition holds for (A, B) or for (B, A);
* string ``==`` is case-insensitive; a literal on the right-hand side may
  use ``*``/``?`` wildcards; a property the item does not have (e.g.
  ``Pad_Type`` of a track) makes both ``==`` and ``!=`` false;
* ``A.NetClass == 'X'`` is true when X is one of the item's net classes;
* when no custom clearance rule matches, the clearance is the larger of the
  two net-class clearances (and never below the board minimum);
* creepage is a property of two *nets*: KiCad evaluates creepage rules with
  a stand-in track of each net on every copper layer, so ``A.Type`` is
  'Track' and footprint functions (``memberOfFootprint()``, sheets, component
  classes) are false there. Clearance rules see the real items.

All distances are in nanometres (KiCad internal units).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

_UNIT_NM = {
    'mm': 1_000_000.0,
    'cm': 10_000_000.0,
    'um': 1_000.0,
    'nm': 1.0,
    'mil': 25_400.0,
    'mils': 25_400.0,
    'thou': 25_400.0,
    'in': 25_400_000.0,
    'inch': 25_400_000.0,
}

_VALUE_RE = re.compile(r'^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z]*)\s*$')


def parse_length(text: str) -> int:
    """Parse '6mm', '0.5 mm', '20mil', '0.1in' into nanometres (unitless = mm)."""
    m = _VALUE_RE.match(text)
    if not m:
        raise ValueError(f"invalid length '{text}'")
    value = float(m.group(1))
    unit = m.group(2).lower() or 'mm'
    if unit not in _UNIT_NM:
        raise ValueError(f"unknown unit '{unit}' in '{text}'")
    return int(round(value * _UNIT_NM[unit]))


# ---------------------------------------------------------------------------
# S-expression reader
# ---------------------------------------------------------------------------

class _Str(str):
    """A quoted string token (distinguished from bare atoms)."""


def _tokenize_sexpr(text: str) -> List[Any]:
    tokens: List[Any] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\r\n':
            i += 1
        elif c == '#':
            # KiCad accepts '#' line comments in .kicad_dru files.
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif c in '()':
            tokens.append(c)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            tokens.append(_Str(''.join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse_sexpr(text: str) -> List[Any]:
    """Parse text into nested Python lists; strings stay str."""
    tokens = _tokenize_sexpr(text)
    pos = 0

    def parse_list() -> List[Any]:
        nonlocal pos
        out: List[Any] = []
        while pos < len(tokens):
            tok = tokens[pos]
            pos += 1
            if tok == '(' and not isinstance(tok, _Str):
                out.append(parse_list())
            elif tok == ')' and not isinstance(tok, _Str):
                return out
            else:
                out.append(tok)
        return out

    return parse_list()


# ---------------------------------------------------------------------------
# Items seen by the rule evaluator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PadInfo:
    """What the rule evaluator knows about one copper item: a pad, or a
    track / via of a net (``kind``)."""
    net_name: str = ''
    netclasses: Tuple[str, ...] = ('Default',)   # constituents, e.g. ('HV', 'Default')
    nc_clearance: int = 0                        # effective net-class clearance (nm)
    fp_ref: str = ''
    component_classes: Tuple[str, ...] = ()
    sheet: str = ''
    groups: Tuple[str, ...] = ()
    pad_type: str = 'smd'                         # smd | tht | npth | conn (pads only)
    layers: Tuple[str, ...] = ('F.Cu',)           # copper layers; THT / vias -> ('*.Cu',)
    kind: str = 'pad'                             # pad | track | via
    fp_lib_id: str = ''                           # e.g. 'Package_TO_SOT_THT:TO-220-3_Vertical'

    @property
    def in_footprint(self) -> bool:
        return self.kind == 'pad'

    def on_layer(self, layer: str) -> bool:
        if '*.Cu' in self.layers:
            return layer.endswith('.Cu') or layer in ('*.Cu', '*')
        return any(_wild_eq(layer, l) for l in self.layers)


ItemInfo = PadInfo


def track_item(net_name: str, netclasses: Tuple[str, ...], nc_clearance: int,
               layer: str = 'F.Cu') -> PadInfo:
    """A track of a net on one copper layer."""
    return PadInfo(net_name=net_name, netclasses=netclasses, nc_clearance=nc_clearance,
                   pad_type='', layers=(layer,), kind='track')


def via_item(net_name: str, netclasses: Tuple[str, ...], nc_clearance: int) -> PadInfo:
    """A through via of a net."""
    return PadInfo(net_name=net_name, netclasses=netclasses, nc_clearance=nc_clearance,
                   pad_type='', layers=('*.Cu',), kind='via')


def net_view(item: PadInfo, layer: str) -> PadInfo:
    """How KiCad's creepage test sees an item: a track of its net on `layer`."""
    return track_item(item.net_name, item.netclasses, item.nc_clearance, layer)


_PAD_TYPE_LABEL = {
    'tht': 'Through-hole',
    'smd': 'SMD',
    'conn': 'Edge connector',
    'npth': 'NPTH, mechanical',
}


_WILD_CACHE: Dict[Tuple[str, bool], Any] = {}


def _wx_match(text: str, pattern: str, case_sensitive: bool = True) -> bool:
    """wxString::Matches / WildCompareString: whole-string match where only
    '*' and '?' are special."""
    key = (pattern, case_sensitive)
    rx = _WILD_CACHE.get(key)
    if rx is None:
        body = ''.join('.*' if ch == '*' else '.' if ch == '?' else re.escape(ch) for ch in pattern)
        rx = re.compile(body + r'\Z', re.DOTALL | (0 if case_sensitive else re.IGNORECASE))
        _WILD_CACHE[key] = rx
    return rx.match(text) is not None


def _has_wild(s: str) -> bool:
    return '*' in s or '?' in s


def _wild_eq(value: str, pattern: str) -> bool:
    """Case-insensitive equality, wildcards in the pattern."""
    if _has_wild(pattern):
        return _wx_match(value, pattern, case_sensitive=False)
    return value.lower() == pattern.lower()


def _strip_slash(s: str) -> str:
    return s[:-1] if s.endswith('/') else s


# ---------------------------------------------------------------------------
# Condition expressions
# ---------------------------------------------------------------------------

class UnsupportedRule(Exception):
    """Raised when a condition uses something the evaluator does not model."""


_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<op>\|\||&&|==|!=|<=|>=|[!<>(),.])
    | '(?P<str>(?:[^'\\]|\\.)*)'
    | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[a-zA-Z]+)?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.VERBOSE)


def _tokenize_expr(src: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    pos = 0
    src = src.strip()
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            raise UnsupportedRule(f"cannot parse condition near '{src[pos:pos + 20]}'")
        pos = m.end()
        for kind in ('op', 'str', 'num', 'ident'):
            v = m.group(kind)
            if v is not None:
                out.append((kind, v))
                break
        # trailing whitespace
        while pos < len(src) and src[pos].isspace():
            pos += 1
    return out


class _Lit(str):
    """A string literal written in the condition."""


class _Undefined:
    """Value of a property the item does not have (e.g. Pad_Type of a track)."""

    def __repr__(self) -> str:
        return 'undefined'


_UNDEF = _Undefined()


def _str_eq(x: str, y: str) -> bool:
    """KiCad VALUE::EqualTo for strings: case-insensitive; wildcards only in
    a literal on the right-hand side."""
    if isinstance(y, _Lit) and _has_wild(y):
        return _wx_match(x, y, case_sensitive=False)
    return x.lower() == y.lower()


class _NetClassValue:
    """Value of A.NetClass: compares equal to any of its constituent names
    (and to the full composite name, e.g. 'HV,Default')."""

    def __init__(self, names: Tuple[str, ...]):
        self.names = names

    @property
    def full(self) -> str:
        return ','.join(self.names)

    def equals(self, other: Any) -> bool:
        if isinstance(other, _NetClassValue):
            return set(self.names) == set(other.names)
        if isinstance(other, str):
            return any(_str_eq(n, other) for n in self.names) or _str_eq(self.full, other)
        return False


Evaluator = Callable[[PadInfo, PadInfo], Any]

_SIMPLE_PROPS = {'NetClass', 'NetName', 'Type', 'Pad_Type', 'Layer'}
_FUNCS_1ARG = {
    'hasNetclass', 'hasExactNetclass', 'hasComponentClass', 'memberOfSheet',
    'memberOfSheetOrChildren', 'memberOfFootprint', 'memberOfGroup', 'existsOnLayer',
}
_FUNCS_0ARG = {'isPlated'}

# Attributes of PadInfo each property/function depends on (for profiling).
# 'kind' and the net-class data are always kept.
_ATTR_OF = {
    'NetClass': ('netclasses',), 'hasNetclass': ('netclasses',),
    'hasExactNetclass': ('netclasses',),
    'NetName': ('net_name',),
    'Type': (),
    'Pad_Type': ('pad_type',), 'isPlated': ('pad_type',),
    'Layer': ('layers',), 'existsOnLayer': ('layers',),
    'hasComponentClass': ('component_classes',),
    'memberOfSheet': ('sheet',), 'memberOfSheetOrChildren': ('sheet',),
    'memberOfFootprint': ('fp_ref', 'fp_lib_id', 'component_classes'),
    'memberOfGroup': ('groups',),
}

_TYPE_LABEL = {'pad': 'Pad', 'track': 'Track', 'via': 'Via'}


class _Parser:
    def __init__(self, tokens: List[Tuple[str, str]]):
        self.toks = tokens
        self.i = 0
        self.attrs: set = set()

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, kind: Optional[str] = None, value: Optional[str] = None) -> Tuple[str, str]:
        tok = self.peek()
        if tok is None or (kind and tok[0] != kind) or (value and tok[1] != value):
            raise UnsupportedRule(f"unexpected token {tok!r} in condition")
        self.i += 1
        return tok

    def accept(self, value: str) -> bool:
        tok = self.peek()
        if tok and tok[0] == 'op' and tok[1] == value:
            self.i += 1
            return True
        return False

    # Operator precedence mirrors KiCad's grammar (libeval_compiler/grammar.lemon),
    # lowest first: '&&', then '||', then comparisons, then '!'.  Note that in
    # KiCad '&&' binds LESS tightly than '||'.

    def parse(self) -> Evaluator:
        ev = self.parse_and()
        if self.peek() is not None:
            raise UnsupportedRule(f"unexpected token {self.peek()!r} in condition")
        return ev

    def parse_and(self) -> Evaluator:
        left = self.parse_or()
        while self.accept('&&'):
            right = self.parse_or()
            left = (lambda l, r: lambda a, b: _truth(l(a, b)) and _truth(r(a, b)))(left, right)
        return left

    def parse_or(self) -> Evaluator:
        left = self.parse_cmp()
        while self.accept('||'):
            right = self.parse_cmp()
            left = (lambda l, r: lambda a, b: _truth(l(a, b)) or _truth(r(a, b)))(left, right)
        return left

    def parse_cmp(self) -> Evaluator:
        left = self.parse_unary()
        tok = self.peek()
        if tok and tok[0] == 'op' and tok[1] in ('==', '!=', '<', '<=', '>', '>='):
            op = tok[1]
            self.i += 1
            right = self.parse_unary()
            if op not in ('==', '!='):
                raise UnsupportedRule(f"numeric comparison '{op}' is not supported")
            if op == '==':
                return lambda a, b: _equals(left(a, b), right(a, b)) is True
            return lambda a, b: _equals(left(a, b), right(a, b)) is False
        return left

    def parse_unary(self) -> Evaluator:
        if self.accept('!'):
            inner = self.parse_unary()
            return lambda a, b: not _truth(inner(a, b))
        return self.parse_primary()

    def parse_primary(self) -> Evaluator:
        tok = self.peek()
        if tok is None:
            raise UnsupportedRule("condition ends unexpectedly")
        kind, val = tok
        if kind == 'op' and val == '(':
            self.i += 1
            ev = self.parse_and()
            self.take('op', ')')
            return ev
        if kind == 'str':
            self.i += 1
            s = _Lit(val.replace("\\'", "'"))
            return lambda a, b: s
        if kind == 'num':
            raise UnsupportedRule("numeric values in conditions are not supported")
        if kind == 'ident':
            self.i += 1
            if val not in ('A', 'B'):
                raise UnsupportedRule(f"unknown identifier '{val}'")
            which = val
            self.take('op', '.')
            name = self.take('ident')[1]
            if self.accept('('):
                args: List[str] = []
                if not self.accept(')'):
                    while True:
                        t = self.take()
                        if t[0] != 'str':
                            raise UnsupportedRule(f"{name}() expects text arguments")
                        args.append(t[1])
                        if self.accept(')'):
                            break
                        self.take('op', ',')
                return self._func(which, name, args)
            return self._prop(which, name)
        raise UnsupportedRule(f"unexpected token {tok!r} in condition")

    def _note(self, name: str) -> None:
        self.attrs.update(_ATTR_OF.get(name, ()))

    def _prop(self, which: str, name: str) -> Evaluator:
        if name not in _SIMPLE_PROPS:
            raise UnsupportedRule(f"property '{name}' is not supported")
        self._note(name)
        pick = (lambda a, b: a) if which == 'A' else (lambda a, b: b)
        if name == 'NetClass':
            return lambda a, b: _NetClassValue(pick(a, b).netclasses)
        if name == 'NetName':
            return lambda a, b: pick(a, b).net_name
        if name == 'Type':
            return lambda a, b: _TYPE_LABEL.get(pick(a, b).kind, 'Pad')
        if name == 'Pad_Type':
            return lambda a, b: (_PAD_TYPE_LABEL.get(pick(a, b).pad_type, 'SMD')
                                 if pick(a, b).kind == 'pad' else _UNDEF)
        if name == 'Layer':
            return lambda a, b: _LayerValue(pick(a, b))
        raise UnsupportedRule(name)

    def _func(self, which: str, name: str, args: List[str]) -> Evaluator:
        if name in _FUNCS_0ARG:
            if args:
                raise UnsupportedRule(f"{name}() takes no argument")
        elif name in _FUNCS_1ARG:
            if len(args) != 1:
                raise UnsupportedRule(f"{name}() takes one argument")
        else:
            raise UnsupportedRule(f"function '{name}()' is not supported")
        self._note(name)
        pick = (lambda a, b: a) if which == 'A' else (lambda a, b: b)
        arg = args[0] if args else ''
        # Footprint-related functions are false for tracks and vias (no
        # parent footprint), as in KiCad's pcbexpr_functions.cpp.
        if name == 'hasNetclass':
            return lambda a, b: arg in pick(a, b).netclasses
        if name == 'hasExactNetclass':
            return lambda a, b: ','.join(pick(a, b).netclasses) == arg
        if name == 'hasComponentClass':
            return lambda a, b: (pick(a, b).in_footprint
                                 and arg in pick(a, b).component_classes)
        if name == 'memberOfSheet':
            return lambda a, b: pick(a, b).in_footprint and _member_of_sheet(pick(a, b).sheet, arg)
        if name == 'memberOfSheetOrChildren':
            return lambda a, b: (pick(a, b).in_footprint
                                 and _member_of_sheet_or_children(pick(a, b).sheet, arg))
        if name == 'memberOfFootprint':
            return lambda a, b: pick(a, b).in_footprint and _footprint_selector(pick(a, b), arg)
        if name == 'memberOfGroup':
            return lambda a, b: any(_wx_match(g, arg) for g in pick(a, b).groups)
        if name == 'existsOnLayer':
            return lambda a, b: pick(a, b).on_layer(arg)
        if name == 'isPlated':
            return lambda a, b: ((pick(a, b).kind == 'pad' and pick(a, b).pad_type == 'tht')
                                 or pick(a, b).kind == 'via')
        raise UnsupportedRule(name)


class _LayerValue:
    def __init__(self, pad: PadInfo):
        self.pad = pad

    def equals(self, other: Any) -> bool:
        return isinstance(other, str) and self.pad.on_layer(other)


def _member_of_sheet(sheet: str, ref: str) -> bool:
    sheet, ref = _strip_slash(sheet), _strip_slash(ref)
    return _wx_match(sheet, ref) or (ref in ('/', '') and sheet == '')


def _member_of_sheet_or_children(sheet: str, ref: str) -> bool:
    sheet, ref = _strip_slash(sheet), _strip_slash(ref)
    sheet_path = sheet.split('/')
    ref_path = ref.split('/')
    if len(ref_path) > len(sheet_path):
        return False
    if ref in ('/', '') and sheet == '':
        return True
    return all(_wx_match(s, r) for s, r in zip(sheet_path, ref_path))


def _footprint_selector(item: PadInfo, sel: str) -> bool:
    """KiCad testFootprintSelector(): ${Class:X}, a reference pattern, or a
    library id pattern ('lib:name')."""
    if not sel:
        return False
    if sel.startswith('$') and sel.endswith('}') and sel.upper().startswith('${CLASS:'):
        return sel[8:-1] in item.component_classes
    if _wx_match(item.fp_ref, sel):
        return True
    return ':' in sel and _wx_match(item.fp_lib_id, sel)


def _truth(v: Any) -> bool:
    if isinstance(v, (_NetClassValue, _LayerValue)):
        return True
    if isinstance(v, _Undefined):
        return False
    return bool(v)


def _equals(x: Any, y: Any) -> Optional[bool]:
    """== between two values; None when either is undefined (then both
    '==' and '!=' are false, as in KiCad)."""
    if isinstance(x, _Undefined) or isinstance(y, _Undefined):
        return None
    if isinstance(x, (_NetClassValue, _LayerValue)):
        return x.equals(y)
    if isinstance(y, _NetClassValue):
        # 'HV' == A.NetClass: KiCad compares with the full name only.
        return isinstance(x, str) and x.lower() == y.full.lower()
    if isinstance(y, _LayerValue):
        return y.equals(x)
    if isinstance(x, str) and isinstance(y, str):
        return _str_eq(x, y)
    return x == y


def compile_condition(src: str) -> Tuple[Evaluator, FrozenSet[str]]:
    """Compile a KiCad rule condition. Returns (evaluator, attributes used)."""
    src = (src or '').strip()
    if not src:
        return (lambda a, b: True), frozenset()
    parser = _Parser(_tokenize_expr(src))
    ev = parser.parse()
    return ev, frozenset(parser.attrs)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

ISOLATION_CONSTRAINTS = ('clearance', 'creepage', 'physical_clearance')


@dataclass
class Rule:
    name: str
    condition: str
    constraints: Dict[str, Optional[int]]      # type -> min (nm); None if severity ignore
    evaluator: Evaluator = field(repr=False, default=lambda a, b: True)
    attrs: FrozenSet[str] = frozenset()

    def matches(self, a: PadInfo, b: PadInfo) -> bool:
        return _truth(self.evaluator(a, b)) or _truth(self.evaluator(b, a))


DEFAULT_COPPER_LAYERS = ('F.Cu', 'B.Cu')


def copper_layer_names(count: int) -> Tuple[str, ...]:
    """Canonical names of a board's copper layers (F.Cu, In1.Cu.., B.Cu)."""
    if count <= 1:
        return ('F.Cu',)
    return ('F.Cu',) + tuple(f'In{i}.Cu' for i in range(1, count - 1)) + ('B.Cu',)


@dataclass
class RuleSet:
    rules: List[Rule] = field(default_factory=list)
    unsupported: List[Tuple[str, str]] = field(default_factory=list)   # (rule name, reason)
    other_rules: int = 0          # rules without isolation constraints (ignored)
    board_min_clearance: int = 0
    source: str = ''
    copper_layers: Tuple[str, ...] = DEFAULT_COPPER_LAYERS
    _creepage_cache: Dict[Any, int] = field(default_factory=dict, repr=False, compare=False)

    @property
    def attrs(self) -> FrozenSet[str]:
        out: set = set()
        for r in self.rules:
            out |= r.attrs
        return frozenset(out)

    @property
    def has_creepage(self) -> bool:
        return any('creepage' in r.constraints for r in self.rules)

    def _lookup(self, a: PadInfo, b: PadInfo, types: Sequence[str]) -> Dict[str, Optional[int]]:
        """Last matching rule per constraint type (None = severity ignore)."""
        found: Dict[str, Optional[int]] = {}
        for rule in reversed(self.rules):
            pending = [c for c in rule.constraints if c in types and c not in found]
            if not pending:
                continue
            if rule.matches(a, b):
                for c in pending:
                    found[c] = rule.constraints[c]
                if len(found) == len(types):
                    break
        return found

    def clearance(self, a: PadInfo, b: PadInfo) -> int:
        """Clearance (and physical clearance) KiCad applies between two items
        of different nets."""
        found = self._lookup(a, b, ('clearance', 'physical_clearance'))
        if 'clearance' in found:
            clearance = found['clearance'] or 0
        else:
            clearance = max(a.nc_clearance, b.nc_clearance)
        clearance = max(clearance, self.board_min_clearance)
        return max(clearance, found.get('physical_clearance') or 0)

    def creepage(self, a: PadInfo, b: PadInfo) -> int:
        """Creepage between the nets of a and b: KiCad evaluates it with a
        stand-in track of each net, on every copper layer; the largest wins."""
        if not self.has_creepage:
            return 0
        key = ((a.net_name, a.netclasses, a.nc_clearance), (b.net_name, b.netclasses, b.nc_clearance))
        cached = self._creepage_cache.get(key)
        if cached is not None:
            return cached
        best = 0
        for layer in self.copper_layers or DEFAULT_COPPER_LAYERS:
            found = self._lookup(net_view(a, layer), net_view(b, layer), ('creepage',))
            best = max(best, found.get('creepage') or 0)
        self._creepage_cache[key] = best
        return best

    def requirement(self, a: PadInfo, b: PadInfo) -> int:
        """Minimum copper-to-copper distance (nm) required between items a and
        b (assumed on different nets): max(clearance, physical, creepage).
        A straight-line gap of at least the creepage distance always
        satisfies KiCad's creepage check (the path can only be longer).
        A non-plated hole has no copper: only creepage applies to it (KiCad
        counts it as an item without net)."""
        creep = self.creepage(a, b)
        if (a.kind == 'pad' and a.pad_type == 'npth') or (b.kind == 'pad' and b.pad_type == 'npth'):
            return creep
        return max(self.clearance(a, b), creep)

    def summary(self) -> Dict[str, int]:
        counts = {c: 0 for c in ISOLATION_CONSTRAINTS}
        for r in self.rules:
            for c in r.constraints:
                counts[c] += 1
        return counts


def parse_rules(text: str, board_min_clearance: int = 0, source: str = '',
                copper_layers: Sequence[str] = DEFAULT_COPPER_LAYERS) -> RuleSet:
    """Parse the content of a .kicad_dru file."""
    rs = RuleSet(board_min_clearance=board_min_clearance, source=source,
                 copper_layers=tuple(copper_layers))
    try:
        tree = parse_sexpr(text)
    except Exception as e:  # pragma: no cover - defensive
        rs.unsupported.append(('(file)', f'unreadable: {e}'))
        return rs
    for node in tree:
        if not isinstance(node, list) or not node or node[0] != 'rule':
            continue
        name = str(node[1]) if len(node) > 1 else '?'
        condition = ''
        severity = ''
        constraints: Dict[str, Optional[int]] = {}
        try:
            for item in node[2:]:
                if not isinstance(item, list) or not item:
                    continue
                head = item[0]
                if head == 'condition' and len(item) > 1:
                    condition = str(item[1])
                elif head == 'severity' and len(item) > 1:
                    severity = str(item[1]).lower()
                elif head == 'constraint' and len(item) > 1:
                    ctype = str(item[1])
                    if ctype not in ISOLATION_CONSTRAINTS:
                        continue
                    vmin = None
                    for sub in item[2:]:
                        if isinstance(sub, list) and len(sub) > 1 and sub[0] == 'min':
                            vmin = parse_length(str(sub[1]))
                    if vmin is not None:
                        constraints[ctype] = vmin
        except ValueError as e:
            rs.unsupported.append((name, str(e)))
            continue
        if not constraints:
            rs.other_rules += 1
            continue
        if severity == 'ignore':
            constraints = {c: None for c in constraints}
        try:
            ev, attrs = compile_condition(condition)
        except UnsupportedRule as e:
            rs.unsupported.append((name, str(e)))
            continue
        rs.rules.append(Rule(name=name, condition=condition, constraints=constraints,
                             evaluator=ev, attrs=attrs))
    return rs


def load_rules_file(path: str, board_min_clearance: int = 0,
                    copper_layers: Sequence[str] = DEFAULT_COPPER_LAYERS) -> RuleSet:
    """Read a .kicad_dru file; a missing file gives an empty rule set."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return RuleSet(board_min_clearance=board_min_clearance, source='',
                       copper_layers=tuple(copper_layers))
    return parse_rules(text, board_min_clearance=board_min_clearance, source=path,
                       copper_layers=copper_layers)
