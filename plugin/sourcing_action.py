"""place-news — Sourcing: stock, prices, minimum orders, lifecycle and
replacements of every BOM line at DigiKey, Mouser, Farnell, TME and LCSC;
the chosen parts written into the components' fields; a BOM and one order
list per distributor (designators as customer reference)."""
from __future__ import annotations

import json
import os
import tempfile
import textwrap
import time
import webbrowser
from typing import Any, Dict, List, Optional, Sequence

import pcbnew
import wx

from .bom import DISTRIBUTORS, board_parts, group_parts, write_fields
from .distributors import DigiKey, Distributor, Farnell, LCSC, Mouser, TME, Cache, Offer, ecb_rates
from .sourcing import (LineResult, Settings, Sourcer, build_report, fields_for, finalize, order_csv,
                       order_lines, totals, write_bom_csv)

# Parameters written only where the designer left the field empty
PARAM_FIELDS = ('Voltage', 'Current', 'Power', 'Tolerance', 'Dielectric', 'Safety class')

KEY_PAGES = {
    'DigiKey': 'https://developer.digikey.com  (My Apps > Create App > Product Information v4, '
               'client credentials)',
    'Mouser': 'https://www.mouser.com/api-hub/  (Search API key)',
    'Farnell': 'https://partner.element14.com  (My API Keys > Product Search API, Basic)',
    'TME': 'https://developers.tme.eu  (new application: token and secret, API v2)',
    'LCSC': 'no key: public JLCPCB / LCSC catalogue',
}
DEFAULTS: Dict[str, Any] = {
    'digikey_id': '', 'digikey_secret': '', 'mouser_key': '', 'farnell_key': '',
    'farnell_store': 'fr.farnell.com', 'tme_token': '', 'tme_secret': '', 'lcsc': True,
    'order': list(DISTRIBUTORS), 'enabled': list(DISTRIBUTORS), 'strategy': 'priority',
    'boards': 1, 'spare': 10, 'search_without_mpn': True, 'alternates': True, 'r_tolerance': 1.0,
}


def settings_dir() -> str:
    try:
        base = str(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath())
        if base:
            return os.path.join(base, 'place-news')
    except Exception:
        pass
    return os.path.join(os.path.expanduser('~'), '.place-news')


def load_settings() -> Dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        with open(os.path.join(settings_dir(), 'sourcing.json'), 'r', encoding='utf-8') as f:
            data.update(json.load(f))
    except (OSError, ValueError):
        pass
    return data


