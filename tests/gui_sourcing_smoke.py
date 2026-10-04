"""Runs the sourcing action (dialogs included) on the placed e2e test board
with realistic values, against LCSC (no key needed, so this needs network
access), writes the fields into the components and saves the BOM, the order
lists and the report.

    xvfb-run -a python3 tests/gui_sourcing_smoke.py
The settings and the cache go to a temporary folder.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pcbnew  # noqa: E402
import wx      # noqa: E402

pcbnew.ActionPlugin.register = lambda self: None   # no KiCad frame to register into

from tests.e2e_kicad import place_board  # noqa: E402

VALUES = {'R1': '10k', 'R2': '10k', 'R3': '4k7', 'R4': '10k', 'C3': '100nF', 'U1': 'UC3843BD1R2G',
          'D1': '1N4007', 'C1': '100uF', 'C2': '470uF', 'J1': 'Screw terminal', 'J2': 'Screw terminal'}
FIELDS = {'Q1': {'MPN': 'IRF840PBF', 'Manufacturer': 'Vishay'}, 'C3': {'Voltage': '50V'},
          'C1': {'Voltage': '400V'}, 'C2': {'Voltage': '25V'}}


def main():
    app = wx.App(False)
    workdir = tempfile.mkdtemp(prefix='place-news-gui-sourcing-')
    board, path, _model = place_board(workdir)
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        if ref in VALUES:
            fp.SetValue(VALUES[ref])
        for k, v in FIELDS.get(ref, {}).items():
            fp.SetField(k, v)
            fp.GetField(k).SetVisible(False)

    import plugin.sourcing_action as sa
    sa.settings_dir = lambda: os.path.join(workdir, 'settings')
    pcbnew.GetBoard = lambda: board
    seen = {}

    def settings_modal(self):
        seen['settings'] = [c.GetLabel() for c in self.GetChildren() if isinstance(c, wx.StaticText)][:3]
        self.boards.SetValue(5)
        self.chk_lcsc.SetValue(True)
        return wx.ID_OK

    def results_modal(self):
        seen['head'] = self.head.GetLabel()
        seen['rows'] = [[self.lst.GetItemText(i, c) for c in range(self.lst.GetColumnCount())]
                        for i in range(self.lst.GetItemCount())]
        self._on_write(None)
        seen['files'] = self.save_all()
        seen['info'] = self.info.GetLabel()
        return wx.ID_CLOSE

    sa.SourcingDialog.ShowModal = settings_modal
    sa.ResultsDialog.ShowModal = results_modal
    sa.wx.MessageBox = lambda *a, **k: print('MessageBox:', str(a[0]).replace('\n', ' | ')[:300])

    sa.PlaceNewsSourcingAction().Run()
    print('settings dialog:', seen.get('settings'))
    print('results:', seen.get('head'))
    for row in seen.get('rows', []):
        print('   ', ' | '.join(row))
    print(seen.get('info', ''))
    for p in seen.get('files', []):
        print('file:', os.path.basename(p), os.path.getsize(p), 'bytes')
    q1 = next(fp for fp in board.GetFootprints() if fp.GetReference() == 'Q1')
    print('Q1 fields:', {k: v for k, v in q1.GetFieldsText().items()
                         if k not in ('Reference', 'Value', 'Datasheet', 'Description')})
    r1 = next(fp for fp in board.GetFootprints() if fp.GetReference() == 'R1')
    f = r1.GetField('MPN') if r1.HasField('MPN') else None
    print('R1 MPN field:', f.GetText() if f else None, 'visible:', f.IsVisible() if f else None,
          'layer:', f.GetLayerName() if f else None)
    pcbnew.SaveBoard(path, board, True)
    app.Destroy()


if __name__ == '__main__':
    main()
