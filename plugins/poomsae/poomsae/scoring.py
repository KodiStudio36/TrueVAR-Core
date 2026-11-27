"""
scoring.py — the ONE place poomsae points are calculated.

Pure functions, no I/O. Everything that shows or stores a poomsae number —
the operator panel and external screen (via /state → "scores"), the saved
match result (results.py), the category ranking (standings.py) and the
livestream scoreboard (live.py) — gets it from here. Nothing else does
its own maths; if a rule changes, it changes in this file only.

The rules
  • Judge rows = every registered judge slot ∪ every judge that has data.
    A judge with nothing for a round counts as full marks (4.0 / 6.0).
  • "trim_high_low" (3+ judges): the single highest and single lowest
    ACCURACY are left out, and — separately — the single highest and single
    lowest PRESENTATION. A judge is never dropped as a whole; his accuracy
    can be trimmed while his presentation still counts, and the other way
    round.
        round average = avg(kept accuracy) + avg(kept presentation)
  • Para and freestyle (TOTAL_BASED_MODES) score from the judge app's own
    Total (technical wheels / deductions page are already inside it), so
    there the whole judge is trimmed by Total, as before.
  • round points = round average + restart deduction + penalties, 3 decimals
  • match score = mean of the round points (one poomsae in a cut-off
    preliminary / semi-final, two in the final), 3 decimals
  • disqualified athlete: match score 0 and he loses a side-by-side match
    whatever the judges gave; the judges' marks are kept for the record.
  • side by side: higher score wins; tie → higher presentation, then the
    higher total of ALL judges' totals (trimmed included) — the WT order;
    still equal → no winner (decided on the floor).
"""
from typing import Iterable, Optional

FULL_ACCURACY = 4.0
FULL_PRESENTATION = 6.0
ROUND_KEYS = ("poomsae1", "poomsae2")
TRIM_HIGH_LOW = "trim_high_low"
MIN_JUDGES_TO_TRIM = 3
TOTAL_BASED_MODES = ("para", "freestyle")

# How a side-by-side match was decided (kyorugi's win_type slot).
WIN_TYPES = (("finalScore", "PTS"), ("presentation", "PRS"), ("totalAll", "ALL"))
WIN_TYPE_DISQUALIFICATION = "DSQ"


