"""
Majority deduction detector.

The live accuracy is an AVERAGE, so a single judge pressing -0.3 only moves
it by 0.1 (3 judges). Flashing every movement would be noise. Instead this
watches each judge's own accuracy and reports a deduction only when a
majority of the judges make the same call (-0.1 or -0.3) within
`window` seconds of each other — that's the moment the audience should
see "-0.3".

Fed with every live payload (live.shape_live_payload) — needs per corner
`judge_accuracy` ({judge: value}) and `judges` (count).
"""
import time

FULL_ACCURACY = 4.0
AMOUNTS = (0.3, 0.1)       # checked in this order
EPS = 1e-6


def split_delta(delta: float) -> list[float]:
    """A poll can merge two taps (0.3 + 0.1 = 0.4): break a drop back into
    0.3s and 0.1s."""
    tenths = round(delta / 0.1)
    return [0.3] * (tenths // 3) + [0.1] * (tenths % 3)


class MajorityDeductions:
    def __init__(self):
        self._key = None
        self._last: dict[str, dict[str, float]] = {}       # corner → judge → accuracy
        self._pending: dict[str, dict[str, list]] = {}     # corner → judge → [(t, amount)]

    def reset(self):
        self._key = None
        self._last, self._pending = {}, {}

    def update(self, data: dict, window: float, now: float | None = None) -> list[dict]:
        """Returns the deductions to flash now: [{"corner", "amount"}]."""
        now = time.monotonic() if now is None else now
        performing = data.get("round_key") and not data.get("result") and data.get("stage") in ("poomsae_1", "poomsae_2")
        if not performing:
            self.reset()
            return []

        key = (data.get("match_id"), data.get("round_key"))
        priming = key != self._key
        if priming:
            # New poomsae (or the extension just started mid-performance):
            # take the current values as the baseline, flash nothing.
            self.reset()
            self._key = key

        flashes = []
        for color in ("blue", "red"):
            corner = data.get(color) or {}
            accs = corner.get("judge_accuracy") or {}
            if not accs:
                continue
            judges = int(corner.get("judges") or len(accs) or 1)
            majority = judges // 2 + 1
            last = self._last.setdefault(color, {})
            pending = self._pending.setdefault(color, {})

            for judge, acc in accs.items():
                if judge not in last:
                    last[judge] = acc if priming else FULL_ACCURACY
                delta = round(last[judge] - acc, 2)
                last[judge] = acc
                if delta > EPS:
                    pending.setdefault(judge, []).extend((now, a) for a in split_delta(delta))
                elif delta < -EPS:
                    # Judge took a deduction back (or Restart cleared the round).
                    pending.pop(judge, None)

            for judge in list(pending):
                pending[judge] = [(t, a) for t, a in pending[judge] if now - t <= window]
                if not pending[judge]:
                    del pending[judge]

            for amount in AMOUNTS:
                agreeing = [j for j, events in pending.items() if any(abs(a - amount) < EPS for _, a in events)]
                if len(agreeing) < majority:
                    continue
                for j in agreeing:   # each agreeing judge's call is used up
                    events = pending[j]
                    events.pop(next(i for i, (_, a) in enumerate(events) if abs(a - amount) < EPS))
                    if not events:
                        del pending[j]
                flashes.append({"corner": color, "amount": amount})
        return flashes