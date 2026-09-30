"""
plugin.py — TkStrikeServerPlugin
"""

import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from plugins.base_plugin import BasePlugin, Namespace
from plugins.tkstrike.server.gen3.settings import TkStrikeServerGen3Settings
from plugins.tkstrike.server.gen3.jsonapi_util import (
    build_document, error_document, parse_filters, resource,
)

JSONAPI_MEDIA_TYPE = "application/vnd.api+json"

# ============================================================ CONFIGURATION CONSTANTS

REMOVE_DIACRITICS: bool = True  # Toggle diacritics stripping globally before JSON output


# ============================================================ DIACRITICS & SANITIZATION HELPERS

def _strip_diacritics(text: str) -> str:
    """Removes diacritics/accents from a string (e.g. Sabína -> Sabina)."""
    if not isinstance(text, str):
        return text
    normalized = unicodedata.normalize('NFD', text)
    return "".join(c for c in normalized if unicodedata.category(c) != 'Mn')


def _clean_diacritics_obj(obj: Any) -> Any:
    """Recursively strips diacritics from all string values in a dict/list structure."""
    if not REMOVE_DIACRITICS:
        return obj
    if isinstance(obj, str):
        return _strip_diacritics(obj)
    elif isinstance(obj, dict):
        return {k: _clean_diacritics_obj(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_diacritics_obj(item) for item in obj]
    return obj


def _json_response(data: dict, status_code: int = 200) -> JSONResponse:
    """Central JSON response renderer that applies diacritic cleaning."""
    cleaned_data = _clean_diacritics_obj(data)
    return JSONResponse(cleaned_data, status_code=status_code, media_type=JSONAPI_MEDIA_TYPE)


# ============================================================ CATEGORY & STRING FORMATTING

def normalize_category(code: Optional[str]) -> str:
    """
    Normalizes category codes into standardized display names:
    - individual_juniors_female_55 -> Individual Juniors Female -55kg
    - individual_juniors_male_plus_68 -> Individual Juniors Male +68kg
    """
    if not code:
        return ""
    
    clean = code.replace("ev-", "").strip()
    
    # Extract weight category modifiers
    plus_match = re.search(r'_plus_(\d+)$', clean, re.IGNORECASE)
    minus_match = re.search(r'_(\d+)$', clean, re.IGNORECASE)
    
    weight_str = ""
    if plus_match:
        weight_str = f"+{plus_match.group(1)}kg"
        clean = clean[:plus_match.start()]
    elif minus_match:
        weight_str = f"-{minus_match.group(1)}kg"
        clean = clean[:minus_match.start()]
        
    words = [w.capitalize() for w in clean.split('_') if w]
    base_name = " ".join(words)
    
    if weight_str:
        return f"{base_name} {weight_str}"
    return base_name


def _extract_weight_category(code: Optional[str]) -> Optional[str]:
    """Extracts short weight notation (e.g. -55 or +68)."""
    if not code:
        return None
    plus_match = re.search(r'(?:_plus_|\+)(\d+)', code, re.IGNORECASE)
    if plus_match:
        return f"+{plus_match.group(1)}"
    minus_match = re.search(r'(?:_|-)(\d+)$', code, re.IGNORECASE)
    if minus_match:
        return f"-{minus_match.group(1)}"
    return code


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split_name(full_name: Optional[str]) -> tuple[str, str]:
    if not full_name:
        return "", ""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], parts[0]
    given, family = parts[0], parts[1]
    return given, family


def _initial_name(full_name: Optional[str]) -> Optional[str]:
    if not full_name:
        return None
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name
    return f"{parts[0]} {parts[-1][0]}."


def _corner_competitor_id(match_doc_id: str, side: str, corner: Optional[dict]) -> Optional[str]:
    if not corner:
        return None
    return corner.get("entryId") or f"{match_doc_id}-{side}"


def _event_id_for(match_doc: dict) -> str:
    code = match_doc.get("categoryCode") or "unknown"
    return f"ev-{code}"


def _match_config_id_for(match_doc: dict) -> str:
    return f"mc-{match_doc['id']}"


