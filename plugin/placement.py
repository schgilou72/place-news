"""Apply optimized positions back to the KiCad board.

Footprints are matched by their UUID: references are not unique on every
board (logos, test points, unannotated parts all share 'REF**' or 'TP?').
"""
from __future__ import annotations

from typing import Dict, List, Tuple
from .board_model import BoardModel


def _uuid(fp) -> str:
    try:
        return str(fp.m_Uuid.AsString())
    except Exception:
        return ''


def footprints_by_uuid(board) -> Dict[str, object]:
    return {_uuid(fp): fp for fp in board.GetFootprints()}


def save_original_positions(board) -> List[Tuple[str, str, int, int, float]]:
    """Save original positions of all footprints for undo:
    (uuid, reference, x, y, angle)."""
    positions = []
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        positions.append((
            _uuid(fp),
            fp.GetReference(),
            pos.x,
            pos.y,
            fp.GetOrientationDegrees(),
        ))
    return positions


def restore_original_positions(board, positions: List[Tuple[str, str, int, int, float]]):
    """Restore footprints to their original positions (undo)."""
    import pcbnew
    by_uuid = footprints_by_uuid(board)
    for uid, ref, x, y, angle in positions:
        fp = by_uuid.get(uid) if uid else board.FindFootprintByReference(ref)
        if fp is not None:
            fp.SetPosition(pcbnew.VECTOR2I(x, y))
            fp.SetOrientation(pcbnew.EDA_ANGLE(angle, pcbnew.DEGREES_T))
    board.GetConnectivity().RecalculateRatsnest()
    pcbnew.Refresh()


def apply_model_to_board(board, model: BoardModel):
    """Write the optimized positions from the BoardModel back to pcbnew."""
    import pcbnew

    by_uuid = footprints_by_uuid(board)
    by_ref = {}
    for fp in board.GetFootprints():
        by_ref.setdefault(fp.GetReference(), fp)

    for mfp in model.footprints:
        kfp = by_uuid.get(mfp.uuid) if mfp.uuid else by_ref.get(mfp.reference)
        if kfp is None or kfp.IsLocked():
            continue
        kfp.SetPosition(pcbnew.VECTOR2I(int(mfp.x), int(mfp.y)))
        kfp.SetOrientation(pcbnew.EDA_ANGLE(mfp.angle_deg, pcbnew.DEGREES_T))

    board.GetConnectivity().RecalculateRatsnest()
    pcbnew.Refresh()
