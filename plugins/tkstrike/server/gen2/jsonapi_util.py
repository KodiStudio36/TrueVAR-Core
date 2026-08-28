"""
jsonapi_util.py

Minimal helpers for emitting crnk-shaped JSON:API documents — the wire
format the physical TkStrike/daedo OVR console speaks (see the decompiled
com.xtremis.daedo.wtdata.model classes and your captured traffic).

No persistent store here on purpose: TkStrikeServerPlugin builds these
resource dicts fresh, per request, from whatever it currently has cached
from Firebase (see its docstring). This module only knows how to turn
already-built resource dicts into a valid JSON:API document — it doesn't
know anything about Firebase, matches, or courts.
"""

from typing import Any, Optional


def resource(rtype: str, rid: str, attributes: dict, relationships: Optional[dict] = None) -> dict:
    """
    One internal resource record.
    relationships values:
      to-one:  {"type": "<type>", "id": "<id>"} or None
      to-many: [{"type": "<type>", "id": "<id>"}, ...]
    """
    return {"type": rtype, "id": rid, "attributes": attributes, "relationships": relationships or {}}


def _relationships_linkage(relationships: dict) -> dict:
    out = {}
    for name, val in relationships.items():
        if val is None:
            out[name] = {"data": None}
        elif isinstance(val, list):
            out[name] = {"data": [{"type": v["type"], "id": v["id"]} for v in val]}
        else:
            out[name] = {"data": {"type": val["type"], "id": val["id"]}}
    return out


def serialize(entry: dict) -> dict:
    doc: dict[str, Any] = {"type": entry["type"], "id": entry["id"], "attributes": entry["attributes"]}
    if entry.get("relationships"):
        doc["relationships"] = _relationships_linkage(entry["relationships"])
    return doc


def build_document(
    data_entries: list[dict],
    included_entries: Optional[list[dict]] = None,
    single: bool = False,
) -> dict:
    data = [serialize(e) for e in data_entries]
    doc: dict[str, Any] = {"data": data[0] if single else data}
    if included_entries:
        seen = set()
        included = []
        for e in included_entries:
            key = (e["type"], e["id"])
            if key in seen:
                continue
            seen.add(key)
            included.append(serialize(e))
        if included:
            doc["included"] = included
    if not single:
        doc["meta"] = {"totalResourceCount": len(data)}
    return doc


def error_document(status: int, title: str, detail: str = "") -> dict:
    return {"errors": [{"status": str(status), "title": title, "detail": detail or title}]}


def parse_filters(query_params) -> dict[str, str]:
    """Parses filter[x]=y style query params into {x: y}."""
    filters = {}
    for key, value in query_params.multi_items():
        if key.startswith("filter[") and key.endswith("]"):
            filters[key[len("filter["):-1]] = value
    return filters