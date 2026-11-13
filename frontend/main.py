"""Browser-facing coordinator for the collaborative pixel canvas."""

from pathlib import Path
import asyncio
import logging
import os
import secrets
import uuid
import sqlite3
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from shared.tiles import TILE_COLUMNS, TILE_ROWS, TileAddress, configured_shard_ids, shard_for_tile, tile_for_pixel

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        for connection in self.active_connections.copy():
            try:
                await asyncio.wait_for(connection.send_json(message), timeout=2.0)
            except Exception:
                logger.exception("WebSocket broadcast failed; dropping connection")
                self.disconnect(connection)

    async def resync_all(self) -> None:
        """Force browsers to fetch an authoritative snapshot after an unknown commit."""
        for connection in self.active_connections.copy():
            self.disconnect(connection)
            try:
                await asyncio.wait_for(connection.close(code=1012, reason="Canvas resynchronizing"), timeout=2.0)
            except Exception:
                logger.exception("WebSocket resynchronization close failed")


manager = ConnectionManager()
app = FastAPI(title="Collaborative Pixel Canvas")
# Serializes state changes and their broadcasts with WebSocket state snapshots.
canvas_lock = asyncio.Lock()

# Session tokens are issued by the server and never accepted from request bodies.
# The dictionary is a process-local cache; the durable mapping is in SQLite.
sessions: dict[str, str] = {}
# Coordinate -> {color, owner} cache, refreshed from the shard stores on reads.
pixels: dict[str, dict[str, str]] = {}
SESSION_COOKIE = "canvas_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_SHARD_URLS = (
    "shard-1=http://shard-1:8001,"
    "shard-2=http://shard-2:8002,"
    "shard-3=http://shard-3:8003"
)
SHARD_HEALTH_TIMEOUT = httpx.Timeout(connect=1.0, read=2.0, write=1.0, pool=1.0)
SHARD_OPERATION_TIMEOUT = httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0)
OWNERSHIP_DB_PATH = Path(os.getenv("OWNERSHIP_DB_PATH", str(Path(__file__).parent / "data" / "ownership.sqlite3")))


def ownership_for_tile(tile: TileAddress) -> tuple[str, int]:
    """Read the durable, fixed-set ownership record for one tile."""
    OWNERSHIP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(OWNERSHIP_DB_PATH, timeout=5.0) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS tile_owners (tile_id TEXT PRIMARY KEY, shard_id TEXT NOT NULL, epoch INTEGER NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO tile_owners VALUES (?, ?, 1)",
                           (tile.tile_id, shard_for_tile(tile)))
        row = connection.execute("SELECT shard_id, epoch FROM tile_owners WHERE tile_id = ?", (tile.tile_id,)).fetchone()
    return row[0], row[1]


