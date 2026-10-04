"""Runs the whole action plugin (dialogs included) on the e2e test board.

Needs KiCad's Python and a display (use xvfb-run on a headless machine):
    xvfb-run -a python3 tests/gui_smoke.py
Modal dialogs are auto-accepted; the result summary is printed.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pcbnew  # noqa: E402
import wx      # noqa: E402

from tests.e2e_kicad import build_board  # noqa: E402


def main():
    app = wx.App(False)
    pcbnew.ActionPlugin.register = lambda self: None   # no KiCad frame to register into
    workdir = tempfile.mkdtemp(prefix='place-news-gui-')
    board, path = build_board(workdir)

    import plugin.place_news_action as act
    import plugin.settings_dialog as sd
    import plugin.aesthetic_learning as al
    al.default_store_path = lambda: os.path.join(workdir, 'learning.json')

    pcbnew.GetBoard = lambda: board
    pcbnew.Refresh = lambda: None
    seen = {}

    def settings_modal(self):
        seen['settings'] = True
        self._chk_isolation.SetValue(True)
        return wx.ID_OK

    def results_modal(self):
        seen['results'] = [c.GetLabel() for c in self.GetChildren()
                           if isinstance(c, wx.StaticText)]
        return wx.ID_OK

    sd.SettingsDialog.ShowModal = settings_modal
    sd.SettingsDialog.EndModal = lambda self, code: None
    act._ResultsDialog.ShowModal = results_modal
    act.wx.MessageBox = lambda *a, **k: print('MessageBox:', a[:1])

    act.PlaceNewsAction().Run()
    pcbnew.SaveBoard(path, board, True)
    print('settings dialog shown:', seen.get('settings'))
    print('results dialog:')
    for label in seen.get('results', []):
        print('   ', label)
    print('board saved to', path)
    app.Destroy()


if __name__ == '__main__':
    main()
