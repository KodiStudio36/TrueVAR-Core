from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/plugins", tags=["Plugins"])


class AttachPluginRequest(BaseModel):
    class_name: str


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
        return await orchestrator.detach_plugin(plugin_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/attach")
async def attach_plugin(body: AttachPluginRequest, request: Request):
    orchestrator = request.app.state.orchestrator
    try:
        return await orchestrator.attach_plugin(body.class_name, request.app)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{plugin_name}/settings")
async def plugin_settings_placeholder(plugin_name: str, request: Request):
    """
    Placeholder — once the real settings UI is built, this should return
    (or redirect to) a form generated from settings_manager.get_schema().
    """
    orchestrator = request.app.state.orchestrator
    instance = orchestrator.active_plugins.get(plugin_name)
    if not instance:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' is not attached.")

    return {
        "plugin": plugin_name,
        "message": "Settings UI not built yet — placeholder response.",
        "schema": orchestrator.settings_manager.get_schema(plugin_name),
        "current_values": orchestrator.settings_manager.get_settings(plugin_name).model_dump(),
    }