# ── Small helpers ───────────────────────────────────────────────────────
def as_map(value) -> dict:
    """RTDB hands a node keyed by small integers back as a list — turn it
    into a dict keyed by the (stringified) index, skipping the None padding."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(i): v for i, v in enumerate(value) if v is not None}
    return {}


def num(value, default=None):
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default      # NaN → default


def judge_numbers(registry, judges_map: dict) -> list[int]:
    """Registered judge slots ∪ judges with data, ascending. `registry` is a
    dict keyed by judge number (or any iterable of numbers)."""
    numbers = set()
    for key in list(registry or []) + list(judges_map or {}):
        try:
            n = int(key)
        except (TypeError, ValueError):
            continue
        if n >= 1:
            numbers.add(n)
    return sorted(numbers)


def is_disqualified(corner) -> bool:
    return isinstance(corner, dict) and corner.get("disqualified") is True


def trimmed_positions(values: list[float], method: str) -> set[int]:
    """Positions in `values` that are left out: the one lowest and the one
    highest — only with trim_high_low and at least 3 values. Ties are broken
    by position (stable), so every consumer drops the same judge."""
    if method != TRIM_HIGH_LOW or len(values) < MIN_JUDGES_TO_TRIM:
        return set()
    order = sorted(range(len(values)), key=lambda i: values[i])
    return {order[0], order[-1]}


def _mean(values: list[float], default: float) -> float:
    return sum(values) / len(values) if values else default


def round_adjustments(corner: dict, round_key: str, current_round_key: Optional[str] = None) -> tuple[float, float]:
    """(restart, penalty) of one poomsae: roundAdjustments/{round_key}; for
    match_data written before roundAdjustments existed, the flat
    roundDeduction / roundPenalty of the poomsae that is on the floor."""
    adj = as_map(as_map(corner.get("roundAdjustments")).get(round_key))
    restart, penalty = num(adj.get("restart"), 0.0), num(adj.get("penalty"), 0.0)
    if not adj and round_key == current_round_key:
        restart, penalty = num(corner.get("roundDeduction"), 0.0), num(corner.get("roundPenalty"), 0.0)
    return restart, penalty


# ── One poomsae of one athlete ──────────────────────────────────────────
def round_result(corner, registry, round_key: str, method: str, mode: str = "solo",
                 current_round_key: Optional[str] = None) -> dict:
    corner = corner if isinstance(corner, dict) else {}
    judges_map = as_map(corner.get("judges"))

    entries = []
    for n in judge_numbers(registry, judges_map):
        data = as_map(judges_map.get(str(n))).get(round_key)
        started = isinstance(data, dict)
        data = data if started else {}
        accuracy = num(data.get("accuracy"), FULL_ACCURACY)
        presentation = num(data.get("presentation"), FULL_PRESENTATION)
        entries.append({
            "judge": n,
            "accuracy": accuracy,
            "presentation": presentation,
            "total": num(data.get("total"), accuracy + presentation),
            "started": started,                       # sent anything for this poomsae
            "default": not started,                   # → counted as full marks
            "submitted": data.get("submitted") is True,
            "edited": data.get("editedByOperator") is True,
            "trimAccuracy": False,                    # left out of the accuracy average
            "trimPresentation": False,                # left out of the presentation average
            "trimmed": False,                         # whole judge left out (para / freestyle only)
        })

    total_based = mode in TOTAL_BASED_MODES
    if total_based:
        for i in trimmed_positions([e["total"] for e in entries], method):
            entries[i]["trimAccuracy"] = entries[i]["trimPresentation"] = entries[i]["trimmed"] = True
    else:
        for i in trimmed_positions([e["accuracy"] for e in entries], method):
            entries[i]["trimAccuracy"] = True
        for i in trimmed_positions([e["presentation"] for e in entries], method):
            entries[i]["trimPresentation"] = True

    acc_kept = [e for e in entries if not e["trimAccuracy"]]
    pres_kept = [e for e in entries if not e["trimPresentation"]]
    avg_accuracy = _mean([e["accuracy"] for e in acc_kept], FULL_ACCURACY)
    avg_presentation = _mean([e["presentation"] for e in pres_kept], FULL_PRESENTATION)
    sum_accuracy = sum(e["accuracy"] for e in acc_kept)
    sum_presentation = sum(e["presentation"] for e in pres_kept)
    if total_based:
        average = _mean([e["total"] for e in acc_kept], FULL_ACCURACY + FULL_PRESENTATION)
        sum_total = sum(e["total"] for e in acc_kept)
    else:
        average = avg_accuracy + avg_presentation
        sum_total = sum_accuracy + sum_presentation

    restart, penalty = round_adjustments(corner, round_key, current_round_key)
    return {
        "roundKey": round_key,
        "judges": entries,
        "judgeCount": len(entries),
        "submittedCount": sum(1 for e in entries if e["submitted"]),
        "allSubmitted": bool(entries) and all(e["submitted"] for e in entries),
        "performed": any(e["started"] for e in entries),   # a round nobody scored wasn't performed
        "trimmedAccuracy": [e["judge"] for e in entries if e["trimAccuracy"]],
        "trimmedPresentation": [e["judge"] for e in entries if e["trimPresentation"]],
        "avgAccuracy": round(avg_accuracy, 3),
        "avgPresentation": round(avg_presentation, 3),
        "average": round(average, 3),
        "sumAccuracy": round(sum_accuracy, 3),
        "sumPresentation": round(sum_presentation, 3),
        "sumTotal": round(sum_total, 3),
        "restart": restart,
        "penalty": penalty,
        "points": round(average + restart + penalty, 3),
        "totalAll": round(sum(e["total"] for e in entries), 3),    # tie-break: trimmed judges included
    }


# ── A whole match of one athlete ────────────────────────────────────────
def corner_result(corner, registry, round_keys: Iterable[str], method: str, mode: str = "solo",
                  current_round_key: Optional[str] = None) -> dict:
    corner = corner if isinstance(corner, dict) else {}
    rounds = [round_result(corner, registry, key, method, mode, current_round_key) for key in round_keys]
    n = len(rounds) or 1
    disqualified = is_disqualified(corner)
    raw_score = round(sum(r["points"] for r in rounds) / n, 3)
    return {
        "disqualified": disqualified,
        "rounds": rounds,
        "finalScore": 0.0 if disqualified else raw_score,
        "rawScore": raw_score,                    # what the judges gave, disqualification ignored
        "presentation": round(sum(r["avgPresentation"] for r in rounds) / n, 3),
        "totalAll": round(sum(r["totalAll"] for r in rounds), 3),
        "judgeNumbers": judge_numbers(registry, as_map(corner.get("judges"))),
    }


def decide_winner(blue: Optional[dict], red: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    """(winner, win_type) of a side-by-side match from two corner results
    (finalScore, presentation, totalAll, disqualified). (None, None) for a
    solo performance, a tie, or when both are disqualified."""
    if not blue or not red:
        return None, None
    blue_dq, red_dq = bool(blue.get("disqualified")), bool(red.get("disqualified"))
    if blue_dq != red_dq:
        return ("red" if blue_dq else "blue"), WIN_TYPE_DISQUALIFICATION
    if blue_dq:
        return None, None
    for key, win_type in WIN_TYPES:
        if blue[key] != red[key]:
            return ("blue" if blue[key] > red[key] else "red"), win_type
    return None, None