"""Learning the user's taste (a small reinforcement-learning loop).

Two things are learned and kept in a JSON file in KiCad's user settings
folder (nothing leaves the computer):

* weights — how much each aesthetic criterion matters to the user; they
  shape the score of the aesthetic check;
* a policy — the knobs place-news turns when it places and routes: the
  wirelength budget of the row/column tidy-up, the budget for turning parts
  of one type the same way, Freerouting's via cost.

Rewards come from the user:

* explicit feedback after a check: 👍 / 👎, optionally "what bothers me";
* corrections: when a board place-news produced is later edited by hand,
  the criteria the user improved gain weight, those the user let degrade
  lose some, and the policy that produced the board is rewarded if the user
  kept it as it was, penalised as much as the user had to fix.

The policy explores: each run perturbs its parameters a little (Gaussian
noise in log space) and the reward moves the parameters along the
perturbation that was tried (an evolution-strategies / REINFORCE update).
No neural network: a handful of numbers, so it adapts after a few boards.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from .aesthetic_check import CRITERIA, weighted_score

STORE_NAME = 'place-news-aesthetics.json'

# Initial weights: what the user asked for first (rows, readable references,
# fewer vias) matters more; orientation was not asked for; a board larger
# than needed is often imposed by the enclosure.
DEFAULT_WEIGHTS: Dict[str, float] = {
    'alignment': 1.5, 'orientation': 0.5, 'ref_readable': 1.5, 'ref_position': 1.5,
    'ref_clear': 1.5, 'angles': 1.0, 'acute': 1.0, 'jogs': 1.0, 'detours': 1.0,
    'pad_exits': 1.0,
    'vias': 1.5, 'crossings': 1.0, 'drill_sizes': 0.5, 'small_drills': 0.5,
    'fine_tracks': 0.5, 'two_sides': 0.5, 'inner_layers': 0.8, 'layer_count': 1.0,
    'board_area': 0.3,
}
W_MIN, W_MAX = 0.05, 5.0

# Policy parameters (log space) and their defaults.
DEFAULT_POLICY: Dict[str, float] = {
    'align_budget': math.log(0.03),     # share of wirelength the row tidy-up may spend
    'orient_budget': math.log(0.02),    # same for turning parts like their siblings
    'untangle_budget': math.log(0.02),  # same for swapping parts to uncross the ratsnest
    'via_cost': 0.0,                    # Freerouting via cost = base * exp(.)
}
POLICY_RANGE: Dict[str, Tuple[float, float]] = {
    'align_budget': (math.log(0.002), math.log(0.15)),
    'orient_budget': (math.log(0.002), math.log(0.10)),
    'untangle_budget': (math.log(0.002), math.log(0.10)),
    'via_cost': (math.log(0.25), math.log(8.0)),
}
PLACEMENT_KNOBS = ('align_budget', 'orient_budget', 'untangle_budget')
ROUTING_KNOBS = ('via_cost',)

ETA_W = 0.25          # weight learning rate
ETA_P = 0.20          # policy learning rate
SIGMA = 0.30          # exploration noise (log space)
ORIENT_ON = 1.25      # orientation weight above which parts get turned alike


def default_store_path() -> str:
    """KiCad's user settings folder, else the plugin folder."""
    try:
        import pcbnew
        base = str(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath())
        if base:
            return os.path.join(base, 'place-news', STORE_NAME)
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), STORE_NAME)