def ownership_db() -> sqlite3.Connection:
    OWNERSHIP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(OWNERSHIP_DB_PATH, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS transfers (
        tile_id TEXT PRIMARY KEY, source TEXT NOT NULL, destination TEXT NOT NULL,
        old_epoch INTEGER NOT NULL, phase TEXT NOT NULL)""")
    return connection


def pending_transfer(tile_id: str) -> sqlite3.Row | None:
    with ownership_db() as connection:
        return connection.execute("SELECT * FROM transfers WHERE tile_id = ?", (tile_id,)).fetchone()


async def transfer_tile(tile: TileAddress, destination: str) -> dict:
    """Resume a fenced single-tile transfer until its new owner is active."""
    async with canvas_lock:
        if destination not in configured_shard_ids():
            raise HTTPException(status_code=422, detail="unknown destination shard")
        owner, epoch = ownership_for_tile(tile)
        with ownership_db() as connection:
            transfer = connection.execute("SELECT * FROM transfers WHERE tile_id = ?", (tile.tile_id,)).fetchone()
            if transfer is None:
                if destination == owner:
                    return {"tile_id": tile.tile_id, "owner": owner, "epoch": epoch}
                connection.execute("INSERT INTO transfers VALUES (?, ?, ?, ?, 'preparing')",
                                   (tile.tile_id, owner, destination, epoch))
                source, old_epoch = owner, epoch
            else:
                if transfer["destination"] != destination:
                    raise HTTPException(status_code=409, detail="different transfer pending")
                source, old_epoch = transfer["source"], transfer["old_epoch"]
        # Source fence commits before any destination activation. Repeatable after
        # a crash; the source's snapshot is stable once fenced.
        await request_shard(source, "POST", f"/tiles/{tile.tile_id}/fence", params={"epoch": old_epoch})
        with ownership_db() as connection:
            connection.execute("UPDATE transfers SET phase = 'fenced' WHERE tile_id = ?", (tile.tile_id,))
        snapshot = await request_shard(source, "GET", f"/tiles/{tile.tile_id}/snapshot")
        operations = []
        after = 0
        while True:
            page = await request_shard(source, "GET", f"/tiles/{tile.tile_id}/operations",
                                       params={"after": after, "limit": 1000})
            batch = page["operations"]
            operations.extend(batch)
            if not batch:
                break
            after = batch[-1]["sequence"]
        try:
            destination_snapshot = await request_shard(destination, "GET", f"/tiles/{tile.tile_id}/snapshot")
        except ShardRequestError as exc:
            if exc.status_code != 404:
                raise
            destination_snapshot = None
        if not destination_snapshot or not destination_snapshot["active"]:
            await request_shard(destination, "POST", f"/tiles/{tile.tile_id}/install",
                                body={"epoch": old_epoch + 1, "last_sequence": snapshot["last_sequence"],
                                      "pixels": snapshot["pixels"], "operations": operations})
        elif (destination_snapshot["epoch"] != old_epoch + 1 or
              destination_snapshot["last_sequence"] < snapshot["last_sequence"]):
            raise HTTPException(status_code=409, detail="destination state does not match transfer")
        with ownership_db() as connection:
            connection.execute("UPDATE transfers SET phase = 'installed' WHERE tile_id = ?", (tile.tile_id,))
        await request_shard(destination, "POST", f"/tiles/{tile.tile_id}/activate", params={"epoch": old_epoch + 1})
        with ownership_db() as connection:
            connection.execute("UPDATE tile_owners SET shard_id = ?, epoch = ? WHERE tile_id = ?",
                               (destination, old_epoch + 1, tile.tile_id))
            connection.execute("DELETE FROM transfers WHERE tile_id = ?", (tile.tile_id,))
        return {"tile_id": tile.tile_id, "owner": destination, "epoch": old_epoch + 1}


class ShardRequestError(Exception):
    """An owner shard could not complete a routed coordinator request."""

    def __init__(self, shard_id: str, detail: str, status_code: int = 503):
        super().__init__(detail)
        self.shard_id = shard_id
        self.detail = detail
        self.status_code = status_code


class DrawRequest(BaseModel):
    x: int = Field(strict=True, ge=0, lt=1000)
    y: int = Field(strict=True, ge=0, lt=1000)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    operation_id: str | None = Field(default=None, min_length=1, max_length=100)


def session_for(token: str | None) -> tuple[str, str]:
    """Return a known session token and user ID, issuing them when necessary."""
    with ownership_db() as connection:
        ensure_sessions_table(connection)
        now = int(time.time())
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        if token is not None:
            row = connection.execute("SELECT user_id FROM sessions WHERE token = ?", (token,)).fetchone()
            if row is not None:
                connection.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                                   (now + SESSION_TTL_SECONDS, token))
                sessions[token] = row["user_id"]
                return token, row["user_id"]
        new_token = secrets.token_urlsafe(32)
        user_id = str(uuid.uuid4())
        connection.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                           (new_token, user_id, now + SESSION_TTL_SECONDS))
    sessions[new_token] = user_id
    return new_token, user_id


def known_session(token: str | None) -> bool:
    if token is None:
        return False
    with ownership_db() as connection:
        ensure_sessions_table(connection)
        return connection.execute("SELECT 1 FROM sessions WHERE token = ? AND expires_at > ?",
                                  (token, int(time.time()))).fetchone() is not None


def ensure_sessions_table(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at INTEGER NOT NULL)")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    if "expires_at" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN expires_at INTEGER")
        connection.execute("UPDATE sessions SET expires_at = ? WHERE expires_at IS NULL",
                           (int(time.time()) + SESSION_TTL_SECONDS,))


def public_pixels() -> dict[str, str]:
    return {key: value["color"] for key, value in pixels.items()}


def configured_shards() -> list[tuple[str, str]]:
    """Parse comma-separated shard-id=base-url entries from environment."""
    raw = os.getenv("SHARD_URLS") or DEFAULT_SHARD_URLS
    targets: list[tuple[str, str]] = []
    for entry in raw.split(","):
        shard_id, separator, url = entry.strip().partition("=")
        if not separator or not shard_id.strip() or not url.strip():
            raise ValueError("SHARD_URLS entries must use shard-id=http(s)://host:port")
        targets.append((shard_id.strip(), url.strip().rstrip("/")))
    return targets


def shard_urls_by_id() -> dict[str, str]:
    urls = dict(configured_shards())
    shard_ids = set(configured_shard_ids())
    if not shard_ids.issubset(urls):
        raise ValueError("SHARD_URLS must include every shard in SHARD_IDS")
    return urls


async def request_shard(
    shard_id: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Send one internal API request to the explicitly selected shard."""
    try:
        url = shard_urls_by_id()[shard_id]
    except (KeyError, ValueError) as exc:
        raise ShardRequestError(shard_id, "shard routing configuration is incomplete") from exc
    try:
        async with httpx.AsyncClient(timeout=SHARD_OPERATION_TIMEOUT) as client:
            response = await client.request(
                method, f"{url}{path}", params=params, json=body
            )
            response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("shard response must be a JSON object")
        return result
    except httpx.HTTPStatusError as exc:
        logger.warning("Shard %s rejected %s %s: %s", shard_id, method, path, exc)
        raise ShardRequestError(shard_id, "owner shard rejected the request", exc.response.status_code) from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Shard request failed for %s: %s %s: %s", shard_id, method, path, exc)
        raise ShardRequestError(shard_id, "owner shard is unavailable", 503) from exc


async def load_pixels_from_shards() -> dict[str, dict[str, str]]:
    """Read and combine current pixel records from every configured shard."""
    shard_ids = configured_shard_ids()
    results = await asyncio.gather(
        *(request_shard(shard_id, "GET", "/pixels") for shard_id in shard_ids)
    )
    state: dict[str, dict[str, str]] = {}
    for shard_id, result in zip(shard_ids, results):
        for pixel in result.get("pixels", []):
            tile = tile_for_pixel(pixel["x"], pixel["y"])
            if ownership_for_tile(tile)[0] == shard_id:
                state[f"{pixel['x']},{pixel['y']}"] = {
                    "color": pixel["color"], "owner": pixel["owner"],
                }
    return state


async def inspect_shard(shard_id: str, url: str) -> dict:
    result = {"shard_id": shard_id, "url": url}
    try:
        async with httpx.AsyncClient(timeout=SHARD_HEALTH_TIMEOUT) as client:
            response = await client.get(f"{url}/health")
            response.raise_for_status()
        health = response.json()
        if (
            not isinstance(health, dict)
            or health.get("shard_id") != shard_id
            or health.get("status") != "healthy"
        ):
            return {**result, "status": "unhealthy", "detail": "shard identity or health check mismatch"}
        return {
            **result,
            "status": "healthy",
            "database": health.get("database", "unknown"),
            "version": health.get("version", "unknown"),
        }
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Shard health check failed for %s at %s: %s", shard_id, url, exc)
        return {**result, "status": "unavailable", "detail": str(exc) or type(exc).__name__}


@app.get("/health")
def coordinator_health() -> dict:
    return {"service": "coordinator", "status": "healthy"}


@app.get("/admin/shards")
async def shard_health_overview() -> dict:
    shards = await asyncio.gather(
        *(inspect_shard(shard_id, url) for shard_id, url in configured_shards())
    )
    return {"shards": shards}


@app.post("/admin/tiles/{x}/{y}/transfer")
async def transfer_endpoint(x: int, y: int, destination: str,
                            x_admin_token: str | None = Header(default=None)) -> dict:
    expected_token = os.getenv("ADMIN_TOKEN")
    if not expected_token or not secrets.compare_digest(x_admin_token or "", expected_token):
        raise HTTPException(status_code=403, detail="admin token required")
    try:
        tile = tile_for_pixel(x, y)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await transfer_tile(tile, destination)


@app.get("/")
def home(request: Request):
    token, _ = session_for(request.cookies.get(SESSION_COOKIE))
    response = FileResponse(Path(__file__).parent / "index.html")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response


@app.get("/state")
async def get_state(request: Request):
    token, _ = session_for(request.cookies.get(SESSION_COOKIE))
    try:
        async with canvas_lock:
            pixels.clear()
            pixels.update(await load_pixels_from_shards())
            state = public_pixels()
    except (ShardRequestError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="canvas storage is unavailable") from exc
    response = JSONResponse({"pixels": state})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response


@app.post("/draw")
async def draw(request: DrawRequest, http_request: Request):
    token, user_id = session_for(http_request.cookies.get(SESSION_COOKIE))
    tile = tile_for_pixel(request.x, request.y)
    color = request.color.upper()
    operation_id = request.operation_id or str(uuid.uuid4())
    key = f"{request.x},{request.y}"
    try:
        async with canvas_lock:
            if pending_transfer(tile.tile_id):
                raise HTTPException(status_code=503, detail="tile transfer pending; retry later")
            shard_id, epoch = ownership_for_tile(tile)
            committed = await request_shard(
                shard_id,
                "POST",
                "/pixels",
                body={
                    "x": request.x,
                    "y": request.y,
                    "tile_id": tile.tile_id,
                    "color": color,
                    "owner": user_id,
                    "epoch": epoch,
                    "operation_id": operation_id,
                },
            )
            if any(
                committed.get(field) != expected
                for field, expected in {
                    "status": "committed",
                    "tile_id": tile.tile_id,
                    "color": color,
                    "owner": user_id,
                }.items()
            ) or (not committed.get("duplicate") and committed.get("shard_id") != shard_id):
                raise ShardRequestError(shard_id, "owner shard returned an invalid commit receipt", 502)
            if not committed.get("duplicate"):
                pixels[key] = {"color": color, "owner": user_id}
                await manager.broadcast({"type": "draw", "x": request.x, "y": request.y, "color": color})
    except ShardRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response = JSONResponse({
        "status": "ok", "x": request.x, "y": request.y, "color": color,
        "tile_id": tile.tile_id, "shard_id": shard_id,
        "operation_id": operation_id, "epoch": committed.get("epoch", 1), "sequence": committed.get("sequence"),
    })
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response


@app.delete("/clear")
async def clear_my_pixels(request: Request):
    token, user_id = session_for(request.cookies.get(SESSION_COOKIE))
    shard_ids = configured_shard_ids()
    try:
        async with canvas_lock:
            with ownership_db() as connection:
                if connection.execute("SELECT 1 FROM transfers LIMIT 1").fetchone():
                    raise HTTPException(status_code=503, detail="tile transfer pending; retry later")
            results = await asyncio.gather(
                *(
                    request_shard(
                        shard_id, "DELETE", "/pixels", params={"user_id": user_id}
                    )
                    for shard_id in shard_ids
                ),
                return_exceptions=True,
            )
            failure = next((result for result in results if isinstance(result, Exception)), None)
            coordinates = [
                [pixel["x"], pixel["y"]]
                for result in results
                if not isinstance(result, Exception)
                for pixel in result["removed"]
            ]
            # A migrated tile can leave a fenced copy on its old shard. Only
            # broadcast removals absent from the authoritative owner now.
            try:
                refreshed = await load_pixels_from_shards()
            except (ShardRequestError, ValueError):
                refreshed = None
            if refreshed is not None:
                coordinates = [[x, y] for x, y in sorted({tuple(point) for point in coordinates})
                               if f"{x},{y}" not in refreshed]
                pixels.clear()
                pixels.update(refreshed)
                if failure is None:
                    await manager.broadcast({"type": "clear", "pixels": coordinates})
                else:
                    await manager.broadcast({"type": "state", "pixels": public_pixels()})
            else:
                await manager.broadcast({"type": "clear", "pixels": coordinates})
                if failure is not None:
                    await manager.resync_all()
            if failure is not None:
                return JSONResponse({"status": "partial", "cleared": len(coordinates),
                                     "detail": "Some shards could not be cleared; refresh state before retrying"},
                                    status_code=503)
    except ShardRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response = JSONResponse({"status": "ok", "cleared": len(coordinates)})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE)
    if not known_session(token):
        await websocket.close(code=1008, reason="Open the canvas to create a session")
        return

    try:
        await websocket.accept()
        # Register and snapshot under the same lock as mutations+broadcasts.
        # A mutation is therefore either included in this snapshot or delivered
        # after it as a WebSocket event, never lost between the two.
        async with canvas_lock:
            try:
                pixels.clear()
                pixels.update(await load_pixels_from_shards())
            except (ShardRequestError, ValueError):
                await websocket.close(code=1011, reason="Canvas storage is unavailable")
                return
            manager.active_connections.append(websocket)
            await asyncio.wait_for(websocket.send_json({"type": "state", "pixels": public_pixels()}), timeout=2.0)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # Failed sends and other connection errors must not leak stale sockets.
        logger.exception("WebSocket session ended unexpectedly")
    finally:
        manager.disconnect(websocket)
