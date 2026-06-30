from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/system", tags=["System API"])


# ── Request models ────────────────────────────────────────────────────────────

class PreflightRequest(BaseModel):
    license_key: str


class BootRequest(BaseModel):
    mode: str                           # "online" | "offline"
    tournament_id: Optional[str] = None
    court_id: Optional[int] = None      # ← new
    license_key: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/preflight")
async def preflight(request: Request, body: PreflightRequest):
    """
    Verifies the license key and returns tournaments + the full case document.
    The 'case' field will contain tournamentId / courtId if the device was
    previously bound, allowing the frontend to skip selection entirely.
    """
    orchestrator = request.app.state.orchestrator
    result = await orchestrator.run_preflight_async(body.license_key)

    if not result["licensed"]:
        raise HTTPException(status_code=401, detail="Invalid license key.")

    return result


@router.post("/boot")
async def boot(request: Request, body: BootRequest):
    """
    Boots the plugin stack.
    - Online:  updates the case binding in Firebase, then loads tournament plugins.
    - Offline: loads the default offline plugin stack.
    """
    orchestrator = request.app.state.orchestrator

    if body.mode == "offline":
        await orchestrator.boot_offline(request.app)

    elif body.mode == "online":
        if not body.tournament_id:
            raise HTTPException(status_code=400, detail="tournament_id is required for online mode.")

        # Persist the binding so the next cold-start can skip selection
        if body.license_key and body.court_id:
            await orchestrator.db_adapter.update_case_binding(
                body.license_key, body.tournament_id, body.court_id
            )

        await orchestrator.boot_tournament(
            request.app, body.tournament_id, body.court_id
        )

    else:
        raise HTTPException(status_code=400, detail=f"Unknown boot mode: {body.mode}")

    return {"status": "booted"}


@router.get("/status")
async def status(request: Request):
    orchestrator = request.app.state.orchestrator
    return {
        "is_booted": orchestrator.is_booted,          # ← new, used by auth.html
        "is_offline_mode": orchestrator.system_status.is_offline_mode,
        "active_tournament_id": orchestrator.system_status.active_tournament_id,
        "loaded_plugins": [p.name for p in orchestrator.active_plugins],
    }


@router.get("/tournaments/{tournament_id}/courts")
async def get_courts(tournament_id: str, request: Request):
    """Returns the selectable court IDs for a given tournament."""
    orchestrator = request.app.state.orchestrator
    courts = await orchestrator.db_adapter.fetch_courts_for_tournament(tournament_id)
    return {"tournament_id": tournament_id, "courts": courts}