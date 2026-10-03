"""Place silkscreen reference designators after placement.

Rules (readable references):

* a reference is centred on its component (the middle of the body);
* text is horizontal (0°) or vertical reading bottom to top (90°) — never
  upside down or top to bottom; it follows the long axis of the part;
* it never touches a pad (with a small clearance) nor another reference;
* if the centre does not work, it goes beside the part, centred on the
  part's axis (above / below for horizontal text, left / right for vertical
  text), then further out.

Pure Python except extract_silkscreen_model() / apply_silkscreen().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .board_model import BoardModel

Box = Tuple[int, int, int, int]

# Clearance between a reference and other texts / parts (0.2 mm)
SILK_CLEARANCE = 200_000
# Clearance between a reference and copper pads (0.2 mm)
SILK_PAD_CLEARANCE = 200_000

ANGLE_H = 0      # horizontal, read from the bottom
ANGLE_V = 90     # vertical, read bottom to top


@dataclass
class TextRect:
    """A reference designator: its parent footprint and horizontal size."""
    fp_index: int       # which footprint this belongs to
    cx: int             # current center x (nm)
    cy: int             # current center y (nm)
    width: int          # text bbox width when horizontal (nm)
    height: int         # text bbox height when horizontal (nm)
    angle: int = ANGLE_H    # current drawn angle (0 or 90 after readable(); else as read)
    side: str = 'F'         # 'F' or 'B' (silkscreen layer side)

    def size(self, angle: int) -> Tuple[int, int]:
        return (self.width, self.height) if angle == ANGLE_H else (self.height, self.width)

    @property
    def bbox(self) -> Box:
        w, h = self.size(self.angle)
        return _candidate_bbox(self.cx, self.cy, w, h)


@dataclass
class SilkscreenModel:
    """All data needed for silkscreen placement — pure Python."""
    texts: List[TextRect]
    fp_bboxes: List[Box]                       # indexed by fp_index
    keepouts: List[Box]
    board_bbox: Box                            # (xmin, ymin, xmax, ymax)
    pad_boxes: Dict[str, List[Box]] = field(default_factory=dict)   # side -> pad copper boxes
    fixed_texts: List[Box] = field(default_factory=list)            # references not moved
    # Footprint keep-outs per board side ('F'/'B'); `keepouts` apply to both.
    keepouts_by_side: Dict[str, List[Box]] = field(default_factory=dict)


def _overlaps(a: Box, b: Box) -> bool:
    """Check if two axis-aligned bounding boxes overlap."""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _inside(inner: Box, outer: Box) -> bool:
    """Check if inner bbox is fully inside outer bbox."""
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _candidate_bbox(cx: int, cy: int, w: int, h: int) -> Box:
    hw, hh = w // 2, h // 2
    return (cx - hw, cy - hh, cx + hw, cy + hh)


def _expand_bbox(bbox: Box, margin: int) -> Box:
    """Expand a bbox by margin on all sides."""
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def _overlap_area(a: Box, b: Box) -> int:
    """Compute overlap area between two bboxes (0 if no overlap)."""
    ox = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ox * oy


def preferred_angle(parent: Box) -> int:
    """Text follows the long axis of the part."""
    return ANGLE_V if (parent[3] - parent[1]) > (parent[2] - parent[0]) else ANGLE_H


def _obstacle_density(text: TextRect, model: SilkscreenModel) -> int:
    """Count how many obstacles are near this text's parent footprint."""
    parent = model.fp_bboxes[text.fp_index]
    radius = max(text.width, text.height) * 2
    search = _expand_bbox(parent, radius)
    return sum(1 for i, fb in enumerate(model.fp_bboxes)
               if i != text.fp_index and _overlaps(search, fb))


