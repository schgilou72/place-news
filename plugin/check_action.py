"""place-news — Aesthetic check: score the look of placement and routing,
mark every issue on the board (User.Comments layer) and in a visual HTML
report, and learn the user's taste from their opinion and their edits."""
from __future__ import annotations

import os
import tempfile
import textwrap
import webbrowser
from typing import List, Optional, Sequence

import pcbnew
import wx

from .aesthetic_check import CRITERIA, CheckResult, Issue, check, extract_geometry
from .aesthetic_learning import Learner, board_state
from .aesthetic_report import build_html

MARKER_GROUP = 'place-news aesthetic check'
MAX_MARKERS = 300


def clear_markers(board) -> int:
    """Remove the markers of a previous check (detached, so Undo works)."""
    removed = 0
    for group in list(board.Groups()):
        if str(group.GetName()) != MARKER_GROUP:
            continue
        for item in list(group.GetItems()):
            try:
                group.RemoveItem(item)
                board.Remove(item)
                removed += 1
            except Exception:
                pass
        board.Remove(group)
    return removed


def add_markers(board, issues: Sequence[Issue]) -> int:
    """A numbered circle per issue on User.Comments, in one group."""
    layer = pcbnew.Cmts_User
    if not board.IsLayerEnabled(layer):
        ls = board.GetEnabledLayers()
        ls.AddLayer(layer)
        board.SetEnabledLayers(ls)
    group = pcbnew.PCB_GROUP(board)
    group.SetName(MARKER_GROUP)
    board.Add(group)
    r = pcbnew.FromMM(1.3)
    for n, iss in enumerate(issues, start=1):
        c = pcbnew.PCB_SHAPE(board)
        c.SetShape(pcbnew.SHAPE_T_CIRCLE)
        c.SetCenter(pcbnew.VECTOR2I(int(iss.x), int(iss.y)))
        c.SetRadius(r)
        c.SetLayer(layer)
        c.SetWidth(pcbnew.FromMM(0.15))
        board.Add(c)
        group.AddItem(c)
        t = pcbnew.PCB_TEXT(board)
        t.SetText(str(n))
        t.SetPosition(pcbnew.VECTOR2I(int(iss.x) + r, int(iss.y) - r))
        t.SetLayer(layer)
        t.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
        t.SetTextThickness(pcbnew.FromMM(0.12))
        board.Add(t)
        group.AddItem(t)
    return len(issues)


def report_path(board) -> str:
    fn = str(board.GetFileName() or '')
    if fn:
        return os.path.splitext(fn)[0] + '-aesthetic-check.html'
    return os.path.join(tempfile.gettempdir(), 'place-news-aesthetic-check.html')


def ordered_issues(result: CheckResult) -> List[Issue]:
    order = {k: i for i, k in enumerate(CRITERIA)}
    issues = sorted(result.issues, key=lambda i: (order[i.criterion], i.y, i.x))
    return issues[:MAX_MARKERS]