def _organization_id_for(country: Optional[str]) -> Optional[str]:
    return f"org-{country.lower()}" if country else None


def _parse_category_code(code: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not code:
        return None, None

    # Normalize dividers (underscores, hyphens) into spaces
    normalized = re.sub(r'[^a-z0-9]+', ' ', code.lower()).strip()
    words = set(normalized.split())

    # Division matching (multi-word categories checked before single-word ones)
    if "younger cadet" in normalized or "younger cadets" in normalized:
        division = "YOUNGER CADETS"
    elif "pupil 1" in normalized or "pupils 1" in normalized or "puplis 1" in normalized:
        division = "PUPILS 1"
    elif "pupil 2" in normalized or "pupils 2" in normalized or "puplis 2" in normalized:
        division = "PUPILS 2"
    elif "cadet" in normalized or "cadets" in normalized:
        division = "CADETS"
    elif "junior" in normalized or "juniors" in normalized:
        division = "JUNIORS"
    elif "senior" in normalized or "seniors" in normalized:
        division = "SENIORS"
    elif "veteran" in normalized or "veterans" in normalized:
        division = "VETERANS"
    else:
        division = None

    # Gender matching via exact word set lookup
    if any(term in words for term in ("female", "women", "woman", "females")):
        gender = "FEMALE"
    elif any(term in words for term in ("male", "men", "man", "males")):
        gender = "MALE"
    else:
        gender = None

    return division, gender


def _default_live_state(settings: TkStrikeServerGen3Settings, doc: Optional[dict] = None) -> dict:
    doc = doc or {}
    return {
        "status": "available",
        "score": {"home": 0, "away": 0},
        "penalties": {"home": 0, "away": 0},
        "round": 1,
        "roundTime": doc.get("roundTime") or settings.default_round_time,
        "result": None,
    }


# ============================================================ SCORING SIDE EFFECTS

SCORE_POINTS = {
    "SCORE_HOME_PUNCH": ("home", 1), "SCORE_HOME_KICK": ("home", 2),
    "SCORE_HOME_TKICK": ("home", 4), "SCORE_HOME_HEAD": ("home", 3),
    "SCORE_HOME_THEAD": ("home", 5),
    "SCORE_AWAY_PUNCH": ("away", 1), "SCORE_AWAY_KICK": ("away", 2),
    "SCORE_AWAY_TKICK": ("away", 4), "SCORE_AWAY_HEAD": ("away", 3),
    "SCORE_AWAY_THEAD": ("away", 5),
}
PENALTY_OPPONENT = {"PENALTY_HOME": "away", "PENALTY_AWAY": "home"}


def _apply_action(live: dict, attributes: dict) -> None:
    action = attributes.get("action")

    if action in SCORE_POINTS:
        side, pts = SCORE_POINTS[action]
        live["score"][side] = live["score"].get(side, 0) + pts

    elif action in PENALTY_OPPONENT:
        side = "home" if action == "PENALTY_HOME" else "away"
        opponent = PENALTY_OPPONENT[action]
        live["penalties"][side] = live["penalties"].get(side, 0) + 1
        live["score"][opponent] = live["score"].get(opponent, 0) + 1

    elif action == "MATCH_START":
        live["status"] = "in-progress"

    elif action == "ROUND_START":
        if attributes.get("round") is not None:
            live["round"] = attributes["round"]

    elif action == "MATCH_END":
        live["status"] = "completed"
        home, away = live["score"]["home"], live["score"]["away"]
        winner = "home" if home > away else ("away" if away > home else "tie")
        live["result"] = {
            "status": "UNOFFICIAL",
            "decision": "PTF" if winner != "tie" else "TIE",
            "homeType": "WIN" if winner == "home" else ("TIE" if winner == "tie" else "LOSS"),
            "awayType": "WIN" if winner == "away" else ("TIE" if winner == "tie" else "LOSS"),
        }

    elif action == "ADJUST_SCORE" and attributes.get("score"):
        live["score"] = attributes["score"]

    elif action == "ADJUST_PENALTY" and attributes.get("penalties"):
        live["penalties"] = attributes["penalties"]


class TkStrikeServerGen3Plugin(BasePlugin):
    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._settings: Optional[TkStrikeServerGen3Settings] = None

        self._tournament = None
        self._tournament_id: Optional[str] = None
        self._sport_id: Optional[str] = None
        self._discipline_id: Optional[str] = None
        self._court_id: Optional[str] = None

        self._watch = None
        self._raw_matches: dict[str, dict] = {}
        self._live_state: dict[str, dict] = {}
        self._actions: dict[str, list[dict]] = {}
        self._last_seen_ids: Optional[tuple] = None
        self._last_error: Optional[str] = None

    @property
    def name(self) -> str:
        return "TkStrike Gen3 Local Server"

    @property
    def requires_internet(self) -> bool:
        return True

    @property
    def settings_schema(self):
        return TkStrikeServerGen3Settings

    namespace: str = Namespace.TKSTRIKE

    async def start(self) -> None:
        self._settings = self.core_context["settings_manager"].get_settings(self.name)

        tournament = self.core_context.get("tournament")
        if tournament is None:
            self.logger.warning("TkStrikeServer: no tournament in context — plugin idle.")
            return

        if not self._settings.enabled:
            self.logger.info("TkStrikeServer disabled via settings — plugin idle.")
            return

        self._court_id = self.core_context.get("court_id")
        if not self._court_id:
            self.logger.warning("TkStrikeServer: no court_id in shared_context, plugin idle.")
            return

        self._tournament = tournament
        self._tournament_id = tournament.id
        self._sport_id = tournament.sport
        self._discipline_id = tournament.discipline

        db = self.core_context["db"]
        try:
            self._watch = db.watch_next_matches_for_court(
                self._sport_id, self._discipline_id, self._tournament_id, self._court_id,
                callback=self._on_matches_changed,
                limit=self._settings.lookahead_count,
            )
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("TkStrikeServer: failed to establish schedule watch", exception=exc)
            return

        self.logger.info(
            "TkStrikeServer started — bound court_id=%r",
            tournament_id=self._tournament_id, sport_id=self._sport_id,
            discipline_id=self._discipline_id, court_id=self._court_id,
            lookahead_count=self._settings.lookahead_count,
        )

    async def stop(self) -> None:
        if self._watch:
            self._watch.unsubscribe()
            self._watch = None
        self.logger.warning("TkStrikeServer plugin stopped.")

    async def on_settings_changed(self, new_settings: TkStrikeServerGen3Settings) -> None:
        self.logger.info("TkStrikeServer settings changed — restarting.")
        await self.stop()
        self._settings = new_settings
        await self.start()

    async def _on_matches_changed(self, matches: list[dict]) -> None:
        self._last_error = None

        for doc in matches:
            self._raw_matches[doc["id"]] = doc
            self._live_state.setdefault(doc["id"], _default_live_state(self._settings, doc))

        current_ids = tuple(sorted(m["id"] for m in matches))
        stale_ids = [mid for mid in self._raw_matches if mid not in current_ids]
        for mid in stale_ids:
            self._raw_matches.pop(mid, None)
            self._live_state.pop(mid, None)
            self._actions.pop(mid, None)

        if current_ids == self._last_seen_ids:
            return
        self._last_seen_ids = current_ids

        bus = self.core_context.get("event_bus")
        if bus:
            asyncio.create_task(bus.publish(
                "tkstrike_server.court_queue_changed",
                {"court_id": self._court_id, "match_ids": list(current_ids)},
            ))

    # ------------------------------------------------------------------ resource builders

    def _mat_attribute(self, court_id: Optional[str]) -> int:
        if self._settings.mat_number is not None:
            return self._settings.mat_number
        return _safe_int(court_id, default=0)

    def _competitor_resource(self, cid: str, corner: dict, event_id: str, match_id: Optional[str] = None) -> dict:
        name = corner.get("name")
        org_id = _organization_id_for(corner.get("country"))
        
        relationships = {
            "organization": {"type": "organizations", "id": org_id} if org_id else None,
            "event": {"type": "events", "id": event_id},
            "participant": {"type": "participants", "id": cid},
        }
        if match_id:
            relationships["matches"] = [{"type": "matches", "id": match_id}]

        return resource("competitors", cid, {
            "competitorType": "T",
            "printName": name or "Unknown",
            "printInitialName": _initial_name(name),
            "tvName": name or "Unknown",
            "tvInitialName": _initial_name(name),
            "scoreboardName": (name or "").upper() or None,
            "rank": corner.get("rank"),
            "seed": corner.get("seed"),
            "country": corner.get("country"),
        }, relationships)

    def _participant_resource(self, cid: str, corner: dict) -> dict:
        name = corner.get("name")
        given, family = _split_name(name)
        org_id = _organization_id_for(corner.get("country"))
        return resource("participants", cid, {
            "licenseNumber": None, #corner.get("clubId"),
            "givenName": given,
            "familyName": family,
            "passportGivenName": given,
            "passportFamilyName": family,
            "preferredGivenName": given,
            "preferredFamilyName": family,
            "printName": name or "Unknown",
            "printInitialName": _initial_name(name),
            "tvName": name or "Unknown",
            "tvInitialName": _initial_name(name),
            "scoreboardName": (name or "").upper() or None,
            "gender": corner.get("gender"),
            "birthDate": corner.get("birthDate"),
            "mainRole": "ATHLETE",
            "country": corner.get("country"),
        }, {
            "organization": {"type": "organizations", "id": org_id} if org_id else None,
        })

    def _event_resource(self, event_id: str, doc: dict, match_id: Optional[str] = None) -> dict:
        raw_code = doc.get("categoryCode") or doc.get("category") or doc.get("name") or ""
        normalized_name = normalize_category(raw_code)
        weight_cat = _extract_weight_category(raw_code)
        division, gender = _parse_category_code(raw_code)
        
        relationships = {
            "matches": [{"type": "matches", "id": match_id}] if match_id else [],
            "medalWinners": [],
        }

        return resource("events", event_id, {
            "discipline": doc.get("discipline") or "TKD",
            "division": doc.get("division") or division,
            "gender": doc.get("gender") or gender,
            "name": normalized_name or raw_code,
            "weightCategory": normalized_name or raw_code,
            "sportClass": doc.get("sportClass"),
            "category": doc.get("category"),
            "role": "ATHLETE",
        }, relationships)

    def _match_configuration_resource(self, mc_id: str, doc: dict) -> dict:
        s = self._settings
        return resource("match-configurations", mc_id, {
            "rules": doc.get("rules") or s.default_rules,
            "rounds": doc.get("rounds") or s.default_rounds,
            "timing": {
                "round": s.default_round_time,
                "rest": s.default_rest_time,
                "injury": s.default_injury_time,
            },
            "thresholds": {"body": doc.get("hitLevel") or 0, "head": 5, "punch": 5},
            "videoReplayQuota": {"home": 1, "away": 1},
            "goldenPoint": {
                "enabled": s.default_golden_point_enabled,
                "time": s.default_golden_point_time if s.default_golden_point_enabled else None,
            },
            "maxDifference": 15,
            "maxPenalties": s.default_max_penalties,
        })

    def _referee_assignment_resource(self, ref_id: str, match_id: str) -> dict:
        return resource("match-referee-assignments", ref_id, {}, {
            "match": {"type": "matches", "id": match_id},
            "refJ1": None,
            "refJ2": None,
            "refJ3": None,
            "refCR": None,
            "refRJ": None,
            "refTA": None,
        })

    def _match_resource(self, doc: dict) -> tuple[dict, list[tuple[str, dict]]]:
        match_id = doc["id"]
        live = self._live_state.setdefault(match_id, _default_live_state(self._settings, doc))
        event_id = _event_id_for(doc)
        mc_id = _match_config_id_for(doc)
        ref_id = doc.get("refereeAssignmentId") or f"ref-{match_id}"
        session_id = doc.get("sessionId") or "s-1"

        event_res = self._event_resource(event_id, doc, match_id)
        mc_res = self._match_configuration_resource(mc_id, doc)
        ref_res = self._referee_assignment_resource(ref_id, match_id)

        related: list[tuple[str, dict]] = [
            ("event", event_res),
            ("matchConfiguration", mc_res),
            ("refereeAssignment", ref_res),
        ]

        relationships: dict[str, Any] = {
            "homeCompetitor": None,
            "awayCompetitor": None,
            "session": {"type": "sessions", "id": session_id},
            "event": {"type": "events", "id": event_id},
            "refereeAssignment": {"type": "match-referee-assignments", "id": ref_id},
            "matchConfiguration": {"type": "match-configurations", "id": mc_id},
            "results": [],
        }

        for side, key in (("home", "blue"), ("away", "red")):
            corner = doc.get(key)
            cid = _corner_competitor_id(match_id, side, corner)
            rel_name = "homeCompetitor" if side == "home" else "awayCompetitor"
            if cid is None:
                relationships[rel_name] = None
                continue
            relationships[rel_name] = {"type": "competitors", "id": cid}
            related.append((rel_name, self._competitor_resource(cid, corner, event_id, match_id)))

        now_iso = datetime.now(timezone.utc).astimezone().isoformat()
        scheduled_start = doc.get("scheduledStart") or now_iso
        estimated_start = doc.get("estimatedStart") or now_iso

        match_number = (
            doc.get("displayNumber")
            if doc.get("displayNumber") is not None
            else (doc.get("number") or match_id)
        )

        return resource("matches", match_id, {
            "status": live["status"],
            "mat": self._mat_attribute(self._court_id),
            "number": str(int(match_number)) if isinstance(match_number, float) and match_number.is_integer() else str(match_number),
            "phase": doc.get("roundPhase") or "F",
            "schedule": {
                "status": "SCHEDULED" if live["status"] == "available" else
                          ("RUNNING" if live["status"] == "in-progress" else "FINISHED"),
                "scheduledStart": scheduled_start,
                "estimatedStart": estimated_start,
                "actualStart": doc.get("actualStart"),
            },
            "result": live["result"],
            "score": live["score"],
            "penalties": live["penalties"],
            "round": live["round"],
            "roundTime": live["roundTime"],
        }, relationships), related

    # ------------------------------------------------------------------ index for /participants, /competitors

    def _find_corner_by_id(self, cid: str) -> Optional[dict]:
        for doc in self._raw_matches.values():
            for side, key in (("home", "blue"), ("away", "red")):
                corner = doc.get(key)
                if corner and _corner_competitor_id(doc["id"], side, corner) == cid:
                    return corner
        return None

    # ------------------------------------------------------------------ HTTP router

    def get_router(self) -> APIRouter:
        router = APIRouter(tags=["TkStrike OVR Server"])

        @router.get("/matches")
        async def list_matches(request: Request):
            filters = parse_filters(request.query_params)
            include_param = request.query_params.get("include")
            include_names = {s.strip() for s in include_param.split(",")} if include_param else set()

            data_entries: list[dict] = []
            included_entries: list[dict] = []

            for doc in self._raw_matches.values():
                live = self._live_state.get(doc["id"], _default_live_state(self._settings, doc))
                if live["status"] != "available":
                    continue
                entry, related = self._match_resource(doc)
                data_entries.append(entry)
                if include_names:
                    included_entries.extend(r for rel_name, r in related if rel_name in include_names)

            self.logger.info(
                "TkStrikeServer: GET /matches",
                requested_filters=filters,
                returned_match_ids=[e["id"] for e in data_entries],
            )

            return _json_response(build_document(data_entries, included_entries))

        @router.get("/matches/{match_id}")
        async def get_match(match_id: str, request: Request):
            doc = self._raw_matches.get(match_id)
            if not doc:
                return _json_response(error_document(404, "Match not found"), status_code=404)
            include_param = request.query_params.get("include")
            include_names = {s.strip() for s in include_param.split(",")} if include_param else set()
            entry, related = self._match_resource(doc)
            included = [r for rel_name, r in related if rel_name in include_names] if include_names else []
            return _json_response(build_document([entry], included, single=True))

        @router.get("/participants/{participant_id}")
        async def get_participant(participant_id: str):
            corner = self._find_corner_by_id(participant_id)
            if not corner:
                return _json_response(error_document(404, "Participant not found"), status_code=404)
            entry = self._participant_resource(participant_id, corner)
            return _json_response(build_document([entry], single=True))

        @router.get("/competitors/{competitor_id}")
        async def get_competitor(competitor_id: str, request: Request):
            corner = self._find_corner_by_id(competitor_id)
            if not corner:
                return _json_response(error_document(404, "Competitor not found"), status_code=404)
            event_id = None
            match_id = None
            for doc in self._raw_matches.values():
                for side, key in (("home", "blue"), ("away", "red")):
                    c = doc.get(key)
                    if c and _corner_competitor_id(doc["id"], side, c) == competitor_id:
                        event_id = _event_id_for(doc)
                        match_id = doc["id"]
                        break
                if event_id:
                    break
            entry = self._competitor_resource(competitor_id, corner, event_id or "ev-unknown", match_id)
            include_param = request.query_params.get("include")
            included = [self._participant_resource(competitor_id, corner)] if include_param else []
            return _json_response(build_document([entry], included, single=True))

        @router.post("/matches/{match_id}/actions")
        async def create_action(match_id: str, request: Request): ...
            # doc = self._raw_matches.get(match_id)
            # if not doc:
            #     return _json_response(error_document(404, "Match not found"), status_code=404)

            # body = await request.json()
            # attributes = dict(body.get("data", {}).get("attributes", {}))
            # live = self._live_state.setdefault(match_id, _default_live_state(self._settings))

            # _apply_action(live, attributes)

            # action_id = f"act-{len(self._actions.get(match_id, [])) + 1}-{match_id}"
            # action_attrs = {
            #     **attributes,
            #     "score": dict(live["score"]),
            #     "penalties": dict(live["penalties"]),
            # }
            # action_entry = resource("match-actions", action_id, action_attrs, {
            #     "match": {"type": "matches", "id": match_id},
            # })
            # self._actions.setdefault(match_id, []).append(action_entry)

            # db = self.core_context["db"]
            # try:
            #     await db.push_live_state(self._tournament_id, self._court_id, {
            #         "matchId": match_id,
            #         "action": attributes.get("action"),
            #         "score": live["score"],
            #         "penalties": live["penalties"],
            #         "round": live["round"],
            #         "status": live["status"],
            #     })
            # except Exception as exc:
            #     self.logger.error("TkStrikeServer: push_live_state failed", exception=exc, match_id=match_id)

            # if attributes.get("action") == "MATCH_END":
            #     await self._handle_match_end(match_id, live)

            # return _json_response(build_document([action_entry], single=True), status_code=201)

        @router.get("/matches/{match_id}/actions")
        async def list_actions(match_id: str): ...
            # if match_id not in self._raw_matches:
            #     return _json_response(error_document(404, "Match not found"), status_code=404)
            # actions = self._actions.get(match_id, [])
            # return _json_response(build_document(actions))

        @router.get("/health")
        async def health():
            return {
                "status": "ok",
                "court_id": self._court_id,
                "tournament_id": self._tournament_id,
                "watching": self._watch is not None,
                "cached_match_ids": list(self._raw_matches.keys()),
                "last_error": self._last_error,
            }

        return router

    async def _handle_match_end(self, match_id: str, live: dict) -> None:
        db = self.core_context["db"]
        try:
            await db.save_match_state(self._sport_id, self._discipline_id, match_id, {
                "status": "completed",
                "score": live["score"],
                "penalties": live["penalties"],
                "result": live["result"],
            })
        except Exception as exc:
            self.logger.error("TkStrikeServer: save_match_state failed", exception=exc, match_id=match_id)