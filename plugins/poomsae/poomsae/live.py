"""
Live scoreboard maths + the payload the livestream overlay binds to.

Pure helpers, no I/O — loaded by path from plugin.py exactly like
results.py / standings.py. The control plugin calls shape_live_payload()
with the current match_data and publishes the result on the event bus as
"poomsae.live_update"; the Poomsae Livestream Extension forwards it to the
overlay unchanged.

Live score = what the athlete would get if the judges stopped right now:
every judge starts at full marks (accuracy 4.0 + presentation 6.0) and the
value drops as they deduct. A judge that hasn't sent anything yet counts
as full marks, so the scoreboard starts at 10.00 and only ever goes down
during the performance. Restart deduction and penalties are added on top.
"""
import math
import re

FULL_ACCURACY = 4.0
FULL_PRESENTATION = 6.0

# Stage → which poomsae's scores the scoreboard shows.
PERFORMING = {"poomsae_1": "poomsae1", "poomsae_2": "poomsae2"}
SHOWING_RESULTS = {"results_1": "poomsae1", "results_2": "poomsae2"}

# Same IOC → ISO-2 table the kyorugi listener uses for flagcdn flags.
try:
    from plugins.tkstrike.listener.gen3.parser import ALPHA3_TO_ALPHA2
except Exception:
    try:
        from plugins.tkstrike.listener.gen3.parser import ALPHA3_TO_ALPHA2
    except Exception:  # pragma: no cover — flags just fall back to "un"
        ALPHA3_TO_ALPHA2 = {}


