from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

router = APIRouter(prefix="/api/plugins", tags=["Plugins"])


class AttachPluginRequest(BaseModel):
    class_name: str


class UpdateSettingsRequest(BaseModel):
    values: Dict[str, Any]


@router.get("/status")
async def plugins_status(request: Request):
    orchestrator = request.app.state.orchestrator
    return orchestrator.get_dashboard_status()


@router.post("/{plugin_name}/restart")
async def restart_plugin(plugin_name: str, request: Request):
    orchestrator = request.app.state.orchestrator
    try:
        return await orchestrator.restart_plugin(plugin_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{plugin_name}/detach")
async def detach_plugin(plugin_name: str, request: Request):
    orchestrator = request.app.state.orchestrator
    try:
        # detach_plugin now also unregisters the plugin's FastAPI routes
        # (settings router + custom router) so a later re-attach doesn't
        # leave two competing route entries bound to different instances —
        # see SystemOrchestrator._remove_plugin_routes.
        return await orchestrator.detach_plugin(plugin_name, request.app)
    except ValueError as e:
        status_code = 404 if "not currently attached" in str(e) else 409
        raise HTTPException(status_code=status_code, detail=str(e))


@router.post("/attach")
async def attach_plugin(body: AttachPluginRequest, request: Request):
    orchestrator = request.app.state.orchestrator
    try:
        return await orchestrator.attach_plugin(body.class_name, request.app)
    except ValueError as e:
        status_code = 409 if "already attached" in str(e) or "missing dependencies" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.get("/{plugin_name}/settings")
async def get_plugin_settings(plugin_name: str, request: Request):
    orchestrator = request.app.state.orchestrator
    instance = orchestrator.active_plugins.get(plugin_name)
    if not instance:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' is not attached.")

    plugin_cls = type(instance)
    current = orchestrator.settings_manager.get_settings(plugin_name)

    return {
        "plugin": plugin_name,
        "class_name": plugin_cls.__name__,
        "custom_ui": plugin_cls.settings_ui,
        "schema": orchestrator.settings_manager.get_schema(plugin_name),
        "values": current.model_dump() if current else {},
    }


@router.put("/{plugin_name}/settings")
async def update_plugin_settings(plugin_name: str, body: UpdateSettingsRequest, request: Request):
    orchestrator = request.app.state.orchestrator
    if plugin_name not in orchestrator.active_plugins:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' is not attached.")
    try:
        updated = await orchestrator.settings_manager.update_settings(plugin_name, body.values)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=e.errors())
    return {"plugin": plugin_name, "values": updated.model_dump()}