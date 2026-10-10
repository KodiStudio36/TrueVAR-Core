"""
Poomsae match result — computed from the live match_data in RTDB when
the operator clicks Final Results, then archived to the match doc in
Firestore by the Firebase Sync plugin (see firebase_sync/plugin.py,
_on_poomsae_result).

Pure functions, no I/O — the SAME rules the operator panel and external
screen use (panel.html buildJudgesTable / screen.html computeAggregate),
so the saved number is exactly the number the audience saw:

  • judge rows = every registered judge slot ∪ every judge with data;
    a judge with nothing for a round counts as full marks (4.0 / 6.0 /
    10.0) and is flagged submitted: False
  • "trim_high_low": with 3+ judges, the single lowest-total and single
    highest-total judge are dropped before averaging
  • a round's score = avg total + restart deduction + penalties
  • the match score = the average of its rounds (one poomsae in round 1
    and the semi-final, two in the final)
  • side by side: higher score wins; tie → higher presentation, then the
    higher total of ALL judges' scores (trimmed ones included) — the WT
    tie-break order; still equal → no winner (decided on the floor)

Shape follows the kyorugi match doc (firebase_sync._archive_match): each
corner's `points` is a LIST with one entry per round — for poomsae one per
poomsae performed — and the match gets round / win / win_score / win_type.
"""
from typing import Optional

DEFAULT_ACCURACY = 4.0
DEFAULT_PRESENTATION = 6.0
DEFAULT_TOTAL = 10.0
ROUND_KEYS = ("poomsae1", "poomsae2")


def normalize_map(value) -> dict:
    """RTDB returns a node keyed by small integers as a list — see
    plugin._normalize_firebase_map; duplicated here to stay import-free."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(i): v for i, v in enumerate(value) if v is not None}
    return {}


def prune(value):
    """Drops None, empty lists and empty maps, recursively — a field that
    doesn't apply (no drawn poomsae, no trimming, no restart…) is simply
    not written, instead of being stored as null / [] / 0."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = prune(v)
            if v is None or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [prune(v) for v in value if v is not None]
    return value


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _judge_numbers(judges_map: dict, registry: dict) -> list[int]:
    numbers = set()
    for key in list(registry.keys()) + list(judges_map.keys()):
        try:
            n = int(key)
        except (TypeError, ValueError):
            continue
        if n >= 1:
            numbers.add(n)
    return sorted(numbers)


def round_result(corner: dict, registry: dict, round_key: str, scoring_method: str,
                 restart: float, penalty: float, poomsae: Optional[dict]) -> dict:
    judges_map = normalize_map(corner.get("judges"))
    entries = []
    for n in _judge_numbers(judges_map, registry):
        data = normalize_map(judges_map.get(str(n))).get(round_key)
        if isinstance(data, dict):
            entry = {
                "judge": n,
                "accuracy": _num(data.get("accuracy"), DEFAULT_ACCURACY),
                "presentation": _num(data.get("presentation"), DEFAULT_PRESENTATION),
                "total": _num(data.get("total"), DEFAULT_TOTAL),
                "submitted": data.get("submitted") is True,
            }
            if isinstance(data.get("presentationCategories"), dict):
                entry["presentationCategories"] = data["presentationCategories"]
            if isinstance(data.get("technical"), dict):
                entry["technical"] = data["technical"]
        else:
            entry = {"judge": n, "accuracy": DEFAULT_ACCURACY, "presentation": DEFAULT_PRESENTATION,
                     "total": DEFAULT_TOTAL, "submitted": False, "noScore": True}
        entries.append(entry)

    included = entries
    trimmed: list[int] = []
    if scoring_method == "trim_high_low" and len(entries) >= 3:
        ordered = sorted(entries, key=lambda e: e["total"])  # stable, like the panel's JS sort
        trimmed = [ordered[0]["judge"], ordered[-1]["judge"]]
        included = [e for e in entries if e["judge"] not in trimmed]
    for e in entries:
        if e["judge"] in trimmed:
            e["trimmed"] = True  # left out otherwise

    count = len(included) or 1
    avg = lambda key: sum(e[key] for e in included) / count
    avg_total = avg("total")
    # One `points` entry, like a kyorugi round: `points` is the round score,
    # `penalties` the penalty total; accuracy / presentation / total are
    # the judges' averages (after trimming).
    return {
        "poomsae": poomsae,
        "points": round(avg_total + restart + penalty, 3),
        "accuracy": round(avg("accuracy"), 3),
        "presentation": round(avg("presentation"), 3),
        "total": round(avg_total, 3),
        "restart": restart or None,      # pruned when 0
        "penalties": penalty or None,
        "judges": {str(e["judge"]): {k: v for k, v in e.items() if k != "judge"} for e in entries},
        "trimmedJudges": trimmed or None,
        "totalAllJudges": round(sum(e["total"] for e in entries), 3),
        "allJudgesSubmitted": bool(entries) and all(e["submitted"] for e in entries),
    }


