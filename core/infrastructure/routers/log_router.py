import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/monitor", tags=["System Monitoring"])

# A simple thread-safe queue to capture in-memory logs for the UI
log_queue = asyncio.Queue(maxsize=100)

async def log_generator():
    """Yields logs to the web interface as they arrive."""
    while True:
        log_entry = await log_queue.get()
        yield f"data: {log_entry}\n\n"

@router.get("/logs/stream")
async def stream_logs(request: Request):
    """An SSE endpoint your web dashboard can connect to using an EventSource object."""
    return StreamingResponse(log_generator(), media_type="text/event-stream")

def push_to_web_queue(_, __, event_dict):
    # This acts as an internal hook to capture logs live
    if 'log_queue' in globals():
        asyncio.run_coroutine_threadsafe(log_queue.put(str(event_dict)), asyncio.get_event_loop())
    return event_dict