"""Runs the aesthetic check action (dialog included) on the placed e2e test
board, answers "not nice enough / vias bother me", then runs it again after
a hand edit to see the learning from corrections.

Needs KiCad's Python and a display (use xvfb-run on a headless machine):
    xvfb-run -a python3 tests/gui_check_smoke.py
The learning store goes to a temporary folder.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pcbnew  # noqa: E402
import wx      # noqa: E402

pcbnew.ActionPlugin.register = lambda self: None   # no KiCad frame to register into

from tests.e2e_kicad import place_board  # noqa: E402


def main():
    app = wx.App(False)
    workdir = tempfile.mkdtemp(prefix='place-news-gui-check-')
    board, path, _model = place_board(workdir)

    import plugin.aesthetic_learning as al
    import plugin.check_action as ca
    store = os.path.join(workdir, 'learning.json')
    al.default_store_path = lambda: store

    pcbnew.GetBoard = lambda: board
    seen = {'dialogs': []}

    def check_modal(self):
        texts = [c.GetLabel() for c in self.GetChildren() if isinstance(c, wx.StaticText)]
        seen['dialogs'].append(texts)
        # "What bothers you most": vias; then thumbs down.
        self._bother.Check(self._labels.index('vias'))
        self._answer(False)
        return wx.ID_OK

    ca.CheckDialog.ShowModal = check_modal
    ca.CheckDialog.EndModal = lambda self, code: None
    ca.wx.MessageBox = lambda *a, **k: print('MessageBox:', a[0].replace('\n', ' | ')[:400])
    ca.webbrowser.open = lambda url: print('would open', url)

    ca.PlaceNewsCheckAction().Run()
    groups = [g for g in board.Groups() if str(g.GetName()) == ca.MARKER_GROUP]
    print('marker groups:', len(groups), 'items:', sum(len(list(g.GetItems())) for g in groups))
    report = ca.report_path(board)
    print('report:', report, os.path.getsize(report), 'bytes')
    print('dialog texts:')
    for t in seen['dialogs'][0]:
        print('   ', t.replace('\n', ' / ')[:300])

    # A hand edit (move a part by 2 mm) and a second check: learned from it.
    fp = next(f for f in board.GetFootprints() if not f.IsLocked())
    p = fp.GetPosition()
    fp.SetPosition(pcbnew.VECTOR2I(p.x + pcbnew.FromMM(2), p.y))
    ca.PlaceNewsCheckAction().Run()
    groups = [g for g in board.Groups() if str(g.GetName()) == ca.MARKER_GROUP]
    print('marker groups after the second run:', len(groups))
    print('second dialog, learned lines:')
    for t in seen['dialogs'][1]:
        if 'Learned' in t or 'weight' in t:
            print('   ', t.replace('\n', ' / ')[:400])
    learner = al.Learner(store)
    print('store:', learner.feedback_count, 'ratings,', learner.correction_count, 'corrections;',
          'vias weight', round(learner.weights['vias'], 2))
    pcbnew.SaveBoard(path, board, True)
    app.Destroy()


if __name__ == '__main__':
    main()
