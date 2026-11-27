"""
Category Results — ranking helpers (no I/O), next to plugin.py like results.py.

No scoring maths lives here: a round's / match's score comes from scoring.py
(the same module the panel, screen, saved result and livestream use). A
round where NO judge sent anything counts as not performed (score None),
never as 10.0. A match's score = mean of its performed poomsae (one in a
cut-off preliminary / semi-final, two in the final).

A disqualified athlete scores 0, is listed after everyone who has a rank
(never ranked, never qualified, never a medal) and loses a side-by-side match.

Ranking ties (WT order): higher score, then higher presentation, then
higher sum of ALL judges' totals.
"""
from typing import Iterable, Optional

try:  # registered by plugin.py (see _load_local_module)
    import poomsae_control_scoring as scoring
except ImportError:  # loaded on its own (tests): pick up the sibling file
    import importlib.util
    import sys
    from pathlib import Path
    _spec = importlib.util.spec_from_file_location("poomsae_control_scoring", Path(__file__).with_name("scoring.py"))
    scoring = importlib.util.module_from_spec(_spec)
    sys.modules["poomsae_control_scoring"] = scoring
    _spec.loader.exec_module(scoring)

FULL_ACCURACY = scoring.FULL_ACCURACY
FULL_PRESENTATION = scoring.FULL_PRESENTATION
NEXT_CUT_OFF_PHASE = {"PR": "SF", "SF": "F"}
normalize_map = scoring.as_map


def _num(value, default: float) -> float:
    return scoring.num(value, default)


def registry_numbers(registry) -> set[int]:
    return {int(k) for k in normalize_map(registry) if str(k).isdigit() and int(k) >= 1}


# ── Scores ──────────────────────────────────────────────────────────────
def round_score(corner: Optional[dict], round_key: str, judges_registry: Iterable[int], scoring_method: str,
                mode: str = "solo") -> Optional[dict]:
    """One poomsae's score, or None when no judge sent anything for it."""
    r = scoring.round_result(corner, set(judges_registry), round_key, scoring_method, mode)
    if not r["performed"]:
        return None
    return {
        "score": r["points"],
        "accuracy": r["avgAccuracy"],
        "presentation": r["avgPresentation"],
        "allTotal": r["totalAll"],
        "restart": r["restart"],
        "penalty": r["penalty"],
    }


def match_score(corner: Optional[dict], round_keys: list[str], judges_registry: Iterable[int], scoring_method: str,
                mode: str = "solo") -> Optional[dict]:
    """The athlete's score over the performed poomsae in `round_keys`; None
    when none was performed — unless he is disqualified (score 0)."""
    registry = set(judges_registry)
    performed = [r for r in (round_score(corner, k, registry, scoring_method, mode) for k in round_keys) if r]
    disqualified = scoring.is_disqualified(corner)
    if not performed and not disqualified:
        return None
    n = len(performed) or 1
    score = round(sum(r["score"] for r in performed) / n, 3)
    return {
        "score": 0.0 if disqualified else score,
        "accuracy": round(sum(r["accuracy"] for r in performed) / n, 3),
        "presentation": round(sum(r["presentation"] for r in performed) / n, 3),
        "allTotal": round(sum(r["allTotal"] for r in performed), 3),
        "rounds": [r["score"] for r in performed],
        "complete": len(performed) == len(round_keys),
        "disqualified": True if disqualified else None,
    }


def rank_key(row: dict) -> tuple:
    return (-_num(row.get("score"), 0.0), -_num(row.get("presentation"), 0.0), -_num(row.get("allTotal"), 0.0))


def duel_winner(blue: Optional[dict], red: Optional[dict]) -> Optional[str]:
    """Winner of a side-by-side match from two match_score records — the
    same decision as the saved result (scoring.decide_winner)."""
    if not blue or not red:
        return None
    if not (blue.get("disqualified") or red.get("disqualified")) \
            and (blue.get("score") is None or red.get("score") is None):
        return None

    def view(rec: dict) -> dict:
        return {"finalScore": _num(rec.get("score"), 0.0), "presentation": _num(rec.get("presentation"), 0.0),
                "totalAll": _num(rec.get("allTotal"), 0.0), "disqualified": rec.get("disqualified") is True}
    return scoring.decide_winner(view(blue), view(red))[0]


def _assign_ranks(rows: list[dict], key) -> None:
    previous, rank = object(), 0
    for i, row in enumerate(rows):
        k = key(row)
        if k != previous:
            rank, previous = i + 1, k
        row["rank"] = rank


def _identity_key(ident: Optional[dict], match_id: str, color: str) -> str:
    ident = ident or {}
    return str(ident.get("entryId") or f"{match_id}-{color}")


# ── Views ───────────────────────────────────────────────────────────────
# matches: this category's matches, queue order, already resolved:
#   {"id", "displayNumber", "displayNumberLabel", "roundPhase",
#    "blue": {entryId, name, club, country} | None, "red": … | None}
# records: matchId -> what plugin._record_category_result saved.
def build_category_view(system: str, phase: Optional[str], matches: list[dict], records: dict,
                        current_match_id: Optional[str], next_phase_count: Optional[int]) -> dict:
    if system == "cut_off":
        return _cut_off_view(phase, matches, records, current_match_id, next_phase_count)
    if system == "round_robin":
        return _round_robin_view(matches, records, current_match_id)
    return _single_elimination_view(phase, matches, records, current_match_id)