def _generate_candidates(parent: Box, text: TextRect) -> List[Tuple[int, int, int, bool]]:
    """(cx, cy, angle, centred) candidates in order of preference: the centre
    of the part, then beside it centred on its axis, then further out."""
    px = (parent[0] + parent[2]) // 2
    py = (parent[1] + parent[3]) // 2
    pref = preferred_angle(parent)
    other = ANGLE_H if pref == ANGLE_V else ANGLE_V
    out: List[Tuple[int, int, int, bool]] = [(px, py, pref, True), (px, py, other, True)]
    for k in (1, 3, 6):
        cl = SILK_CLEARANCE * k
        wh, hh = text.size(ANGLE_H)
        wv, hv = text.size(ANGLE_V)
        top_h = (px, parent[1] - hh // 2 - cl, ANGLE_H, False)
        bottom_h = (px, parent[3] + hh // 2 + cl, ANGLE_H, False)
        left_v = (parent[0] - wv // 2 - cl, py, ANGLE_V, False)
        right_v = (parent[2] + wv // 2 + cl, py, ANGLE_V, False)
        right_h = (parent[2] + wh // 2 + cl, py, ANGLE_H, False)
        left_h = (parent[0] - wh // 2 - cl, py, ANGLE_H, False)
        if pref == ANGLE_H:
            out += [top_h, bottom_h, left_v, right_v, right_h, left_h]
        else:
            out += [left_v, right_v, top_h, bottom_h, right_h, left_h]
    # Corners, horizontal (last resort before the least-overlap fallback)
    wh, hh = text.size(ANGLE_H)
    cl = SILK_CLEARANCE
    out += [(parent[2] + wh // 2 + cl, parent[1] - hh // 2 - cl, ANGLE_H, False),
            (parent[2] + wh // 2 + cl, parent[3] + hh // 2 + cl, ANGLE_H, False),
            (parent[0] - wh // 2 - cl, parent[3] + hh // 2 + cl, ANGLE_H, False),
            (parent[0] - wh // 2 - cl, parent[1] - hh // 2 - cl, ANGLE_H, False)]
    return out


def _problems(cbox: Box, text: TextRect, model: SilkscreenModel, placed: Sequence[Box],
              centred: bool) -> int:
    """0 when the candidate is acceptable, else a penalty (overlap area,
    pads weighted most)."""
    total = 0
    for pb in model.pad_boxes.get(text.side, ()):
        total += 100 * _overlap_area(cbox, _expand_bbox(pb, SILK_PAD_CLEARANCE))
    for i, fb in enumerate(model.fp_bboxes):
        if i != text.fp_index:
            total += _overlap_area(cbox, fb)
    if centred and not _inside(cbox, _expand_bbox(model.fp_bboxes[text.fp_index], SILK_CLEARANCE)):
        total += 1     # a centred reference must stay on its part
    for ko in list(model.keepouts) + model.keepouts_by_side.get(text.side, []):
        total += _overlap_area(cbox, ko)
    for pb in placed:
        total += 10 * _overlap_area(cbox, _expand_bbox(pb, SILK_CLEARANCE))
    for fb in model.fixed_texts:
        total += 10 * _overlap_area(cbox, _expand_bbox(fb, SILK_CLEARANCE))
    if not _inside(cbox, model.board_bbox):
        total += 1 + _overlap_area(cbox, cbox) - _overlap_area(cbox, model.board_bbox)
    return total


def place_silkscreen(model: SilkscreenModel) -> List[Tuple[int, int, int]]:
    """Find readable, collision-free positions for the references.

    Returns (cx, cy, angle) per text in model.texts; angle is 0 or 90.
    The most constrained parts are served first. When no candidate is free,
    the one with the least overlap is used."""
    if not model.texts:
        return []
    order = sorted(range(len(model.texts)),
                   key=lambda i: _obstacle_density(model.texts[i], model), reverse=True)
    results: List[Optional[Tuple[int, int, int]]] = [None] * len(model.texts)
    placed: List[Box] = []
    for idx in order:
        text = model.texts[idx]
        parent = model.fp_bboxes[text.fp_index]
        best = None
        best_score = None
        for cx, cy, angle, centred in _generate_candidates(parent, text):
            w, h = text.size(angle)
            cbox = _candidate_bbox(cx, cy, w, h)
            score = _problems(cbox, text, model, placed, centred)
            if score == 0:
                best = (cx, cy, angle)
                break
            if best_score is None or score < best_score:
                best, best_score = (cx, cy, angle), score
        results[idx] = best
        w, h = text.size(best[2])
        placed.append(_candidate_bbox(best[0], best[1], w, h))
    return results  # type: ignore[return-value]


def _draw_angle(ref_text) -> int:
    """Drawn angle of a KiCad text, folded to 0 / 90."""
    try:
        deg = ref_text.GetDrawRotation().AsDegrees()
    except Exception:
        deg = ref_text.GetTextAngleDegrees()
    deg = round(deg) % 180
    return ANGLE_V if 45 <= deg < 135 else ANGLE_H


def extract_silkscreen_model(board, board_model: BoardModel) -> SilkscreenModel:
    """Read visible silkscreen reference designators, pads and fixed texts."""
    import pcbnew

    texts: List[TextRect] = []
    fixed: List[Box] = []
    fp_bboxes = [fp.bbox for fp in board_model.footprints]

    # Map footprints by UUID (references can repeat); fall back to reference.
    uuid_to_idx = {fp.uuid: fp.index for fp in board_model.footprints if fp.uuid}
    ref_to_idx = {fp.reference: fp.index for fp in board_model.footprints}

    pad_boxes: Dict[str, List[Box]] = {'F': [], 'B': []}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            try:
                bb = pad.GetBoundingBox()
                box = (bb.GetX(), bb.GetY(), bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight())
                ls = pad.GetLayerSet()
                if ls.Contains(pcbnew.F_Cu) or ls.Contains(pcbnew.F_Mask):
                    pad_boxes['F'].append(box)
                if ls.Contains(pcbnew.B_Cu) or ls.Contains(pcbnew.B_Mask):
                    pad_boxes['B'].append(box)
            except Exception:
                continue

    for fp in board.GetFootprints():
        ref_text = fp.Reference()
        if not ref_text.IsVisible():
            continue
        layer = ref_text.GetLayer()
        if layer not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            continue
        side = 'F' if layer == pcbnew.F_SilkS else 'B'
        bbox = ref_text.GetBoundingBox()
        box = (bbox.GetX(), bbox.GetY(), bbox.GetX() + bbox.GetWidth(),
               bbox.GetY() + bbox.GetHeight())

        try:
            uid = str(fp.m_Uuid.AsString())
        except Exception:
            uid = ''
        fp_idx = uuid_to_idx.get(uid) if uid else ref_to_idx.get(fp.GetReference())
        # Hand-placed (locked) parts keep their silkscreen as the designer left it.
        if fp_idx is None or board_model.footprints[fp_idx].locked:
            fixed.append(box)
            continue

        angle = _draw_angle(ref_text)
        w, h = bbox.GetWidth(), bbox.GetHeight()
        if angle == ANGLE_V:
            w, h = h, w
        texts.append(TextRect(fp_index=fp_idx, cx=(box[0] + box[2]) // 2,
                              cy=(box[1] + box[3]) // 2, width=w, height=h,
                              angle=angle, side=side))

    # References stay out of footprint keep-outs on their own side only.
    by_side: Dict[str, List[Box]] = {'F': [], 'B': []}
    for ko in board_model.keepouts:
        if not getattr(ko, 'no_footprints', True):
            continue
        for side in (getattr(ko, 'sides', None) or ('F', 'B')):
            by_side.setdefault(side, []).append((ko.xmin, ko.ymin, ko.xmax, ko.ymax))
    board_bbox = (board_model.outline_xmin, board_model.outline_ymin,
                  board_model.outline_xmax, board_model.outline_ymax)
    return SilkscreenModel(texts=texts, fp_bboxes=fp_bboxes, keepouts=[],
                           board_bbox=board_bbox, pad_boxes=pad_boxes, fixed_texts=fixed,
                           keepouts_by_side=by_side)


def apply_silkscreen(board, silk_model: SilkscreenModel,
                     new_positions: Sequence[Tuple[int, ...]],
                     board_model: Optional[BoardModel] = None) -> int:
    """Write the references' new positions and angles back to pcbnew.
    Accepts (cx, cy) or (cx, cy, angle) per text. Returns the count changed."""
    import pcbnew

    kfps = list(board.GetFootprints())
    by_uuid = {}
    by_ref = {}
    for fp in kfps:
        try:
            by_uuid[str(fp.m_Uuid.AsString())] = fp
        except Exception:
            pass
        by_ref.setdefault(fp.GetReference(), fp)

    moved = 0
    for text, pos in zip(silk_model.texts, new_positions):
        new_cx, new_cy = pos[0], pos[1]
        angle = pos[2] if len(pos) > 2 else None
        if new_cx == text.cx and new_cy == text.cy and (angle is None or angle == text.angle):
            continue

        kfp = None
        if board_model is not None and text.fp_index < len(board_model.footprints):
            mfp = board_model.footprints[text.fp_index]
            kfp = by_uuid.get(mfp.uuid) if mfp.uuid else by_ref.get(mfp.reference)
        elif text.fp_index < len(kfps):
            kfp = kfps[text.fp_index]
        if kfp is None:
            continue

        ref_text = kfp.Reference()
        if angle is not None:
            try:
                ref_text.SetKeepUpright(True)
            except Exception:
                pass
            ref_text.SetTextAngleDegrees(float(angle))
        ref_text.SetPosition(pcbnew.VECTOR2I(int(new_cx), int(new_cy)))
        # The anchor is not always the centre of the drawn text (justification,
        # font metrics): shift so that the bounding box is centred where asked.
        try:
            bb = ref_text.GetBoundingBox()
            dx = int(new_cx) - (bb.GetX() + bb.GetWidth() // 2)
            dy = int(new_cy) - (bb.GetY() + bb.GetHeight() // 2)
            if dx or dy:
                p = ref_text.GetPosition()
                ref_text.SetPosition(pcbnew.VECTOR2I(p.x + dx, p.y + dy))
        except Exception:
            pass
        moved += 1

    if moved > 0:
        try:
            pcbnew.Refresh()
        except Exception:
            pass
    return moved
