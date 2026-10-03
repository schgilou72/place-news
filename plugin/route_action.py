"""place-news — Route: autorouting with Freerouting, keeping the board's
isolation rules (net-class clearances, custom clearance / creepage rules).

Flow: export Specctra DSN (rule areas and isolation rules fixed up, see
specctra.py) -> Freerouting (headless) -> import the session's routes ->
optional KiCad DRC on a copy of the board -> summary. Everything the router
changes is one step in KiCad's undo history.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import pcbnew
import wx

from . import specctra
from .route_report import (class_label, describe_class_rules, describe_keepouts,  # noqa: F401
                           progress_text, summarize_result)

_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), 'route_settings.json')
_DEFAULTS: Dict[str, Any] = {
    'jar': '',
    'java': '',
    'passes': 30,
    'use_isolation': True,
    'check_drc': True,
}

FREEROUTING_URL = 'https://github.com/freerouting/freerouting/releases'
JAVA_URL = 'https://adoptium.net/temurin/releases/'


def load_route_settings() -> Dict[str, Any]:
    data = dict(_DEFAULTS)
    try:
        with open(_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data.update(json.load(f))
    except Exception:
        pass
    return data


def save_route_settings(data: Dict[str, Any]) -> None:
    try:
        with open(_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Board side
# ---------------------------------------------------------------------------

def safe_import_ses(board, ses_path: str) -> bool:
    """Import the session's routes into the board shown in the editor.

    KiCad's importer frees the unlocked tracks and the DRC markers it
    replaces, which leaves dangling pointers in the undo snapshot the action
    plugin framework took. Detaching them first (board.Remove) keeps them
    alive, so Edit > Undo restores the previous tracks."""
    for track in list(board.GetTracks()):
        try:
            if not track.IsLocked():
                board.Remove(track)
        except Exception:
            pass
    try:
        for marker in list(board.Markers()):
            board.Remove(marker)
    except Exception:
        pass
    return specctra.import_ses(board, ses_path)


def _board_name(board) -> str:
    fn = str(board.GetFileName() or '')
    return os.path.splitext(os.path.basename(fn))[0] or 'board'


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class RouteDialog(wx.Dialog):
    def __init__(self, parent, board, settings: Dict[str, Any],
                 preview: Optional[specctra.ExportReport], preview_error: str = ''):
        super().__init__(parent, title='place-news — Route with Freerouting',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._settings = dict(settings)
        self._board = board
        self._preview = preview
        main = wx.BoxSizer(wx.VERTICAL)
        grey = wx.Colour(90, 90, 90)
        warn = wx.Colour(190, 110, 0)

        # --- Router ---
        box = wx.StaticBoxSizer(wx.StaticBox(self, label='Router'), wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(self, label='Freerouting jar:'), 0, wx.ALIGN_CENTER_VERTICAL)
        jar = specctra.find_freerouting_jar(self._settings.get('jar', ''))
        self._jar = wx.FilePickerCtrl(self, path=jar, message='Freerouting executable jar',
                                      wildcard='Java archive (*.jar)|*.jar',
                                      style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL)
        self._jar.SetMinSize(wx.Size(380, -1))
        grid.Add(self._jar, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label='Java runtime:'), 0, wx.ALIGN_CENTER_VERTICAL)
        java, version = specctra.find_java(self._settings.get('java', ''))
        self._java = wx.FilePickerCtrl(self, path=java, message='Java executable',
                                       style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL)
        grid.Add(self._java, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label='Max. routing passes:'), 0, wx.ALIGN_CENTER_VERTICAL)
        self._passes = wx.SpinCtrl(self, min=1, max=500, initial=int(self._settings.get('passes', 30)))
        grid.Add(self._passes, 0)
        box.Add(grid, 0, wx.ALL | wx.EXPAND, 6)
        notes = []
        if not jar:
            notes.append(f'Freerouting not found: download freerouting-2.x-executable.jar from\n'
                         f'{FREEROUTING_URL} and select it above.')
        if version < specctra.MIN_JAVA:
            found = f'Java {version} found' if version else 'No Java found'
            notes.append(f'{found}; Freerouting 2.x needs Java {specctra.MIN_JAVA} or later\n'
                         f'({JAVA_URL}).')
        else:
            notes.append(f'Java {version} found.')
        self._lbl_tools = wx.StaticText(self, label='\n'.join(notes))
        self._lbl_tools.SetForegroundColour(warn if (not jar or version < specctra.MIN_JAVA) else grey)
        box.Add(self._lbl_tools, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        main.Add(box, 0, wx.ALL | wx.EXPAND, 8)

        # --- Rules ---
        rbox = wx.StaticBoxSizer(wx.StaticBox(self, label='Design rules'), wx.VERTICAL)
        self._chk_iso = wx.CheckBox(self, label='Respect isolation rules (net classes + custom rules)')
        self._chk_iso.SetValue(bool(self._settings.get('use_isolation', True)))
        self._chk_iso.SetToolTip(
            'Freerouting knows one clearance per pair of net classes. The clearance,\n'
            'creepage and physical_clearance rules of the project (.kicad_dru) are\n'
            'turned into such class-to-class clearances. Creepage becomes a straight-\n'
            'line distance, which is never longer than KiCad\'s creepage path.')
        rbox.Add(self._chk_iso, 0, wx.ALL, 5)
        lines: List[str] = []
        if preview is not None:
            lines += describe_class_rules(preview.classes) or [
                'No custom rule asks for more than the net-class clearances.']
            if preview.rules is not None:
                for name, reason in preview.rules.unsupported[:3]:
                    lines.append(f"Rule '{name}' ignored: {reason}")
            lines += describe_keepouts(preview.keepouts)
        elif preview_error:
            lines.append(f'Could not read the board for routing: {preview_error}')
        if not str(board.GetFileName() or ''):
            lines.append('The board is not saved: the custom rules file cannot be found.')
        lbl = wx.StaticText(self, label='\n'.join(lines))
        lbl.SetForegroundColour(grey)
        rbox.Add(lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._chk_drc = wx.CheckBox(self, label="Check the result with KiCad's DRC")
        cli = specctra.find_kicad_cli()
        self._chk_drc.SetValue(bool(self._settings.get('check_drc', True)) and bool(cli))
        self._chk_drc.Enable(bool(cli))
        if not cli:
            self._chk_drc.SetToolTip('kicad-cli was not found next to KiCad.')
        rbox.Add(self._chk_drc, 0, wx.ALL, 5)
        main.Add(rbox, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        note = wx.StaticText(self, label=(
            'Unlocked tracks are replaced by the router\'s result (existing ones are kept\n'
            'as a starting point; lock tracks to protect them). Edit > Undo reverts.'))
        note.SetForegroundColour(grey)
        main.Add(note, 0, wx.ALL, 10)

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK).SetLabel('Route')
        main.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.SetSizerAndFit(main)
        self.CentreOnParent()

    def _on_ok(self, evt):
        jar = self.jar
        java = self.java
        if not jar or not os.path.isfile(jar):
            wx.MessageBox(f'Select the Freerouting jar (freerouting-2.x-executable.jar).\n'
                          f'Download: {FREEROUTING_URL}', 'place-news', wx.OK | wx.ICON_WARNING)
            return
        version = specctra._java_major(java) if java else 0
        if version < specctra.MIN_JAVA:
            if wx.MessageBox(f'Java {specctra.MIN_JAVA} or later is needed by Freerouting 2.x '
                             f'(found: {version or "none"}).\nTry anyway?', 'place-news',
                             wx.YES_NO | wx.ICON_WARNING) != wx.YES:
                return
        self.EndModal(wx.ID_OK)

    @property
    def jar(self) -> str:
        return self._jar.GetPath().strip()

    @property
    def java(self) -> str:
        return self._java.GetPath().strip()

    @property
    def values(self) -> Dict[str, Any]:
        return {'jar': self.jar, 'java': self.java, 'passes': int(self._passes.GetValue()),
                'use_isolation': self._chk_iso.GetValue(), 'check_drc': self._chk_drc.GetValue()}


class RouteResultDialog(wx.Dialog):
    def __init__(self, parent, lines: List[str], ok: bool = True):
        super().__init__(parent, title='place-news — Routing result',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        main = wx.BoxSizer(wx.VERTICAL)
        self.text = '\n'.join(lines)
        txt = wx.TextCtrl(self, value=self.text, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        txt.SetMinSize(wx.Size(560, min(520, 22 * (len(lines) + 2))))
        main.Add(txt, 1, wx.ALL | wx.EXPAND, 8)
        btns = self.CreateStdDialogButtonSizer(wx.OK)
        main.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
        self.SetSizerAndFit(main)
        self.CentreOnParent()


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

class PlaceNewsRouteAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'place-news — Route (Freerouting)'
        self.category = 'Routing'
        self.description = ('Autoroute with Freerouting while keeping the clearance and creepage '
                            'rules of the board')
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), 'icon_route.png')
        self.dark_icon_file_name = os.path.join(os.path.dirname(__file__), 'icon_route_dark.png')

    def Run(self):
        board = pcbnew.GetBoard()
        if not any(len(fp.Pads()) for fp in board.GetFootprints()):
            wx.MessageBox('The board has no pads to route.', 'place-news', wx.OK | wx.ICON_WARNING)
            return
        workdir = tempfile.mkdtemp(prefix='place-news-route-')
        keep_workdir = False
        try:
            keep_workdir = self._run(board, workdir)
        finally:
            if not keep_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def _run(self, board, workdir: str) -> bool:
        """The routing flow. Returns True to keep the working files (shown to
        the user for diagnosis)."""
        settings = load_route_settings()
        name = _board_name(board)
        dsn = os.path.join(workdir, name + '.dsn')
        ses = os.path.join(workdir, name + '.ses')

        preview, preview_error = None, ''
        busy = wx.BusyInfo('Reading the board and its design rules...')
        try:
            preview = specctra.export_dsn(board, dsn, use_isolation=True)
        except Exception as e:      # shown in the dialog
            preview_error = str(e)
        finally:
            del busy

        dlg = RouteDialog(None, board, settings, preview, preview_error)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return False
        values = dlg.values
        dlg.Destroy()
        settings.update(values)
        save_route_settings(settings)

        export = preview
        if export is None or not values['use_isolation']:
            try:
                export = specctra.export_dsn(board, dsn, use_isolation=values['use_isolation'])
            except Exception as e:
                wx.MessageBox(f'Specctra export failed:\n{e}', 'place-news', wx.OK | wx.ICON_ERROR)
                return False

        progress = wx.ProgressDialog(
            'place-news \u2014 Routing', 'Starting Freerouting...', maximum=100, parent=None,
            style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME | wx.PD_SMOOTH)

        def poll(line: str, _elapsed: float) -> bool:
            cont, _skip = progress.Pulse(progress_text(line))
            return bool(cont)

        start = time.time()
        try:
            result = specctra.run_freerouting(values['java'], values['jar'], dsn, ses,
                                              passes=values['passes'], poll=poll)
        except Exception as e:
            wx.MessageBox(f'Freerouting could not be started:\n{e}', 'place-news',
                          wx.OK | wx.ICON_ERROR)
            return False
        finally:
            progress.Destroy()
        elapsed = time.time() - start

        if result.timed_out:
            wx.MessageBox('Freerouting did not finish within the time limit; the board was not '
                          'changed.', 'place-news', wx.OK | wx.ICON_WARNING)
            return False
        if result.cancelled:
            wx.MessageBox('Routing cancelled; the board was not changed.', 'place-news',
                          wx.OK | wx.ICON_INFORMATION)
            return False
        if not result.ok:
            dlg = RouteResultDialog(None, ['Freerouting did not produce a result '
                                           f'(exit code {result.returncode}).', '',
                                           f'Working files: {workdir}', ''] + result.log[-15:],
                                    ok=False)
            dlg.ShowModal()
            dlg.Destroy()
            return True

        refill = any(z.IsFilled() for z in board.Zones() if not z.GetIsRuleArea())
        if not safe_import_ses(board, ses):
            wx.MessageBox(f'KiCad could not import the routing session.\n\n'
                          f'Edit > Undo restores the previous tracks.\nWorking files: {workdir}',
                          'place-news', wx.OK | wx.ICON_ERROR)
            return True
        if refill:
            try:
                pcbnew.ZONE_FILLER(board).Fill(board.Zones())
            except Exception:
                refill = False

        drc = None
        if values['check_drc']:
            busy = wx.BusyInfo("Running KiCad's DRC on a copy of the board...")
            try:
                drc = specctra.drc_summary(board, specctra.find_kicad_cli(),
                                           workdir=os.path.join(workdir, 'drc'))
            except Exception as e:
                drc = specctra.DrcSummary(error=str(e))
            finally:
                del busy

        lines = summarize_result(result, export, drc, elapsed, values['check_drc'],
                                 zones_refilled=refill)
        dlg = RouteResultDialog(None, lines)
        dlg.ShowModal()
        dlg.Destroy()
        return False
