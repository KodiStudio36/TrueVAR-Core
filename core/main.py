from core.adapters.local_json_settings_adapter import LocalJSONSettingsAdapter
from plugins.gstreamer_core.plugin import GStreamerCorePlugin
import uvicorn
import webbrowser
import socketio
import time
from fastapi import FastAPI
from contextlib import asynccontextmanager

# Domain & Adapters
from core.adapters.firebase_adapter import FirebaseAdapter
from core.adapters.system_network_adapter import SystemNetworkAdapter
from core.use_cases.system_orchestrator import SystemOrchestrator
from core.adapters.structured_logger_adapter import StructuredLoggerAdapter
from core.infrastructure.routers import log_router

# Routers
from core.infrastructure.routers import api_router, ui_router

# 1. Initialize Adapters
db_adapter = FirebaseAdapter()
settings_adapter = LocalJSONSettingsAdapter()
network_adapter = SystemNetworkAdapter()
root_logger = StructuredLoggerAdapter()
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')  # ADD
root_logger.info("System initializing composition root lifecycle sequence...")

# 2. Build the Orchestrator
orchestrator = SystemOrchestrator(
    db_adapter=db_adapter, 
    settings_adapter=settings_adapter,
    network_adapter=network_adapter,
    root_logger=root_logger
)

def open_browser():
    time.sleep(1.0)
    webbrowser.open("http://127.0.0.1:8000/") 

# 3. Lifespan Management
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Lifespan] Starting Web Server...")
    # Inject orchestrator into the app state so routers can access it
    app.state.orchestrator = orchestrator
    app.state.sio = sio

    # threading.Thread(target=open_browser, daemon=True).start()
    yield 
    print("[Lifespan] Shutting down Web Server...")
    # await orchestrator.shutdown_system()

# 4. FastAPI Setup
app = FastAPI(title="TrueVAR Core", lifespan=lifespan)

# Mount separated routes
app.include_router(ui_router.router)
app.include_router(api_router.router)

combined_app = socketio.ASGIApp(sio, other_asgi_app=app)

if __name__ == "__main__":
    uvicorn.run(combined_app, host="127.0.0.1", port=8000)