"""Single-process collaborative canvas prototype.

The in-memory stores here are intentionally temporary. The coordinator/shard
and durable storage work is planned for later phases.
"""

from pathlib import Path
import asyncio
import logging
import secrets
import uuid

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        for connection in self.active_connections.copy():
            try:
                await connection.send_json(message)
            except Exception:
                logger.exception("WebSocket broadcast failed; dropping connection")
                self.disconnect(connection)


manager = ConnectionManager()
app = FastAPI(title="Collaborative Pixel Canvas")
# Serializes state changes and their broadcasts with WebSocket state snapshots.
canvas_lock = asyncio.Lock()

# Session tokens are issued by the server and never accepted from request bodies.
# This in-memory registry, like the canvas, is reset when the process restarts.
sessions: dict[str, str] = {}
# Coordinate -> {color, owner}. Last committed draw wins in this prototype.
pixels: dict[str, dict[str, str]] = {}
SESSION_COOKIE = "canvas_session"


class DrawRequest(BaseModel):
    x: int = Field(strict=True, ge=0, lt=1000)
    y: int = Field(strict=True, ge=0, lt=1000)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


def session_for(token: str | None) -> tuple[str, str]:
    """Return a known session token and user ID, issuing them when necessary."""
    if token is not None and token in sessions:
        return token, sessions[token]
    new_token = secrets.token_urlsafe(32)
    user_id = str(uuid.uuid4())
    sessions[new_token] = user_id
    return new_token, user_id


def public_pixels() -> dict[str, str]:
    return {key: value["color"] for key, value in pixels.items()}


@app.get("/")
def home(request: Request):
    token, _ = session_for(request.cookies.get(SESSION_COOKIE))
    response = FileResponse(Path(__file__).parent / "index.html")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.get("/state")
async def get_state(request: Request):
    token, _ = session_for(request.cookies.get(SESSION_COOKIE))
    async with canvas_lock:
        state = public_pixels()
    response = JSONResponse({"pixels": state})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.post("/draw")
async def draw(request: DrawRequest, http_request: Request):
    token, user_id = session_for(http_request.cookies.get(SESSION_COOKIE))
    key = f"{request.x},{request.y}"
    color = request.color.upper()
    async with canvas_lock:
        pixels[key] = {"color": color, "owner": user_id}
        await manager.broadcast({"type": "draw", "x": request.x, "y": request.y, "color": color})
    response = JSONResponse({"status": "ok", "x": request.x, "y": request.y, "color": color})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.delete("/clear")
async def clear_my_pixels(request: Request):
    token, user_id = session_for(request.cookies.get(SESSION_COOKIE))
    async with canvas_lock:
        removed = [key for key, pixel in pixels.items() if pixel["owner"] == user_id]
        for key in removed:
            del pixels[key]

        coordinates = [[int(part) for part in key.split(",")] for key in removed]
        await manager.broadcast({"type": "clear", "pixels": coordinates})
    response = JSONResponse({"status": "ok", "cleared": len(removed)})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE)
    if token is None or token not in sessions:
        await websocket.close(code=1008, reason="Open the canvas to create a session")
        return

    try:
        await websocket.accept()
        # Register and snapshot under the same lock as mutations+broadcasts.
        # A mutation is therefore either included in this snapshot or delivered
        # after it as a WebSocket event, never lost between the two.
        async with canvas_lock:
            manager.active_connections.append(websocket)
            await websocket.send_json({"type": "state", "pixels": public_pixels()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # Failed sends and other connection errors must not leak stale sockets.
        logger.exception("WebSocket session ended unexpectedly")
    finally:
        manager.disconnect(websocket)