def _clip(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class Learner:
    def __init__(self, path: Optional[str] = None, rng: Optional[random.Random] = None):
        self.path = path or default_store_path()
        self.rng = rng or random.Random()
        self.weights: Dict[str, float] = {k: DEFAULT_WEIGHTS.get(k, 1.0) for k in CRITERIA}
        self.policy: Dict[str, float] = dict(DEFAULT_POLICY)
        self.history: List[Dict[str, Any]] = []
        self.snapshots: Dict[str, Dict[str, Any]] = {}
        self.feedback_count = 0
        self.correction_count = 0
        self._load()

    # -- storage -------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for k, v in data.get('weights', {}).items():
            if k in self.weights:
                self.weights[k] = _clip(float(v), W_MIN, W_MAX)
        for k, v in data.get('policy', {}).items():
            if k in self.policy:
                self.policy[k] = _clip(float(v), *POLICY_RANGE[k])
        self.history = list(data.get('history', []))[-200:]
        self.snapshots = dict(data.get('snapshots', {}))
        self.feedback_count = int(data.get('feedback_count', 0))
        self.correction_count = int(data.get('correction_count', 0))

    def save(self) -> None:
        data = {'version': 1, 'weights': self.weights, 'policy': self.policy,
                'history': self.history[-200:], 'snapshots': self._recent_snapshots(),
                'feedback_count': self.feedback_count, 'correction_count': self.correction_count}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _recent_snapshots(self, keep: int = 30) -> Dict[str, Any]:
        items = sorted(self.snapshots.items(), key=lambda kv: kv[1].get('time', 0), reverse=True)
        return dict(items[:keep])

    # -- policy --------------------------------------------------------------
    def sample(self, knobs, explore: bool = True) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Parameters to use for one run, and the exploration noise used."""
        values: Dict[str, float] = {}
        eps: Dict[str, float] = {}
        for k in knobs:
            e = self.rng.gauss(0.0, SIGMA) if explore else 0.0
            theta = _clip(self.policy[k] + e, *POLICY_RANGE[k])
            eps[k] = theta - self.policy[k]
            values[k] = math.exp(theta)
        return values, eps

    @property
    def orientation_enabled(self) -> bool:
        return self.weights.get('orientation', 0.0) >= ORIENT_ON

    def _reinforce(self, eps: Dict[str, float], reward: float) -> Dict[str, float]:
        changes = {}
        for k, e in eps.items():
            if k in self.policy and e:
                new = _clip(self.policy[k] + ETA_P * reward * e / SIGMA, *POLICY_RANGE[k])
                changes[k] = new - self.policy[k]
                self.policy[k] = new
        return changes

    # -- runs and snapshots --------------------------------------------------
    def record_run(self, board_key: str, kind: str, qualities: Dict[str, Optional[float]],
                   state: Dict[str, Any], eps: Dict[str, float]) -> None:
        """Remember what place-news produced on a board (to learn from edits)."""
        if not board_key:
            return
        snap = self.snapshots.get(board_key, {})
        pending = dict(snap.get('eps', {}))
        pending.update(eps)
        self.snapshots[board_key] = {'time': time.time(), 'kind': kind, 'qualities': qualities,
                                     'state': state, 'eps': pending}

    # -- explicit feedback ---------------------------------------------------
    def feedback(self, board_key: str, qualities: Dict[str, Optional[float]], liked: bool,
                 bothered: List[str]) -> List[str]:
        """👍 / 👎 after a check. Returns what changed, in words."""
        notes: List[str] = []
        for k in bothered:
            if k in self.weights:
                old = self.weights[k]
                self.weights[k] = _clip(old * (1 + ETA_W) + ETA_W, W_MIN, W_MAX)
                notes.append(f'{CRITERIA[k][1]}: weight {old:.2f} -> {self.weights[k]:.2f}')
        if not bothered:
            for k, q in qualities.items():
                if q is None or q >= 0.7 or k not in self.weights:
                    continue
                old = self.weights[k]
                if liked:      # a weak criterion did not bother the user
                    self.weights[k] = _clip(old * (1 - ETA_W / 2), W_MIN, W_MAX)
                else:          # it probably did
                    self.weights[k] = _clip(old + ETA_W * (1 - q), W_MIN, W_MAX)
                if abs(self.weights[k] - old) > 1e-6:
                    notes.append(f'{CRITERIA[k][1]}: weight {old:.2f} -> {self.weights[k]:.2f}')
        snap = self.snapshots.get(board_key)
        if snap and snap.get('eps'):
            changes = self._reinforce(snap['eps'], 1.0 if liked else -1.0)
            snap['eps'] = {}
            notes += [f'policy {k}: {"+" if d >= 0 else ""}{d:.2f}' for k, d in changes.items() if d]
        self.feedback_count += 1
        self.history.append({'time': time.time(), 'board': os.path.basename(board_key or ''),
                             'type': 'feedback', 'liked': liked, 'bothered': bothered,
                             'score': weighted_score(qualities, self.weights)})
        self.save()
        return notes

    # -- implicit feedback: the user's corrections ----------------------------
    def learn_from_edits(self, board_key: str, qualities: Dict[str, Optional[float]],
                         state: Dict[str, Any]) -> List[str]:
        """Compare the board with what place-news produced on it; learn from
        the user's edits (once per change). Returns what changed, in words."""
        snap = self.snapshots.get(board_key) if board_key else None
        if not snap or not changed_state(snap.get('state', {}), state):
            return []
        before = snap.get('qualities', {})
        notes: List[str] = []
        for k in CRITERIA:
            qb, qa = before.get(k), qualities.get(k)
            if qb is None or qa is None or k not in self.weights:
                continue
            dq = qa - qb
            old = self.weights[k]
            if dq > 0.02:
                self.weights[k] = _clip(old + ETA_W * min(1.0, 5 * dq), W_MIN, W_MAX)
            elif dq < -0.02:
                self.weights[k] = _clip(old - ETA_W * min(1.0, 2 * -dq), W_MIN, W_MAX)
            if abs(self.weights[k] - old) > 1e-6:
                notes.append(f'{CRITERIA[k][1]}: weight {old:.2f} -> {self.weights[k]:.2f} '
                             f'(your edits {"improved" if dq > 0 else "relaxed"} it)')
        s_before = weighted_score(before, self.weights) or 0.0
        s_after = weighted_score(qualities, self.weights) or 0.0
        gain = s_after - s_before
        reward = -_clip(gain / 10.0, -1.0, 1.0) if gain > 1.0 else 0.3
        if snap.get('eps'):
            changes = self._reinforce(snap['eps'], reward)
            notes += [f'policy {k}: {"+" if d >= 0 else ""}{d:.2f}' for k, d in changes.items() if d]
        self.correction_count += 1
        self.history.append({'time': time.time(), 'board': os.path.basename(board_key),
                             'type': 'edits', 'gain': gain, 'reward': reward})
        # The edited board is the new reference: learn from each change once.
        self.snapshots[board_key] = {'time': time.time(), 'kind': 'user', 'qualities': qualities,
                                     'state': state, 'eps': {}}
        self.save()
        return notes

    def summary(self) -> List[str]:
        lines = [f'{self.feedback_count} rating(s) and {self.correction_count} set(s) of '
                 f'corrections learned from.']
        ranked = sorted(self.weights.items(), key=lambda kv: -kv[1])
        lines.append('Matters most: ' + ', '.join(f'{CRITERIA[k][1]} ({w:.1f})'
                                                 for k, w in ranked[:3]))
        lines.append(f'Row tidy-up budget: {100 * math.exp(self.policy["align_budget"]):.1f} % '
                     f'of wirelength; turn parts alike: '
                     f'{"on" if self.orientation_enabled else "off"} '
                     f'({100 * math.exp(self.policy["orient_budget"]):.1f} %); '
                     f'untangling: {100 * math.exp(self.policy["untangle_budget"]):.1f} %; '
                     f'via cost x{math.exp(self.policy["via_cost"]):.2f}')
        return lines


# ---------------------------------------------------------------------------
# Board state fingerprint (what the user may edit)
# ---------------------------------------------------------------------------

def board_state(board) -> Dict[str, Any]:
    """Positions of parts and references, and a fingerprint of the tracks."""
    parts: Dict[str, List[float]] = {}
    for fp in board.GetFootprints():
        try:
            uid = str(fp.m_Uuid.AsString())
            p = fp.GetPosition()
            r = fp.Reference()
            rp = r.GetPosition()
            parts[uid] = [p.x, p.y, round(fp.GetOrientationDegrees(), 3), rp.x, rp.y,
                          round(r.GetTextAngleDegrees(), 3)]
        except Exception:
            continue
    h = 0
    n = 0
    for t in board.GetTracks():
        try:
            a, b = t.GetStart(), t.GetEnd()
            h = (h * 1_000_003 + a.x * 7 + a.y * 13 + b.x * 17 + b.y * 19) % (1 << 61)
            n += 1
        except Exception:
            continue
    return {'parts': parts, 'tracks': [n, h]}


def changed_state(before: Dict[str, Any], after: Dict[str, Any], tol: int = 1000) -> bool:
    if before.get('tracks') != after.get('tracks'):
        return True
    pb, pa = before.get('parts', {}), after.get('parts', {})
    if set(pb) != set(pa):
        return True
    for k, vb in pb.items():
        va = pa[k]
        if any(abs(x - y) > (tol if i not in (2, 5) else 0.01) for i, (x, y) in enumerate(zip(vb, va))):
            return True
    return False
