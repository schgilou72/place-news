"""Runs the routing action (dialogs included) on the placed e2e test board.

Needs KiCad's Python, a display (use xvfb-run on a headless machine), Java 25+
and a Freerouting jar (PLACE_NEWS_FREEROUTING_JAR or auto-detected):
    xvfb-run -a python3 tests/gui_route_smoke.py
Modal dialogs are auto-accepted; the result summary is printed.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pcbnew  # noqa: E402
import wx      # noqa: E402

pcbnew.ActionPlugin.register = lambda self: None   # no KiCad frame to register into

from tests.e2e_kicad import place_board, run_drc, isolation_violations  # noqa: E402


def main():
    app = wx.App(False)
    workdir = tempfile.mkdtemp(prefix='place-news-gui-route-')
    board, path, _model = place_board(workdir)

    import plugin.route_action as ra
    from plugin import specctra

    jar = os.environ.get('PLACE_NEWS_FREEROUTING_JAR') or specctra.find_freerouting_jar()
    pcbnew.GetBoard = lambda: board
    seen = {}

    def route_modal(self):
        seen['dialog'] = True
        if jar:
            self._jar.SetPath(jar)
        return wx.ID_OK

    def result_modal(self):
        seen['result'] = self.text
        return wx.ID_OK

    ra.RouteDialog.ShowModal = route_modal
    ra.RouteResultDialog.ShowModal = result_modal
    ra.wx.MessageBox = lambda *a, **k: print('MessageBox:', a[:1]) or wx.YES

    tracks_before = len(board.GetTracks())
    ra.PlaceNewsRouteAction().Run()
    pcbnew.SaveBoard(path, board, True)
    print('route dialog shown:', seen.get('dialog'))
    print('tracks: before', tracks_before, 'after', len(board.GetTracks()))
    print('result dialog:')
    print(seen.get('result', '(none)'))
    violations = run_drc(path)
    print('KiCad DRC clearance/creepage violations:', len(isolation_violations(violations)))
    print('board saved to', path)
    app.Destroy()


if __name__ == '__main__':
    main()
