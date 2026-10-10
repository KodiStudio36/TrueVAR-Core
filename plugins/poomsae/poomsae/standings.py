"""
Category Results — pure scoring / ranking helpers (no I/O), next to
plugin.py like results.py.

Scores are computed from match_data exactly the way the operator panel
shows them (panel.html buildJudgesTable): every registered judge counts,
a judge with no data for the round counts as full marks (4.0 / 6.0),
"trim_high_low" drops the single highest and lowest Total when there are
3+ judges, then avg Total + restart + penalties. A round where NO judge
sent anything counts as not performed (None), never as 10.0.

A match's score = mean of its performed poomsae (one in a cut-off
preliminary / semi-final, two in the final).

Ranking ties (WT order): higher score, then higher presentation, then
higher sum of ALL judges' totals.
"""
from typing import Iterable, Optional

FULL_ACCURACY = 4.0
FULL_PRESENTATION = 6.0
NEXT_CUT_OFF_PHASE = {"PR": "SF", "SF": "F"}


def normalize_map(value) -> dict:
    """RTDB hands back small-integer-keyed nodes as lists — see
    plugin._normalize_firebase_map."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(i): v for i, v in enumerate(value) if v is not None}
    return {}


def _num(value, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def registry_numbers(registry) -> set[int]:
    return {int(k) for k in normalize_map(registry) if str(k).isdigit() and int(k) >= 1}


# ── Scores ──────────────────────────────────────────────────────────────
def round_score(corner: Optional[dict], round_key: str, judges_registry: Iterable[int], scoring_method: str) -> Optional[dict]:
    judges = normalize_map((corner or {}).get("judges"))
    numbers = set(judges_registry)
    for k in judges:
        if str(k).isdigit() and int(k) >= 1:
            numbers.add(int(k))
    if not numbers:
        return None

    entries = []
    has_data = False
    for n in sorted(numbers):
        data = judges.get(str(n))
        rd = data.get(round_key) if isinstance(data, dict) else None
        if isinstance(rd, dict):
            has_data = True
            acc = _num(rd.get("accuracy"), FULL_ACCURACY)
            pres = _num(rd.get("presentation"), FULL_PRESENTATION)
            tot = _num(rd.get("total"), acc + pres)
        else:
            acc, pres, tot = FULL_ACCURACY, FULL_PRESENTATION, FULL_ACCURACY + FULL_PRESENTATION
        entries.append((n, acc, pres, tot))
    if not has_data:
        return None

    included = entries
    if scoring_method == "trim_high_low" and len(entries) >= 3:
        ordered = sorted(entries, key=lambda e: e[3])
        dropped = {ordered[0][0], ordered[-1][0]}
        included = [e for e in entries if e[0] not in dropped]

    count = len(included) or 1
    avg_acc = sum(e[1] for e in included) / count
    avg_pres = sum(e[2] for e in included) / count
    avg_tot = sum(e[3] for e in included) / count

    adj = normalize_map(normalize_map((corner or {}).get("roundAdjustments")).get(round_key))
    restart = _num(adj.get("restart"), 0.0)
    penalty = _num(adj.get("penalty"), 0.0)
    return {
        "score": round(avg_tot + restart + penalty, 3),
        "accuracy": round(avg_acc, 3),
        "presentation": round(avg_pres, 3),
        "allTotal": round(sum(e[3] for e in entries), 3),
        "restart": restart,
        "penalty": penalty,
    }


def match_score(corner: Optional[dict], round_keys: list[str], judges_registry: Iterable[int], scoring_method: str) -> Optional[dict]:
    registry = set(judges_registry)
    performed = [r for r in (round_score(corner, k, registry, scoring_method) for k in round_keys) if r]
    if not performed:
        return None
    n = len(performed)
    return {
        "score": round(sum(r["score"] for r in performed) / n, 3),
        "accuracy": round(sum(r["accuracy"] for r in performed) / n, 3),
        "presentation": round(sum(r["presentation"] for r in performed) / n, 3),
        "allTotal": round(sum(r["allTotal"] for r in performed), 3),
        "rounds": [r["score"] for r in performed],
        "complete": n == len(round_keys),
    }


def rank_key(row: dict) -> tuple:
    return (-_num(row.get("score"), 0.0), -_num(row.get("presentation"), 0.0), -_num(row.get("allTotal"), 0.0))


def duel_winner(blue: Optional[dict], red: Optional[dict]) -> Optional[str]:
    if not blue or not red or blue.get("score") is None or red.get("score") is None:
        return None
    kb, kr = rank_key(blue), rank_key(red)
    if kb < kr:
        return "blue"
    if kr < kb:
        return "red"
    return None


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
            "isCurrent": m["id"] == current_id,
        })
    scored = sorted([r for r in rows if r["score"] is not None], key=rank_key)
    unscored = [r for r in rows if r["score"] is None]
    _assign_ranks(scored, rank_key)
    for r in scored:
        r["qualified"] = bool(qualify_count) and r["rank"] <= qualify_count
        r["medal"] = r["rank"] if phase == "F" and r["rank"] <= 3 else None
    for r in unscored:
        r.update({"rank": None, "qualified": False, "medal": None})
    ordered = scored + unscored
    # The athlete who just performed starts at the very bottom and rises
    # to their place.
    current = [r["key"] for r in ordered if r["isCurrent"]]
    start_order = [r["key"] for r in ordered if not r["isCurrent"]] + current
    return {
        "system": "cut_off",
        "rows": ordered,
        "startOrder": start_order,
        "qualifyCount": qualify_count,
        "performed": len(scored),
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
                           "score": (corners.get(color) or {}).get("score")}
        out.append(item)
    return {
        "system": "single_elimination",
        "matches": out,
        "performed": sum(1 for i in out if i["played"]),
        "total": len(out),
    }