import socketio
from typing import Any

class OverlayBroadcaster:
    def __init__(self, sio: socketio.AsyncServer):
        # Create an Asynchronous Socket.IO server allowing all origins
        self.sio = sio
        
        self._setup_handlers()

    def _setup_handlers(self):
        # FIX — namespace must match what the JS client connects to
        @self.sio.event(namespace='/truevar')
        async def connect(sid, environ):
            print(f"[/taekwondo] Client connected: {sid}")

        @self.sio.event(namespace='/truevar')
        async def disconnect(sid):
            print(f"[/taekwondo] Client disconnected: {sid}")

    async def emit_to_namespace(self, event_name: str, data: Any = None):
        """
        Generic pass-through to Socket.IO. 
        No domain knowledge (like "SHOW_NEXT_ROUND" or Taekwondo logic) lives here.
        """
        await self.sio.emit(event_name, data, namespace="/truevar")