def _cut_off_view(phase, matches, records, current_id, qualify_count) -> dict:
    rows = []
    for m in matches:
        if m.get("roundPhase") != phase:
            continue
        ident = m.get("blue") or {}
        rec = ((records.get(m["id"]) or {}).get("corners") or {}).get("blue") or {}
        rows.append({
            "key": _identity_key(ident, m["id"], "blue"),
            "matchId": m["id"],
            "displayNumberLabel": m.get("displayNumberLabel"),
            "name": ident.get("name") or rec.get("name"),
            "club": ident.get("club") or rec.get("club"),
            "country": ident.get("country") or rec.get("country"),
            "score": rec.get("score"),
            "presentation": rec.get("presentation"),
            "allTotal": rec.get("allTotal"),
            "rounds": rec.get("rounds"),
            "disqualified": rec.get("disqualified") is True,
            "isCurrent": m["id"] == current_id,
        })
    # Disqualified athletes are done but never ranked: after everyone with a
    # rank, before those who still have to perform.
    disqualified = [r for r in rows if r["disqualified"]]
    scored = sorted([r for r in rows if r["score"] is not None and not r["disqualified"]], key=rank_key)
    unscored = [r for r in rows if r["score"] is None and not r["disqualified"]]
    _assign_ranks(scored, rank_key)
    for r in scored:
        r["qualified"] = bool(qualify_count) and r["rank"] <= qualify_count
        r["medal"] = r["rank"] if phase == "F" and r["rank"] <= 3 else None
    for r in disqualified + unscored:
        r.update({"rank": None, "qualified": False, "medal": None})
    ordered = scored + disqualified + unscored
    # The athlete who just performed starts at the very bottom and rises
    # to their place.
    current = [r["key"] for r in ordered if r["isCurrent"]]
    start_order = [r["key"] for r in ordered if not r["isCurrent"]] + current
    return {
        "system": "cut_off",
        "rows": ordered,
        "startOrder": start_order,
        "qualifyCount": qualify_count,
        "performed": len(scored) + len(disqualified),
        "total": len(rows),
    }


def _round_robin_table(rr_matches, records, skip_match_id) -> list[dict]:
    table, order = {}, []
    for m in rr_matches:
        for color in ("blue", "red"):
            ident = m.get(color)
            if not ident:
                continue
            key = _identity_key(ident, m["id"], color)
            if key not in table:
                table[key] = {"key": key, "name": ident.get("name"), "club": ident.get("club"),
                              "country": ident.get("country"), "wins": 0, "losses": 0, "played": 0,
                              "points": 0.0, "isCurrent": False}
                order.append(key)
    for m in rr_matches:
        if m["id"] == skip_match_id:
            continue
        rec = records.get(m["id"])
        if not rec:
            continue
        winner = rec.get("winner")
        corners = rec.get("corners") or {}
        for color in ("blue", "red"):
            ident = m.get(color)
            score = (corners.get(color) or {}).get("score")
            if not ident or score is None:
                continue
            row = table[_identity_key(ident, m["id"], color)]
            row["played"] += 1
            row["points"] = round(row["points"] + float(score), 3)
            if winner == color:
                row["wins"] += 1
            elif winner:
                row["losses"] += 1
    rows = [table[k] for k in order]
    rows.sort(key=lambda r: (-r["wins"], -r["points"]))
    return rows


def _round_robin_view(matches, records, current_id) -> dict:
    rr = [m for m in matches if str(m.get("roundPhase") or "").startswith("RR")]
    before = _round_robin_table(rr, records, current_id)
    rows = _round_robin_table(rr, records, None)
    current = next((m for m in rr if m["id"] == current_id), None)
    current_keys = {_identity_key(current.get(c), current["id"], c) for c in ("blue", "red") if current and current.get(c)}
    key = lambda r: (-r["wins"], -r["points"])
    _assign_ranks(rows, key)
    for r in rows:
        r["isCurrent"] = r["key"] in current_keys
        r["score"] = r["points"] if r["played"] else None
        r["medal"] = None
        r["qualified"] = False
    return {
        "system": "round_robin",
        "rows": rows,
        "startOrder": [r["key"] for r in before],
        "performed": sum(1 for m in rr if m["id"] in records),
        "total": len(rr),
    }


def _single_elimination_view(phase, matches, records, current_id) -> dict:
    out = []
    for m in matches:
        if m.get("roundPhase") != phase:
            continue
        rec = records.get(m["id"]) or {}
        corners = rec.get("corners") or {}
        item = {"matchId": m["id"], "displayNumberLabel": m.get("displayNumberLabel"),
                "isCurrent": m["id"] == current_id, "played": bool(rec), "winner": rec.get("winner")}
        for color in ("blue", "red"):
            ident = m.get(color) or {}
            item[color] = {"name": ident.get("name"), "club": ident.get("club"), "country": ident.get("country"),
                           "score": (corners.get(color) or {}).get("score"),
                           "disqualified": (corners.get(color) or {}).get("disqualified") is True}
        out.append(item)
    return {
        "system": "single_elimination",
        "matches": out,
        "performed": sum(1 for i in out if i["played"]),
        "total": len(out),
    }