class CheckDialog(wx.Dialog):
    ID_LIKE = wx.NewIdRef()
    ID_DISLIKE = wx.NewIdRef()

    def __init__(self, parent, result: CheckResult, issues: Sequence[Issue], report: str,
                 learned: Sequence[str], summary: Sequence[str], n_markers: int):
        super().__init__(parent, title='place-news — Aesthetic check',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.report = report
        self.liked: Optional[bool] = None
        self.bothered: List[str] = []
        grey = wx.Colour(90, 90, 90)
        main = wx.BoxSizer(wx.VERTICAL)

        def fmt(v):
            return '—' if v is None else f'{v:.0f}'
        head = wx.StaticText(self, label=f'Score {fmt(result.score)} / 100    '
                                         f'placement {fmt(result.group_score("placement"))} · '
                                         f'routing {fmt(result.group_score("routing"))} · '
                                         f'cost {fmt(result.group_score("cost"))}')
        font = head.GetFont()
        font.SetPointSize(font.GetPointSize() + 3)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        head.SetFont(font)
        main.Add(head, 0, wx.ALL, 10)

        lst = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for col, (title, width) in enumerate((('Criterion', 300), ('Quality', 70),
                                              ('Issues', 60), ('Weight', 60))):
            lst.InsertColumn(col, title, width=width)
        for k, (group, label, _s) in CRITERIA.items():
            q = result.qualities[k]
            row = lst.InsertItem(lst.GetItemCount(), f'{group}: {label}')
            lst.SetItem(row, 1, '—' if q is None else f'{100 * q:.0f} %')
            lst.SetItem(row, 2, str(sum(1 for i in issues if i.criterion == k)))
            lst.SetItem(row, 3, f'{result.weights.get(k, 1.0):.2f}')
        lst.SetMinSize(wx.Size(520, 24 * (len(CRITERIA) + 2)))
        main.Add(lst, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        info = []
        if result.layers is not None:
            info.append(textwrap.fill(result.layers.describe(), 90))
            for line in result.layers.quick_rules():
                info.append(textwrap.fill('For comparison: ' + line, 90))
        info.append(f'{n_markers} issue(s) numbered on the User.Comments layer (group '
                    f'"{MARKER_GROUP}": Edit > Undo or delete the group to remove them).')
        if learned:
            info.append('Learned from your edits since the last run:')
            info += ['  ' + line for line in learned[:6]]
        lbl = wx.StaticText(self, label='\n'.join(info))
        lbl.SetForegroundColour(grey)
        main.Add(lbl, 0, wx.ALL, 10)
        btn_report = wx.Button(self, label='Open the visual report')
        btn_report.Bind(wx.EVT_BUTTON, self._on_report)
        main.Add(btn_report, 0, wx.LEFT | wx.BOTTOM, 10)

        box = wx.StaticBoxSizer(wx.StaticBox(self, label='Your opinion teaches place-news your taste'),
                                wx.VERTICAL)
        box.Add(wx.StaticText(self, label='What bothers you most? (optional)'), 0, wx.ALL, 4)
        self._labels = [k for k in CRITERIA]
        self._bother = wx.CheckListBox(self, choices=[CRITERIA[k][1] for k in self._labels])
        self._bother.SetMinSize(wx.Size(-1, 120))
        box.Add(self._bother, 0, wx.ALL | wx.EXPAND, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        like = wx.Button(self, self.ID_LIKE, label='\U0001F44D  I like it')
        dislike = wx.Button(self, self.ID_DISLIKE, label='\U0001F44E  Not nice enough')
        row.Add(like, 0, wx.RIGHT, 8)
        row.Add(dislike, 0)
        box.Add(row, 0, wx.ALL, 4)
        summ = wx.StaticText(self, label='\n'.join(summary))
        summ.SetForegroundColour(grey)
        box.Add(summ, 0, wx.ALL, 4)
        main.Add(box, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        close = self.CreateStdDialogButtonSizer(wx.CLOSE)
        main.Add(close, 0, wx.ALL | wx.EXPAND, 10)
        self.Bind(wx.EVT_BUTTON, lambda e: self._answer(True), id=self.ID_LIKE)
        self.Bind(wx.EVT_BUTTON, lambda e: self._answer(False), id=self.ID_DISLIKE)
        self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
        self.SetSizerAndFit(main)
        self.CentreOnParent()

    def _on_report(self, _evt):
        try:
            webbrowser.open('file://' + os.path.abspath(self.report))
        except Exception:
            wx.MessageBox(f'Report: {self.report}', 'place-news', wx.OK)

    def _answer(self, liked: bool):
        self.liked = liked
        self.bothered = [self._labels[i] for i in self._bother.GetCheckedItems()]
        self.EndModal(wx.ID_OK)


class PlaceNewsCheckAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'place-news — Aesthetic check'
        self.category = 'Inspection'
        self.description = ('Score the look of placement and routing, mark the issues on the '
                            'board and learn your taste')
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), 'icon_check.png')
        self.dark_icon_file_name = os.path.join(os.path.dirname(__file__), 'icon_check_dark.png')

    def Run(self):
        try:
            self._run(pcbnew.GetBoard())
        except Exception as e:          # never leave the user with a silent failure
            import traceback
            wx.MessageBox(f'The aesthetic check failed:\n{e}\n\n{traceback.format_exc()[-1500:]}',
                          'place-news', wx.OK | wx.ICON_ERROR)

    def _run(self, board):
        busy = wx.BusyInfo('Checking placement and routing...')
        try:
            learner = Learner()
            key = str(board.GetFileName() or '')
            geom = extract_geometry(board)
            result = check(geom, learner.weights)
            learned = learner.learn_from_edits(key, result.qualities, board_state(board))
            if learned:                       # weights moved: score again
                result = check(geom, learner.weights)
            clear_markers(board)
            issues = ordered_issues(result)
            n_markers = add_markers(board, issues)
            path = report_path(board)
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    title = os.path.basename(key) or 'board'
                    f.write(build_html(f'{title} — aesthetic check', geom, result, issues,
                                       learner.summary()))
            except OSError:
                path = os.path.join(tempfile.gettempdir(), 'place-news-aesthetic-check.html')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(build_html('aesthetic check', geom, result, issues, learner.summary()))
        finally:
            del busy

        dlg = CheckDialog(None, result, issues, path, learned, learner.summary(), n_markers)
        answered = dlg.ShowModal() == wx.ID_OK
        liked, bothered = dlg.liked, dlg.bothered
        dlg.Destroy()
        if answered and liked is not None:
            notes = learner.feedback(key, result.qualities, liked, bothered)
            wx.MessageBox('Thanks, noted.' + ('\n\n' + '\n'.join(notes[:8]) if notes else ''),
                          'place-news', wx.OK | wx.ICON_INFORMATION)
        # Remember this state: later edits are compared with it.
        learner.record_run(key, 'check', result.qualities, board_state(board), {})
        learner.save()
