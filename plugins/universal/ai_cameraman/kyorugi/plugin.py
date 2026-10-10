from plugins.universal.ai_cameraman.base_plugin import AICameramanBasePlugin
from plugins.universal.ai_cameraman.kyorugi.settings import KyorugiCameramanSettings


class AICameramanKyorugiPlugin(AICameramanBasePlugin):
    """AI cameraman for kyorugi: frames the two fighters (white dobok + red/blue
    hogu) and ignores the referee. API: /api/ai-cameraman/kyorugi/..."""

    discipline = "kyorugi"

    @property
    def name(self) -> str: return "AI Cameraman Kyorugi"

    @property
    def settings_schema(self): return KyorugiCameramanSettings