def corner_result(corner: dict, registry: dict, round_keys: list[str], scoring_method: str,
                  poomsae: list[dict], current_round_key: Optional[str]) -> dict:
    adjustments = normalize_map(corner.get("roundAdjustments"))
    points = []
    for i, key in enumerate(round_keys):
        adj = normalize_map(adjustments.get(key))
        restart = _num(adj.get("restart"))
        penalty = _num(adj.get("penalty"))
        # Live match_data written before roundAdjustments existed only has
        # the flat per-stage fields — use them for the round on the floor.
        if not adj and key == current_round_key:
            restart = _num(corner.get("roundDeduction"))
            penalty = _num(corner.get("roundPenalty"))
        entry = round_result(corner, registry, key, scoring_method, restart, penalty,
                             poomsae[i] if i < len(poomsae) else None)
        points.append({"round": i + 1, **entry})

    n = len(points) or 1
    return {
        "entryId": corner.get("entryId"),
        "points": points,
        "finalScore": round(sum(r["points"] for r in points) / n, 3),
        "presentation": round(sum(r["presentation"] for r in points) / n, 3),
        "totalAllJudges": round(sum(r["totalAllJudges"] for r in points), 3),
    }


# win_type — how a side-by-side match was decided (kyorugi's win_type
# slot): PTS higher final score · PRS tie → higher presentation ·
# ALL tie → higher total of all judges' scores (trimmed included).
WIN_TYPES = (("finalScore", "PTS"), ("presentation", "PRS"), ("totalAllJudges", "ALL"))


