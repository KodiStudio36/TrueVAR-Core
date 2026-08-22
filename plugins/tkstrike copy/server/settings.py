from typing import Optional

from pydantic import BaseModel, Field


class TkStrikeServerSettings(BaseModel):
    """Settings for TkStrikeServerPlugin — same plugin name/key as before
    (`tkstrike_server`), so existing persisted settings still load. This
    version serves the actual WT/daedo OVR JSON:API surface (GET /matches,
    /participants/{id}, /competitors/{id}, POST /matches/{id}/actions,
    ...) that the physical TkStrike console expects at the server root,
    backed live by Firebase — see the plugin's module docstring for why.

    This instance binds to ONE court (`shared_context["court_id"]`, set at
    boot_tournament()). The console's own filter[mat] query param is
    ignored on purpose — see the plugin's module docstring."""

    enabled: bool = True

    lookahead_count: int = Field(
        default=5, ge=1, le=20,
        description="How many 'ready' matches on this court to keep watching/exposing "
                    "via GET /matches (filter[status]=available), not just the "
                    "immediate next one.",
    )

    mat_number: Optional[int] = Field(
        default=None,
        description="Cosmetic-only value put in the outgoing Match.mat attribute. "
                    "NOT used to filter anything — filter[mat] from the console is "
                    "always ignored (this instance only ever has its own bound "
                    "court_id's matches to serve). Leave unset to just echo back "
                    "court_id as an int if it looks numeric, else 0.",
    )

    # --- Defaults used to synthesize match-configurations, since Firebase
    #     doesn't currently store an official WT rules config per match. ---
    default_rules: str = Field(default="CONVENTIONAL", description="CONVENTIONAL or BESTOF3.")
    default_rounds: int = Field(default=3, ge=1, le=5)
    default_round_time: str = Field(default="02:00")
    default_rest_time: str = Field(default="01:00")
    default_injury_time: str = Field(default="01:00")
    default_max_penalties: int = Field(default=10, ge=1, le=30)
    default_golden_point_enabled: bool = Field(default=True)
    default_golden_point_time: str = Field(default="01:00")