def _as_map(value) -> dict:
    """RTDB can return integer-keyed nodes as lists — see
    _normalize_firebase_map in plugin.py."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(i): v for i, v in enumerate(value) if v is not None}
    return {}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def combine(values: list[float], method: str) -> float:
    """Average of the judges' values; "trim_high_low" drops the highest and
    lowest first (only when there are at least 3 judges)."""
    if not values:
        return 0.0
    vals = sorted(values)
    if method == "trim_high_low" and len(vals) >= 3:
        vals = vals[1:-1]
    return sum(vals) / len(vals)


def flags(country) -> tuple[str, str]:
    """(flag2 for flagcdn, flag3 to print) from whatever the match doc stores."""
    code = str(country or "").strip().upper()
    if len(code) == 2:
        return code.lower(), code
    if len(code) == 3:
        return ALPHA3_TO_ALPHA2.get(code, "un"), code
    return "un", ""


# Words dropped from the category for the stream. "Para" and "Freestyle"
# always stay — they change what the scoreboard shows.
_CATEGORY_DROP = re.compile(r"\b(individuals?|pairs?|teams?|class|poomsae)\b", re.IGNORECASE)


def short_category(label: str) -> str:
    """"Individual Seniors Under 30 Class A Men" → "Seniors Under 30 A Men",
    "Freestyle Individual Poomsae Juniors Female" → "Freestyle Juniors Female"."""
    text = _CATEGORY_DROP.sub(" ", str(label or ""))
    return re.sub(r"\s{2,}", " ", text).strip(" -·")


def has_live_score(state: dict) -> bool:
    """Para has no accuracy deductions and freestyle scores completely
    differently — neither gets the live score on the scoreboard."""
    text = f"{state.get('categoryLabel') or ''} {state.get('categoryCode') or ''}".lower()
    if state.get("isPara") is True or state.get("scoringMode") == "para" or "para" in text.split():
        return False
    return "freestyle" not in text


def format_clock(seconds) -> str:
    """1:30 … 0:01, 0:00, then overtime -0:01, -0:02 … (a countdown shows
    0:00 for the second between 0 and -1, like any clock passing zero)."""
    if seconds is None:
        return ""
    value = float(seconds)
    if value > 0:
        s = math.ceil(value - 1e-6)
        return f"{s // 60}:{s % 60:02d}"
    over = int(-value + 1e-6)
    return f"-{over // 60}:{over % 60:02d}" if over > 0 else "0:00"


# Keys the judge app might use to say "I'm on the presentation screen now".
_PRESENTATION_FLAGS = (("mode", "presentation"), ("phase", "presentation"), ("step", "presentation"), ("screen", "presentation"))


def _judge_in_presentation(entry: dict) -> bool:
    """A judge counts as giving presentation marks once they say so, have
    moved presentation off the 6.0 default, or have submitted."""
    if entry.get("submitted") is True or entry.get("presentationMode") is True:
        return True
    if any(str(entry.get(k, "")).lower() == v for k, v in _PRESENTATION_FLAGS):
        return True
    pr = _num(entry.get("presentation"))
    return pr is not None and abs(pr - FULL_PRESENTATION) > 1e-6


def corner_live_score(corner: dict, round_key: str, judge_numbers: list[int], method: str, para: bool = False,
                      stored_adjustments: bool = False) -> dict:
    """stored_adjustments=True takes restart / penalty from
    roundAdjustments/{round_key} (for a poomsae that is over — Final
    Results); otherwise from roundDeduction / roundPenalty, which always
    hold the poomsae currently on the floor."""
    judges = _as_map((corner or {}).get("judges"))
    numbers = list(judge_numbers) or sorted(int(k) for k in judges if str(k).isdigit()) or [1]

    accs, pres, totals = [], [], []
    judge_accuracy: dict[str, float] = {}
    judge_scores: list[dict] = []
    started = submitted = presenting = 0
    for n in numbers:
        entry = _as_map(judges.get(str(n))).get(round_key)
        acc = pr = tot = None
        if isinstance(entry, dict):
            started += 1
            if _judge_in_presentation(entry):
                presenting += 1
            if entry.get("submitted") is True:
                submitted += 1
            acc = _num(entry.get("accuracy"))
            pr = _num(entry.get("presentation"))
            tot = _num(entry.get("total"))
        a = FULL_ACCURACY if acc is None else acc
        p = FULL_PRESENTATION if pr is None else pr
        accs.append(a)
        pres.append(p)
        judge_accuracy[str(n)] = round(a, 2)
        judge_scores.append({
            "judge": n,
            "accuracy": round(a, 2),
            "presentation": round(p, 2),
            "submitted": isinstance(entry, dict) and entry.get("submitted") is True,
            "trim_accuracy": False,
            "trim_presentation": False,
        })
        # Para judges send a technical total instead of accuracy/presentation.
        totals.append(tot if (acc is None and pr is None and tot is not None) else a + p)

    if para:
        base = combine(totals, method)
    else:
        base = combine(accs, method) + combine(pres, method)

    # trim_high_low: mark the highest and lowest value of each component so
    # the Results widget can grey out what wasn't counted.
    if method == "trim_high_low" and len(judge_scores) >= 3:
        for field in ("accuracy", "presentation"):
            order = sorted(range(len(judge_scores)), key=lambda i: judge_scores[i][field])
            judge_scores[order[0]][f"trim_{field}"] = True
            judge_scores[order[-1]][f"trim_{field}"] = True

    if stored_adjustments:
        adj = _as_map(_as_map(corner.get("roundAdjustments")).get(round_key))
        deduction = (_num(adj.get("restart")) or 0.0) + (_num(adj.get("penalty")) or 0.0)
    else:
        deduction = (_num(corner.get("roundDeduction")) or 0.0) + (_num(corner.get("roundPenalty")) or 0.0)
    score = max(0.0, base + deduction)
    return {
        "score": round(score, 3),
        "score_label": f"{score:.2f}",
        "accuracy": round(combine(accs, method), 3),
        "presentation": round(combine(pres, method), 3),
        "deduction": round(deduction, 2),
        "deduction_label": f"{deduction:.1f}" if deduction else "",
        # Each judge's own live accuracy — the livestream extension uses it
        # to flash a deduction only when most judges made the same call.
        "judge_accuracy": judge_accuracy,
        # Every judge's marks, in judge order — the Results widget's columns.
        "judge_scores": judge_scores,
        "judges": len(numbers),
        "started": started,
        "submitted": submitted,
        "presenting": presenting,
    }


def shape_final(state: dict, corners: dict, is_solo: bool, judge_numbers: list[int], method: str, mode: str) -> dict:
    """Per athlete: each poomsae's total (with its own restart / penalty)
    and the overall score = the average of the poomsae performed (WT)."""
    count = int(_num(state.get("poomsaeCount")) or 2)
    count = max(1, min(2, count))
    drawn = [p for p in (state.get("poomsae") or []) if isinstance(p, dict)]
    columns = []
    for i in range(count):
        name = drawn[i].get("name") if i < len(drawn) else None
        columns.append({"key": f"poomsae{i + 1}", "label": f"P{i + 1}", "name": name or f"Poomsae {i + 1}"})

    athletes = []
    for color in ("blue",) if is_solo else ("blue", "red"):
        c = corners.get(color)
        if not isinstance(c, dict):
            continue
        flag2, flag3 = flags(c.get("country"))
        scores = [
            corner_live_score(c, col["key"], judge_numbers, method, para=(mode == "para"), stored_adjustments=True)["score"]
            for col in columns
        ]
        total = sum(scores) / len(scores)
        athletes.append({
            "corner": color, "name": c.get("name") or "", "club": c.get("clubName") or "",
            "flag2": flag2, "flag3": flag3,
            "poomsae": [round(x, 3) for x in scores],
            "total": round(total, 3),
        })

    leader = None
    if len(athletes) == 1:
        leader = athletes[0]["corner"]
    elif len(athletes) == 2 and athletes[0]["total"] != athletes[1]["total"]:
        leader = max(athletes, key=lambda a: a["total"])["corner"]
    return {"columns": columns, "athletes": athletes, "leader": leader}


# ── Category Results ────────────────────────────────────────────────────
# match_data.categoryResults is written by the control plugin
# (standings.build_category_view). The overlay only needs a ranked list, so
# this reads the rows defensively: the first list of row objects under one
# of these keys, with the usual field names for each value.
_ROW_KEYS = ("rows", "ranking", "rankings", "standings", "entries", "athletes", "results", "table")


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _find_rows(view) -> list[dict]:
    if isinstance(view, list):
        return [r for r in view if isinstance(r, dict)]
    if not isinstance(view, dict):
        return []
    for key in _ROW_KEYS:
        rows = [r for r in _as_map(view.get(key)).values() if isinstance(r, dict)] \
            if isinstance(view.get(key), (dict, list)) else []
        if isinstance(view.get(key), list):
            rows = [r for r in view[key] if isinstance(r, dict)]
        if rows:
            return rows
    for key in ("groups", "pools", "sections"):   # nested: first group that has rows
        for group in _as_map(view.get(key)).values() if isinstance(view.get(key), (dict, list)) else []:
            rows = _find_rows(group)
            if rows:
                return rows
    return []


def shape_category(view, current_match_id=None) -> dict:
    rows = []
    for r in _find_rows(view):
        who = r
        if not _first(r, "name", "athleteName", "displayName"):
            who = r.get("athlete") or r.get("blue") or r.get("corner") or {}
            who = who if isinstance(who, dict) else {}
        flag2, flag3 = flags(_first(who, "country", "countryCode", "nationality", "flag3") or _first(r, "country", "countryCode"))
        score = _num(_first(r, "total", "finalScore", "final", "score", "average", "points")
                     if _first(r, "total", "finalScore", "final", "score", "average", "points") is not None
                     else _first(who, "total", "finalScore", "final", "score", "average"))
        rank = _num(_first(r, "rank", "position", "place"))
        rows.append({
            "rank": int(rank) if rank is not None else None,
            "name": _first(who, "name", "athleteName", "displayName") or "",
            "flag2": flag2, "flag3": flag3,
            "score": score,
            "qualified": bool(_first(r, "qualified", "advances", "isQualified", "qualifies")),
            "current": bool(current_match_id) and _first(r, "matchId", "match_id") == current_match_id,
        })
    rows = [r for r in rows if r["name"]]
    if not rows:
        return {"rows": []}

    # Ranked order: by rank when given, else by score (ties share a rank).
    if all(r["rank"] is not None for r in rows):
        rows.sort(key=lambda r: r["rank"])
    else:
        rows.sort(key=lambda r: -(r["score"] if r["score"] is not None else -1))
        last, place = None, 0
        for i, r in enumerate(rows, start=1):
            if r["score"] != last:
                place, last = i, r["score"]
            r["rank"] = place
    return {"rows": rows}


def shape_live_payload(state: dict, judge_numbers: list[int], method: str, remaining_seconds=None) -> dict:
    state = state or {}
    stage = state.get("stage")
    round_key = PERFORMING.get(stage) or SHOWING_RESULTS.get(stage)
    in_results = stage in SHOWING_RESULTS
    corners = state.get("corners") or {}
    mode = state.get("scoringMode") or "solo"
    is_solo = state["isSolo"] if isinstance(state.get("isSolo"), bool) else not corners.get("red")

    def corner(color: str):
        c = corners.get(color)
        if not isinstance(c, dict):
            return None
        flag2, flag3 = flags(c.get("country"))
        data = {
            "name": c.get("name") or "",
            "club": c.get("clubName") or "",
            "flag2": flag2,
            "flag3": flag3,
            "score": 10.0,
            "score_label": "10.00",
            "accuracy": FULL_ACCURACY,
            "deduction": 0,
            "deduction_label": "",
        }
        if round_key:
            data.update(corner_live_score(c, round_key, judge_numbers, method, para=(mode == "para")))
        return data

    blue = corner("blue")
    red = None if is_solo else corner("red")

    # What the big number shows: average accuracy while the athlete performs
    # (starts at 4.00 and drops), the full score once Results are on screen.
    # Para poomsae has no accuracy/presentation split → always the score.
    for c in (blue, red):
        if c is None:
            continue
        value = c["score"] if (in_results or mode == "para") else c["accuracy"]
        c["display_score"] = round(value, 3)
        c["display_label"] = f"{value:.2f}"

    leader = None
    if in_results and blue:
        if red is None:
            leader = "blue"
        elif blue["score"] != red["score"]:
            leader = "blue" if blue["score"] > red["score"] else "red"

    # Whole seconds the poomsae clock has run (frozen while paused, 0 before
    # the operator starts it). The livestream extension compacts from this.
    elapsed = None
    duration = _num(state.get("stageDurationSeconds"))
    if round_key and not in_results and duration is not None and remaining_seconds is not None:
        elapsed = int(max(0.0, duration - float(remaining_seconds)))

    # Seconds since the timer was last started (None while paused / stopped)
    # — the livestream extension collapses the clock from this.
    timer_running = bool(round_key) and not in_results and state.get("timerRunning") is True
    run_seconds = None
    start_value = _num(state.get("timerRemainingSeconds"))
    if timer_running and start_value is not None and remaining_seconds is not None:
        run_seconds = int(max(0.0, start_value - float(remaining_seconds)))

    # Presentation mode: the clock is stopped after the athlete performed and
    # most judges are on their presentation marks (any corner).
    presentation_mode = False
    if stage in PERFORMING and not timer_running and (elapsed or 0) > 0:
        for c in (blue, red):
            if c and c.get("judges") and c.get("presenting", 0) >= c["judges"] // 2 + 1:
                presentation_mode = True

    final = shape_final(state, corners, is_solo, judge_numbers, method, mode) if stage == "final_results" else None

    index = 2 if round_key == "poomsae2" else 1
    if in_results:
        clk = "RESULT"
    elif round_key:
        clk = format_clock(remaining_seconds)
    else:
        clk = ""

    return {
        "match_id": state.get("matchId"),
        "id_formatted": state.get("displayNumberLabel") or "",
        # Short form for the stream (see short_category); the full one too.
        "category": short_category(state.get("categoryLabel")),
        "category_full": state.get("categoryLabel") or "",
        # False for para / freestyle → no accuracy score, no deductions shown.
        "live_score": has_live_score(state),
        "phase": state.get("roundPhaseLabel") or "",
        "stage": stage,
        "stage_label": state.get("stageLabel") or "",
        "round_key": round_key,
        "poomsae_index": index,
        # Only the drawn poomsae's name ("" when nothing was drawn).
        "poomsae_name": state.get("currentPoomsae") or "",
        "mode": mode,
        "is_solo": is_solo,
        # Scoreboard: Preparation = number + category only, Poomsae = full,
        # Results = header again (the Results widget shows the detail).
        "visible": stage == "preparation" or bool(round_key),
        "header_only": stage == "preparation" or in_results,
        "result": in_results,
        "leader": leader,
        "all_submitted": state.get("allJudgesSubmitted") is True,
        "clk": clk,
        "elapsed_seconds": elapsed,
        "timer_running": timer_running,
        "run_seconds": run_seconds,
        "presentation_mode": presentation_mode,
        # Final Score widget (stage Final Results): every poomsae's total and
        # the overall score per athlete.
        "final_visible": final is not None,
        "final": final,
        # Category Results widget (stage Category Results): ranked list.
        "category_results": shape_category(state.get("categoryResults"), state.get("matchId"))
        if stage == "category_results" else None,
        "blue": blue,
        "red": red,
    }