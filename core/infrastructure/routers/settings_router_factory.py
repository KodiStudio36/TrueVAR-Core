from fastapi import APIRouter, Body
from core.use_cases.settings_manager import SettingsManager

def create_plugin_settings_router(plugin_name: str, settings_manager: SettingsManager) -> APIRouter:
    """Generates standard settings endpoints for any given plugin."""
    router = APIRouter(prefix=f"/api/settings/{plugin_name}", tags=[f"{plugin_name} Settings"])

    @router.get("/schema")
    async def get_schema():
        """Returns JSON schema for front-end form generation."""
        return settings_manager.get_schema(plugin_name)

    @router.get("/values")
    async def get_values():
        """Returns the current active values."""
        return settings_manager.get_settings(plugin_name).model_dump()

    @router.post("/update")
    async def update_values(new_data: dict = Body(...)):
        """Receives form payload, saves it, and alerts the plugin."""
        updated = await settings_manager.update_settings(plugin_name, new_data)
        return {"status": "success", "new_values": updated.model_dump()}

    return router