from pathlib import Path
import uuid
from fastapi import Query
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from fastapi import WebSocket, WebSocketDisconnect

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        failed = []

        for connection in self.active_connections.copy():
            try:
                await connection.send_json(message)
            except Exception:
                failed.append(connection)

        for connection in failed:
            self.disconnect(connection)


manager = ConnectionManager()

app = FastAPI(title="Collaborative Pixel Canvas")

# Temporary in-memory storage.
# Later, each shard will own and persist its assigned tiles.
pixels: dict[str, str] = {}


class DrawRequest(BaseModel):
    x: int = Field(ge=0, lt=1000)
    y: int = Field(ge=0, lt=1000)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/state")
def get_state():
    return {"pixels": pixels}


@app.post("/draw")
async def draw(request: DrawRequest):
    key = f"{request.x},{request.y}"
    pixels[key] = request.color.upper()

    await manager.broadcast({
        "type": "draw",
        "x": request.x,
        "y": request.y,
        "color": pixels[key],
    })

    return {
        "status": "ok",
        "x": request.x,
        "y": request.y,
        "color": pixels[key],
    }




@app.delete("/clear")
async def clear_canvas():
    pixels.clear()

    await manager.broadcast({
        "type": "clear",
    })

    return {"status": "ok"}



@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)

    try:
        # Send the current canvas to this newly connected client.
        await websocket.send_json({
            "type": "state",
            "pixels": pixels,
        })

        # Keep the connection alive.
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(websocket)
