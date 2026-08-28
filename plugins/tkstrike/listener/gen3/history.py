"""Append-only timeline of scoreboard changes for a single fight.

The IVR replay needs to answer "what was the score/round/clock at this
point in the video" — this is what makes that possible. One
ScoreboardHistory instance covers one fight, from START_FIGHT to WIN. Every
time the parser produces a *materially different* score/penalty/round/clock
state, we record one ScoreEvent with the elapsed time (seconds since the
fight started).

The clock deliberately rides the same timeline as scores rather than being
modeled separately with start/stop interval math: the hardware already
emits one clk tick per second while running and simply stops emitting while
stopped, so recording each tick as a discrete event and looking up "last
value at or before time T" reconstructs the exact displayed clock —
including pauses — with zero interpolation.

This class is transport-agnostic: it doesn't know about the event bus, the
websocket to the IVR app, or PyQt. Something else (TkStrikeListenerPlugin)
feeds it data and forwards its output.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class ScoreEvent:
    t: float             # seconds since fight start (monotonic, always increasing)
    round: int
    clk: str              # clock display exactly as sent by the hardware, e.g. "01:45"
    blue_points: int
    blue_penalties: int
    red_points: int
    red_penalties: int


class ScoreboardHistory:
    """Timeline for one fight. Create on NEW_FIGHT, start() on START_FIGHT,
    record() on every scoreboard.stable_update, save() on WIN.
    """

    def __init__(self, match_id):
        self.match_id = match_id
        self.fight_start: Optional[float] = None
        self.events: list[ScoreEvent] = []
        self._last_snapshot: Optional[tuple] = None

    def start(self, at: Optional[float] = None) -> None:
        """Call when scoreboard.START_FIGHT fires. This is t=0 for the timeline."""
        self.fight_start = at if at is not None else time.time()
        self.events = []
        self._last_snapshot = None

    def record(self, data: dict, at: Optional[float] = None) -> Optional[dict]:
        """Feed the full parser state dict (same shape passed to on_stable_update
        and available as parser.data on every clock tick).

        Returns the new ScoreEvent as a dict if the score, penalties, round,
        OR clock actually changed (so the caller can forward just the
        delta), or None if nothing changed since the last call.
        """
        if self.fight_start is None:
            # Fight hasn't officially started yet (pre-fight scoreboard tests,
            # or a NEW_FIGHT that hasn't reached START_FIGHT). Nothing to anchor to.
            return None

        round_idx = max(0, min(data["round"] - 1, 2))
        blue = data["blue"]["points"][round_idx]
        red = data["red"]["points"][round_idx]

        snapshot = (
            data["round"], data.get("clk", ""),
            int(blue["points"]), int(blue["penalties"]),
            int(red["points"]), int(red["penalties"]),
        )
        if snapshot == self._last_snapshot:
            return None

        self._last_snapshot = snapshot
        now = at if at is not None else time.time()
        event = ScoreEvent(
            t=round(now - self.fight_start, 3),
            round=snapshot[0], clk=snapshot[1],
            blue_points=snapshot[2], blue_penalties=snapshot[3],
            red_points=snapshot[4], red_penalties=snapshot[5],
        )
        self.events.append(event)
        return asdict(event)

    def state_at(self, t: float) -> Optional[dict]:
        """Last known score at or before elapsed time t (seconds since fight start)."""
        result = None
        for event in self.events:
            if event.t <= t:
                result = event
            else:
                break
        return asdict(result) if result else None

    def to_list(self) -> list[dict]:
        return [asdict(e) for e in self.events]

    def save(self, directory: str) -> Path:
        Path(directory).mkdir(parents=True, exist_ok=True)
        path = Path(directory) / f"{self.match_id}_scoreboard.json"
        path.write_text(json.dumps(self.to_list(), indent=2))
        return path

    @classmethod
    def load(cls, path: str, match_id) -> "ScoreboardHistory":
        history = cls(match_id)
        history.fight_start = 0.0
        raw = json.loads(Path(path).read_text())
        history.events = [ScoreEvent(**e) for e in raw]
        if history.events:
            last = history.events[-1]
            history._last_snapshot = (
                last.round, last.clk,
                last.blue_points, last.blue_penalties,
                last.red_points, last.red_penalties,
            )
        return history