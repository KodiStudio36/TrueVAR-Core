from plugins.universal.ai_cameraman.base_plugin import AICameramanBasePlugin
from plugins.universal.ai_cameraman.poomsae.settings import PoomsaeCameramanSettings


class AICameramanPoomsaePlugin(AICameramanBasePlugin):
    """AI cameraman for poomsae: frames everyone standing on the square
    competition area (individual, pairs, team). API: /api/ai-cameraman/poomsae/..."""

    discipline = "poomsae"

    @property
    def name(self) -> str: return "AI Cameraman Poomsae"

    @property
    def settings_schema(self): return PoomsaeCameramanSettings