def save_settings(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(settings_dir(), exist_ok=True)
        path = os.path.join(settings_dir(), 'sourcing.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1)
        try:
            os.chmod(path, 0o600)       # the API keys stay readable by you only
        except OSError:
            pass
    except OSError:
        pass


def make_distributors(cfg: Dict[str, Any], cache: Cache, rates: Dict[str, float]) -> List[Distributor]:
    """rates: EUR per unit of each currency (ECB), for prices not in euros."""
    enabled = set(cfg.get('enabled', DISTRIBUTORS))
    out: List[Distributor] = []
    if 'DigiKey' in enabled:
        out.append(DigiKey(cfg.get('digikey_id', ''), cfg.get('digikey_secret', ''), cache=cache))
    if 'Mouser' in enabled:
        out.append(Mouser(cfg.get('mouser_key', ''), cache=cache))
    if 'Farnell' in enabled:
        out.append(Farnell(cfg.get('farnell_key', ''), store=cfg.get('farnell_store') or 'fr.farnell.com',
                           cache=cache))
    if 'TME' in enabled:
        out.append(TME(cfg.get('tme_token', ''), cfg.get('tme_secret', ''), cache=cache))
    if 'LCSC' in enabled:
        out.append(LCSC(usd_to_eur=rates.get('USD', 0.92), cache=cache,
                        enabled=bool(cfg.get('lcsc', True))))
    for d in out:
        d.rates = dict(rates)
    return out


def board_base(board) -> str:
    fn = str(board.GetFileName() or '')
    if fn:
        return os.path.splitext(fn)[0]
    return os.path.join(tempfile.gettempdir(), 'board')


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SourcingDialog(wx.Dialog):
    KEY_FIELDS = (('DigiKey', (('digikey_id', 'Client ID'), ('digikey_secret', 'Client secret'))),
                  ('Mouser', (('mouser_key', 'Search API key'),)),
                  ('Farnell', (('farnell_key', 'API key'), ('farnell_store', 'Store'))),
                  ('TME', (('tme_token', 'Token'), ('tme_secret', 'Secret'))),
                  ('LCSC', ()))

    def __init__(self, parent, cfg: Dict[str, Any], n_lines: int, n_parts: int):
        super().__init__(parent, title='place-news — Sourcing',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.cfg = dict(cfg)
        grey = wx.Colour(90, 90, 90)
        main = wx.BoxSizer(wx.VERTICAL)
        head = wx.StaticText(self, label=f'{n_parts} parts in {n_lines} BOM lines.')
        main.Add(head, 0, wx.ALL, 10)

        # --- keys ---
        box = wx.StaticBoxSizer(wx.StaticBox(self, label='Distributors (your own free API keys)'),
                                wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=4, vgap=4, hgap=6)
        grid.AddGrowableCol(2, 1)
        self.ctrls: Dict[str, wx.TextCtrl] = {}
        self.status: Dict[str, wx.StaticText] = {}
        for dist, fields in self.KEY_FIELDS:
            label = dist + (' (beta)' if dist == 'TME' else '')
            first = True
            if not fields:
                self.chk_lcsc = wx.CheckBox(self, label='use (no key needed)')
                self.chk_lcsc.SetValue(bool(self.cfg.get('lcsc', True)))
                grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(wx.StaticText(self, label=''), 0)
                grid.Add(self.chk_lcsc, 0, wx.ALIGN_CENTER_VERTICAL)
                self._add_test(grid, dist)
                continue
            for key, name in fields:
                grid.Add(wx.StaticText(self, label=label if first else ''), 0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(wx.StaticText(self, label=name), 0, wx.ALIGN_CENTER_VERTICAL)
                style = wx.TE_PASSWORD if 'secret' in key else 0
                ctrl = wx.TextCtrl(self, value=str(self.cfg.get(key, '')), style=style)
                ctrl.SetMinSize(wx.Size(300, -1))
                ctrl.SetToolTip(KEY_PAGES[dist])
                self.ctrls[key] = ctrl
                grid.Add(ctrl, 1, wx.EXPAND)
                if first:
                    self._add_test(grid, dist)
                else:
                    grid.Add(wx.StaticText(self, label=''), 0)
                first = False
        box.Add(grid, 0, wx.ALL | wx.EXPAND, 6)
        where = wx.StaticText(self, label='\n'.join(f'{d}: {u}' for d, u in KEY_PAGES.items()))
        where.SetForegroundColour(grey)
        box.Add(where, 0, wx.ALL, 6)
        note = wx.StaticText(self, label='Keys are kept in your KiCad settings folder '
                                         '(place-news/sourcing.json), readable by you only.')
        note.SetForegroundColour(grey)
        box.Add(note, 0, wx.LEFT | wx.BOTTOM, 6)
        main.Add(box, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        # --- order ---
        obox = wx.StaticBoxSizer(wx.StaticBox(self, label='Order'), wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label='Boards:'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.boards = wx.SpinCtrl(self, min=1, max=100000, initial=int(self.cfg.get('boards', 1)))
        row.Add(self.boards, 0, wx.RIGHT, 14)
        row.Add(wx.StaticText(self, label='Spare parts (%):'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.spare = wx.SpinCtrl(self, min=0, max=200, initial=int(self.cfg.get('spare', 10)))
        row.Add(self.spare, 0)
        obox.Add(row, 0, wx.ALL, 6)
        self.rb_priority = wx.RadioButton(self, label='Buy each line from the first distributor of '
                                                      'this list that has the stock', style=wx.RB_GROUP)
        self.rb_cheap = wx.RadioButton(self, label='Buy each line where its total is the lowest')
        (self.rb_cheap if self.cfg.get('strategy') == 'cheapest' else self.rb_priority).SetValue(True)
        obox.Add(self.rb_priority, 0, wx.LEFT | wx.TOP, 6)
        obox.Add(self.rb_cheap, 0, wx.LEFT | wx.BOTTOM, 6)
        order = [d for d in self.cfg.get('order', DISTRIBUTORS) if d in DISTRIBUTORS]
        order += [d for d in DISTRIBUTORS if d not in order]
        enabled = set(self.cfg.get('enabled', DISTRIBUTORS))
        self._order_names = order
        self.rearrange = wx.RearrangeCtrl(self, wx.ID_ANY, wx.DefaultPosition, wx.Size(260, 130),
                                          [i if d in enabled else ~i for i, d in enumerate(order)], order)
        self.rearrange.SetToolTip('Tick the distributors to ask; their order is your preference.')
        obox.Add(self.rearrange, 0, wx.ALL, 6)
        main.Add(obox, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)

        # --- options ---
        self.chk_search = wx.CheckBox(self, label='Parts without MPN: search resistors, capacitors and '
                                                  'inductors by value and package')
        self.chk_search.SetValue(bool(self.cfg.get('search_without_mpn', True)))
        self.chk_alt = wx.CheckBox(self, label='Propose replacements for parts short or at the end '
                                               'of their life (same package, ratings kept)')
        self.chk_alt.SetValue(bool(self.cfg.get('alternates', True)))
        main.Add(self.chk_search, 0, wx.LEFT | wx.TOP, 12)
        main.Add(self.chk_alt, 0, wx.LEFT | wx.TOP, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label='Resistors without tolerance: at most'), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.r_tol = wx.SpinCtrlDouble(self, min=0.0, max=20.0, inc=0.1,
                                       initial=float(self.cfg.get('r_tolerance', 1.0)))
        row.Add(self.r_tol, 0, wx.RIGHT, 4)
        row.Add(wx.StaticText(self, label='%   (chip capacitors: X5R or better)'), 0,
                wx.ALIGN_CENTER_VERTICAL)
        main.Add(row, 0, wx.LEFT | wx.TOP, 12)

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK).SetLabel('Search')
        main.Add(btns, 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(main)
        self.CentreOnParent()

    def _add_test(self, grid, dist: str) -> None:
        cell = wx.BoxSizer(wx.HORIZONTAL)
        btn = wx.Button(self, label='Test', style=wx.BU_EXACTFIT)
        btn.Bind(wx.EVT_BUTTON, lambda e, d=dist: self._test(d))
        lbl = wx.StaticText(self, label='')
        self.status[dist] = lbl
        cell.Add(btn, 0, wx.RIGHT, 4)
        cell.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(cell, 0)

    def _test(self, dist: str) -> None:
        cfg = self.values
        cfg['enabled'] = [dist]
        rates, _day = ecb_rates()
        d = make_distributors(cfg, Cache(), rates)
        if not d or not d[0].configured():
            self.status[dist].SetLabel('no key')
            self.status[dist].SetForegroundColour(wx.Colour(200, 120, 0))
            return
        busy = wx.BusyInfo(f'Asking {dist}...')
        try:
            err = d[0].test()
        finally:
            del busy
        self.status[dist].SetLabel('✔ works' if not err else '✘')
        self.status[dist].SetForegroundColour(wx.Colour(34, 139, 34) if not err else wx.Colour(200, 40, 40))
        if err:
            wx.MessageBox(f'{dist}: {err}', 'place-news', wx.OK | wx.ICON_WARNING)
        self.Layout()

    @property
    def values(self) -> Dict[str, Any]:
        out = dict(self.cfg)
        for key, ctrl in self.ctrls.items():
            out[key] = ctrl.GetValue().strip()
        out['lcsc'] = self.chk_lcsc.GetValue()
        order_idx = self.rearrange.GetList().GetCurrentOrder()
        out['order'] = [self._order_names[i if i >= 0 else ~i] for i in order_idx]
        out['enabled'] = [self._order_names[i] for i in order_idx if i >= 0]
        out['strategy'] = 'cheapest' if self.rb_cheap.GetValue() else 'priority'
        out['boards'] = int(self.boards.GetValue())
        out['spare'] = int(self.spare.GetValue())
        out['search_without_mpn'] = self.chk_search.GetValue()
        out['alternates'] = self.chk_alt.GetValue()
        out['r_tolerance'] = float(self.r_tol.GetValue())
        return out


# ---------------------------------------------------------------------------
# Results dialog
# ---------------------------------------------------------------------------

def _fmt_money(v: Optional[float], digits: int = 4) -> str:
    return '—' if v is None else f'{v:.{digits}f}'


def _fmt_int(v: Optional[int]) -> str:
    return '—' if v is None else f'{v:,}'.replace(',', ' ')


class ResultsDialog(wx.Dialog):
    STATUS = {'ok': 'OK', 'short': 'short stock', 'risk': 'end of life / NRND',
              'proposed': 'proposed', 'not found': 'not found'}
    COLORS = {'ok': wx.Colour(34, 139, 34), 'short': wx.Colour(200, 120, 0),
              'risk': wx.Colour(200, 40, 40), 'proposed': wx.Colour(28, 126, 214),
              'not found': wx.Colour(120, 120, 120)}

    def __init__(self, parent, board, results: List[LineResult], settings: Settings, date: str,
                 rate_note: str, skipped: Sequence[str]):
        super().__init__(parent, title='place-news — Sourcing results',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self.board, self.results, self.s = board, results, settings
        self.date, self.rate_note, self.skipped = date, rate_note, list(skipped)
        self.base = board_base(board)
        self.written = 0
        main = wx.BoxSizer(wx.VERTICAL)
        self.head = wx.StaticText(self, label='')
        f = self.head.GetFont()
        f.SetPointSize(f.GetPointSize() + 2)
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        self.head.SetFont(f)
        main.Add(self.head, 0, wx.ALL, 10)

        self.lst = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for col, (title, width, fmt) in enumerate((
                ('Designators', 130, wx.LIST_FORMAT_LEFT), ('Value', 90, wx.LIST_FORMAT_LEFT),
                ('Package', 80, wx.LIST_FORMAT_LEFT), ('MPN', 160, wx.LIST_FORMAT_LEFT),
                ('Distributor', 110, wx.LIST_FORMAT_LEFT), ('To order', 65, wx.LIST_FORMAT_RIGHT),
                ('Unit €', 70, wx.LIST_FORMAT_RIGHT), ('Min. ×mult.', 80, wx.LIST_FORMAT_RIGHT),
                ('Total €', 70, wx.LIST_FORMAT_RIGHT), ('Stock', 80, wx.LIST_FORMAT_RIGHT),
                ('Status', 120, wx.LIST_FORMAT_LEFT))):
            self.lst.InsertColumn(col, title, fmt, width)
        self.lst.SetMinSize(wx.Size(1080, 260))
        main.Add(self.lst, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        main.Add(wx.StaticText(self, label='Choices for the selected line (double-click to use one):'),
                 0, wx.LEFT | wx.TOP, 10)
        self.choices = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for col, (title, width) in enumerate((('Kind', 90), ('Distributor', 80), ('Part number', 120),
                                              ('MPN', 160), ('Manufacturer', 120), ('Stock', 80),
                                              ('Unit €', 70), ('Min. ×mult.', 80), ('Total €', 70),
                                              ('Lifecycle', 80), ('Note', 220))):
            self.choices.InsertColumn(col, title, width=width)
        self.choices.SetMinSize(wx.Size(1080, 170))
        main.Add(self.choices, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
        self._choice_offers: List[Offer] = []

        self.info = wx.StaticText(self, label='')
        self.info.SetForegroundColour(wx.Colour(90, 90, 90))
        main.Add(self.info, 0, wx.ALL, 10)

        row = wx.BoxSizer(wx.HORIZONTAL)
        b_write = wx.Button(self, label='Write into the components')
        b_write.SetToolTip('MPN, manufacturer, part number at each distributor and key parameters, '
                           'as fields of the footprints. Then in the schematic: Tools > Update '
                           'Schematic from PCB, with "Other fields" ticked.')
        b_save = wx.Button(self, label='Save the BOM and the order lists')
        b_report = wx.Button(self, label='Open the report')
        row.Add(b_write, 0, wx.RIGHT, 8)
        row.Add(b_save, 0, wx.RIGHT, 8)
        row.Add(b_report, 0)
        main.Add(row, 0, wx.LEFT | wx.RIGHT, 10)
        close = self.CreateStdDialogButtonSizer(wx.CLOSE)
        main.Add(close, 0, wx.ALL | wx.EXPAND, 10)

        self.lst.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)
        self.choices.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_choose)
        b_write.Bind(wx.EVT_BUTTON, self._on_write)
        b_save.Bind(wx.EVT_BUTTON, self._on_save)
        b_report.Bind(wx.EVT_BUTTON, self._on_report)
        self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
        self._fill()
        self.SetSizerAndFit(main)
        self.CentreOnParent()

    # -- content ---------------------------------------------------------------
    def _fill(self) -> None:
        self.lst.DeleteAllItems()
        for r in self.results:
            o, order = r.chosen, r.order
            i = self.lst.InsertItem(self.lst.GetItemCount(), r.line.designators)
            cells = (r.line.value, r.line.package, r.mpn, f'{o.distributor} {o.sku}' if o else '—',
                     str(order[0]) if order else '—', _fmt_money(order[1]) if order else '—',
                     f'{o.moq}' + (f' ×{o.multiple}' if o and o.multiple > 1 else '') if o else '—',
                     _fmt_money(order[2], 2) if order else '—', _fmt_int(o.stock) if o else '—',
                     self.STATUS.get(r.status, r.status) + (f' ({r.lifecycle})' if r.status == 'risk' else '')
                     + (' — check package' if r.package_warning else ''))
            for c, text in enumerate(cells, start=1):
                self.lst.SetItem(i, c, text)
            self.lst.SetItemTextColour(i, self.COLORS['short'] if r.package_warning and r.status == 'ok'
                                       else self.COLORS.get(r.status, wx.BLACK))
        t = totals(self.results)
        grand = sum(t.values())
        extra = sum(r.extra_cost for r in self.results)
        self.head.SetLabel(f'{grand:.2f} € to order ({grand / max(1, self.s.boards):.2f} € per board, '
                           f'{extra:.2f} € due to minimum orders) — '
                           + ', '.join(f'{d} {v:.2f} €' for d, v in sorted(t.items())))
        counts = {k: sum(1 for r in self.results if r.status == k) for k in self.STATUS}
        n_pkg = sum(1 for r in self.results if r.package_warning)
        self.info.SetLabel(
            textwrap.fill(f'{counts["ok"]} OK, {counts["short"]} short, {counts["risk"]} at the end of '
                          f'their life, {counts["proposed"]} proposed (no MPN), {counts["not found"]} not '
                          f'found' + (f', {n_pkg} with a package that does not fit the footprint'
                                      if n_pkg else '')
                          + f'. Prices of {self.date}, excl. VAT and shipping. {self.rate_note}', 160)
            + (f'\n{self.written} field(s) written into the components: in the schematic, Tools > '
               f'Update Schematic from PCB, tick "Other fields".' if self.written else ''))

    def _on_select(self, evt) -> None:
        r = self.results[evt.GetIndex()]
        self.choices.DeleteAllItems()
        self._choice_offers = []
        rows = [('part', o) for o in r.offers] + [('proposal', o) for o in r.candidates] + \
               [('replacement', a.offer) for a in r.alternates]
        for kind, o in rows:
            c = o.cost(r.need)
            i = self.choices.InsertItem(self.choices.GetItemCount(), kind + (' ←' if o is r.chosen else ''))
            for col, text in enumerate((o.distributor, o.sku, o.mpn, o.manufacturer, _fmt_int(o.stock),
                                        _fmt_money(c[1]) if c else '—',
                                        f'{o.moq}' + (f' ×{o.multiple}' if o.multiple > 1 else ''),
                                        _fmt_money(c[2], 2) if c else '—', o.lifecycle,
                                        o.note or o.description[:60]), start=1):
                self.choices.SetItem(i, col, text)
            self._choice_offers.append(o)

    def _on_choose(self, evt) -> None:
        sel = self.lst.GetFirstSelected()
        if sel < 0:
            return
        r = self.results[sel]
        o = self._choice_offers[evt.GetIndex()]
        r.chosen = o
        if any(a.offer is o for a in r.alternates):
            r.offers = [o]                              # the replacement is now the part
            r.notes.append(f'replaced by {o.mpn} ({o.manufacturer})')
        finalize(self.results)                          # shared orders, statuses
        self._fill()
        self.lst.Select(sel)

    # -- actions ---------------------------------------------------------------
    def _on_write(self, _evt) -> None:
        n = 0
        for r in self.results:
            values = fields_for(r)
            if values:
                n += write_fields(self.board, r.line.refs, values, fill_only=PARAM_FIELDS)
        self.written += n
        self._fill()
        wx.MessageBox(f'{n} field(s) written into the components.\n\nTo bring them into the schematic: '
                      f'Tools > Update Schematic from PCB, tick "Other fields".\nEdit > Undo removes '
                      f'them from the board.', 'place-news', wx.OK | wx.ICON_INFORMATION)

    def _paths(self) -> Dict[str, str]:
        out = {'bom': f'{self.base}-bom.csv', 'report': f'{self.base}-sourcing.html'}
        for d in order_lines(self.results):
            out[d] = f'{self.base}-order-{d}.csv'
        return out

    def save_all(self) -> List[str]:
        paths = self._paths()
        written = []
        write_bom_csv(paths['bom'], self.results)
        written.append(paths['bom'])
        for d, lines in order_lines(self.results).items():
            with open(paths[d], 'w', encoding='utf-8', newline='') as f:
                f.write(order_csv(lines))
            written.append(paths[d])
        self.write_report()
        written.append(paths['report'])
        return written

    def write_report(self) -> str:
        path = self._paths()['report']
        title = os.path.basename(self.base) + ' — sourcing'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(build_report(title, self.results, self.s, self.date, self.rate_note, self.skipped))
        return path

    def _on_save(self, _evt) -> None:
        try:
            files = self.save_all()
        except OSError as e:
            wx.MessageBox(f'Could not write the files:\n{e}', 'place-news', wx.OK | wx.ICON_ERROR)
            return
        wx.MessageBox('Written next to the board:\n\n' + '\n'.join(os.path.basename(p) for p in files)
                      + '\n\nOrder lists: upload them in each distributor\'s BOM / list tool (columns: '
                        'part number, quantity, customer reference = designators).',
                      'place-news', wx.OK | wx.ICON_INFORMATION)

    def _on_report(self, _evt) -> None:
        try:
            path = self.write_report()
            webbrowser.open('file://' + os.path.abspath(path))
        except Exception as e:
            wx.MessageBox(f'Report: {e}', 'place-news', wx.OK)


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

class PlaceNewsSourcingAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'place-news — Sourcing (BOM)'
        self.category = 'Fabrication'
        self.description = ('Stock, prices, minimum orders, lifecycle and replacements at DigiKey, '
                            'Mouser, Farnell, TME and LCSC; BOM and order lists')
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), 'icon_sourcing.png')
        self.dark_icon_file_name = os.path.join(os.path.dirname(__file__), 'icon_sourcing_dark.png')

    def Run(self):
        try:
            self._run(pcbnew.GetBoard())
        except Exception as e:
            import traceback
            wx.MessageBox(f'Sourcing failed:\n{e}\n\n{traceback.format_exc()[-1500:]}', 'place-news',
                          wx.OK | wx.ICON_ERROR)

    def _run(self, board) -> None:
        parts, skipped = board_parts(board)
        lines = group_parts(parts)
        if not lines:
            wx.MessageBox('No part in the BOM.', 'place-news', wx.OK | wx.ICON_INFORMATION)
            return
        cfg = load_settings()
        dlg = SourcingDialog(None, cfg, len(lines), len(parts))
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        cfg = dlg.values
        dlg.Destroy()
        save_settings(cfg)

        cache = Cache(os.path.join(settings_dir(), 'sourcing-cache.json'))
        rates, day = ecb_rates(cache)
        dists = [d for d in make_distributors(cfg, cache, rates) if d.configured()]
        if not dists:
            wx.MessageBox('No distributor to ask: enter at least one API key, or tick LCSC.',
                          'place-news', wx.OK | wx.ICON_WARNING)
            return
        s = Settings(boards=cfg['boards'], spare_percent=float(cfg['spare']), strategy=cfg['strategy'],
                     order=tuple(cfg['order']), search_without_mpn=cfg['search_without_mpn'],
                     alternates=cfg['alternates'], r_tolerance=float(cfg['r_tolerance']))
        progress = wx.ProgressDialog('place-news — Sourcing', 'Asking the distributors...',
                                     maximum=len(lines), parent=None,
                                     style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME
                                     | wx.PD_REMAINING_TIME | wx.PD_SMOOTH)

        def on_line(k: int, n: int, designators: str) -> bool:
            cont, _skip = progress.Update(k, f'{designators}  ({k + 1}/{n}) — '
                                             + ', '.join(d.name for d in dists))
            return bool(cont)
        try:
            sourcer = Sourcer(dists, s, progress=on_line)
            results = sourcer.run(lines)
        finally:
            progress.Destroy()
            cache.save()
        if sourcer.cancelled and not results:
            return
        if sourcer.cancelled:
            wx.MessageBox(f'Stopped after {len(results)} of {len(lines)} lines: the BOM and the order '
                          f'lists only cover these lines.', 'place-news', wx.OK | wx.ICON_WARNING)
        date = time.strftime('%Y-%m-%d %H:%M')
        converted = any('converted from' in (o.note or '') for r in results
                        for o in r.offers + r.candidates + [a.offer for a in r.alternates])
        rate_note = ''
        if any(d.name == 'LCSC' for d in dists) or converted:
            rate_note = (f'Prices in other currencies (LCSC: US dollars) converted at the ECB rates of '
                         f'{day}: 1 USD = {rates["USD"]:.4f} EUR.' if 'USD' in rates else
                         'ECB rates not reached: LCSC prices converted at 1 USD = 0.92 EUR, prices in '
                         'other currencies left out.')
        errors = sorted({e for r in results for e in r.errors})
        rdlg = ResultsDialog(None, board, results, s, date, rate_note, skipped)
        try:
            rdlg.write_report()
        except OSError:
            pass
        if errors:
            wx.MessageBox('Some distributors answered with an error:\n\n' + '\n'.join(errors[:8]),
                          'place-news', wx.OK | wx.ICON_WARNING)
        rdlg.ShowModal()
        rdlg.Destroy()