def decide_winner(blue: dict, red: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    if not red:
        return None, None
    for key, win_type in WIN_TYPES:
        if blue[key] != red[key]:
            return ("blue" if blue[key] > red[key] else "red"), win_type
    return None, None


def build_match_result(live: dict, registry: dict, scoring_method: str, round_count: int,
                       poomsae: list[dict], current_round_key: Optional[str]) -> dict:
    corners = live.get("corners") or {}
    round_keys = list(ROUND_KEYS[:max(1, min(2, round_count))])
    blue = corner_result(corners.get("blue") or {}, registry, round_keys, scoring_method, poomsae, current_round_key)
    red = corner_result(corners["red"], registry, round_keys, scoring_method, poomsae, current_round_key) \
        if corners.get("red") else None
    winner, win_type = decide_winner(blue, red)
    # Kyorugi writes win_score as the score line, e.g. "12-7".
    win_score = f"{blue['finalScore']:.3f}-{red['finalScore']:.3f}" if red else f"{blue['finalScore']:.3f}"
    return prune({
        "scoringMethod": scoring_method,
        "format": "duel" if red else "solo",
        "round": len(round_keys),
        "blue": blue,
        "red": red,
        "win": winner,
        "win_score": win_score,
        "win_type": win_type,
        "tie": bool(red) and winner is None,
        "allJudgesSubmitted": all(r["allJudgesSubmitted"] for c in (blue, red) if c for r in c["points"]),
    })


# ════════════════════════════════════════════════════════════════════════
# TIMELINE — what happened during each poomsae, for analysis
# ════════════════════════════════════════════════════════════════════════
# Recorded silently by the plugin (operator actions + judge score changes
# seen in RTDB) and saved with each performance doc. One event, compact:
#     {"t": 41250, "e": "a", "j": 2, "p": 3.7}
#   t  ms since this poomsae started (its stage was entered)
#   e  event code (below)
#   j  judge number — judge events only
#   p  value — only when the event has one
#
#   st  poomsae started (stage entered; again = re-entered, scores reset)
#   ts  timer started             p: seconds left
#   tp  timer paused              p: seconds left
#   rs  restart                   p: deduction
#   pn  penalty given             p: athlete's penalty total
#   pc  penalties cleared
#   a   judge accuracy changed    p: accuracy (para: technical sum)
#   pr  judge presentation sent   p: presentation total
#   sb  judge submitted           p: total
#   us  judge submission withdrawn / cleared
#   as  all judges submitted
EVENT_CODES = {
    "st": "poomsae started", "ts": "timer started", "tp": "timer paused",
    "rs": "restart", "pn": "penalty", "pc": "penalties cleared",
    "a": "judge accuracy", "pr": "judge presentation", "sb": "judge submitted",
    "us": "judge submission withdrawn", "as": "all judges submitted",
}


class Timeline:
    """
    Per live match: {roundKey: {"t0": epoch ms, "startedAt": iso,
    "events": [(epoch_ms, code, corner|None, judge|None, value|None)]}}
    plus the last judge snapshot used to detect score changes. Lives in
    memory only — a plugin restart mid-match loses what was recorded so far.
    """

    def __init__(self, match_id: str):
        self.match_id = match_id
        self.rounds: dict[str, dict] = {}
        self._last: dict[tuple, dict] = {}   # (round, corner, judge) -> {accuracy, presentation, submitted}

    def start_round(self, round_key: str, now_ms: int, iso: str) -> None:
        r = self.rounds.get(round_key)
        if r is None:
            r = self.rounds[round_key] = {"t0": now_ms, "startedAt": iso, "events": []}
        r["events"].append((now_ms, "st", None, None, None))
        self.forget_judges(round_key)

    def forget_judges(self, round_key: str) -> None:
        """Scores reset (stage re-entered, or Restart) — forget what the
        judges had, so their next scores are recorded as new."""
        for key in [k for k in self._last if k[0] == round_key]:
            del self._last[key]

    def add(self, round_key: Optional[str], now_ms: int, code: str,
            corner: Optional[str] = None, judge: Optional[int] = None, value=None) -> None:
        r = self.rounds.get(round_key or "")
        if r is not None:
            r["events"].append((now_ms, code, corner, judge, value))

    def observe_judges(self, round_key: str, corners: dict, now_ms: int) -> None:
        """Diffs every judge's data for this round against the last poll."""
        if round_key not in self.rounds:
            return
        for color in ("blue", "red"):
            corner = corners.get(color)
            if not isinstance(corner, dict):
                continue
            for judge_key, judge in normalize_map(corner.get("judges")).items():
                try:
                    j = int(judge_key)
                except (TypeError, ValueError):
                    continue
                data = normalize_map(judge).get(round_key)
                key = (round_key, color, j)
                prev = self._last.get(key)
                if not isinstance(data, dict):
                    if prev and prev.get("submitted"):
                        self.add(round_key, now_ms, "us", color, j)
                    self._last.pop(key, None)
                    continue
                cur = {
                    "accuracy": _num(data.get("accuracy"), DEFAULT_ACCURACY),
                    "presentation": _num(data.get("presentation"), DEFAULT_PRESENTATION),
                    "total": _num(data.get("total"), DEFAULT_TOTAL),
                    "submitted": data.get("submitted") is True,
                }
                # The judge app stamps every write (updatedAt, server ms) —
                # more exact than this poll; used unless clocks disagree.
                stamp = data.get("updatedAt")
                t = int(stamp) if isinstance(stamp, (int, float)) and abs(stamp - now_ms) < 5000 else now_ms
                prev = prev or {"accuracy": DEFAULT_ACCURACY, "presentation": DEFAULT_PRESENTATION, "submitted": False}
                if cur["accuracy"] != prev["accuracy"]:
                    self.add(round_key, t, "a", color, j, cur["accuracy"])
                if cur["presentation"] != prev["presentation"]:
                    self.add(round_key, t, "pr", color, j, cur["presentation"])
                if cur["submitted"] and not prev["submitted"]:
                    self.add(round_key, t, "sb", color, j, cur["total"])
                elif prev["submitted"] and not cur["submitted"]:
                    self.add(round_key, t, "us", color, j)
                self._last[key] = cur

    def events_for(self, round_key: str, color: str) -> list[dict]:
        """Compact events for one athlete's performance of one poomsae —
        shared events (start, timer, all submitted) plus that athlete's own
        (judges, restart, penalties); corner dropped, t made relative."""
        r = self.rounds.get(round_key)
        if not r:
            return []
        out = []
        for ms, code, corner, judge, value in sorted(r["events"], key=lambda e: e[0]):
            if corner is not None and corner != color:
                continue
            ev = {"t": max(0, int(ms - r["t0"])), "e": code}
            if judge is not None:
                ev["j"] = judge
            if value is not None:
                ev["p"] = value
            out.append(ev)
        return out

    def timer_seconds(self, round_key: str) -> Optional[float]:
        """Seconds the timer actually ran during this poomsae."""
        r = self.rounds.get(round_key)
        if not r:
            return None
        running_since, total = None, 0.0
        for ms, code, *_ in sorted(r["events"], key=lambda e: e[0]):
            if code == "ts" and running_since is None:
                running_since = ms
            elif code in ("tp", "st") and running_since is not None:
                total += (ms - running_since) / 1000
                running_since = None
        return round(total, 1) if total else None


def build_performances(match: dict, result: dict, timeline: Optional[Timeline], context: dict) -> list[dict]:
    """
    One doc per athlete per poomsae performed, for
        sports/{sport}/disciplines/{discipline}/performances/{matchId}_{n}_{corner}
    Everything needed for analysis without opening the match: who, which
    poomsae, every judge's scores, deductions, the round score, and the
    timeline. Linked to the match and tournament by id.
    """
    docs = []
    match_id = match.get("id")
    for color in ("blue", "red"):
        corner_result_ = result.get(color)
        if not corner_result_:
            continue
        identity = match.get(color) or {}
        opponent = (match.get("red" if color == "blue" else "blue") or {}) if result.get("format") == "duel" else {}
        for entry in corner_result_.get("points", []):
            n = entry.get("round")
            round_key = ROUND_KEYS[n - 1] if n and n <= len(ROUND_KEYS) else None
            doc = {
                "performanceId": f"{match_id}_{n}_{color}",
                "matchId": match_id,
                "tournamentId": context.get("tournament_id"),
                "tournamentName": context.get("tournament_name"),
                "courtId": context.get("court_id"),
                "categoryCode": match.get("categoryCode"),
                "roundPhase": match.get("roundPhase"),
                "displayNumber": match.get("displayNumber"),
                "format": result.get("format"),
                "scoringMethod": result.get("scoringMethod"),
                "corner": color,
                "poomsaeNumber": n,
                "poomsae": entry.get("poomsae"),
                "athlete": {k: identity.get(k) for k in ("entryId", "name", "clubId", "clubName", "country", "athleteIds")},
                "opponentEntryId": opponent.get("entryId"),
                "points": entry.get("points"),
                "accuracy": entry.get("accuracy"),
                "presentation": entry.get("presentation"),
                "total": entry.get("total"),
                "restart": entry.get("restart"),
                "penalties": entry.get("penalties"),
                "judges": entry.get("judges"),
                "trimmedJudges": entry.get("trimmedJudges"),
                "matchFinalScore": corner_result_.get("finalScore"),
                "won": (result.get("win") == color) if result.get("format") == "duel" and result.get("win") else None,
                "startedAt": (timeline.rounds.get(round_key) or {}).get("startedAt") if timeline and round_key else None,
                "timerSeconds": timeline.timer_seconds(round_key) if timeline and round_key else None,
                "timeline": timeline.events_for(round_key, color) if timeline and round_key else None,
                # Where this poomsae starts in the court's YouTube video.
                "video": (timeline.rounds.get(round_key) or {}).get("video") if timeline and round_key else None,
                "completedAt": context.get("completed_at"),
            }
            docs.append(prune(doc))
    return docs