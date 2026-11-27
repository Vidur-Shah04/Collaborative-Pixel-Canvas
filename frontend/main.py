"""Browser-facing coordinator for the collaborative pixel canvas."""

from pathlib import Path
from contextlib import asynccontextmanager, suppress
import asyncio
import logging
import os
import secrets
import uuid
import sqlite3
import time
import json
import hashlib

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
                try:
                    await asyncio.wait_for(connection.close(code=1012, reason="Update delivery failed; resync required"), timeout=1.0)
                except Exception:
                    logger.debug("WebSocket was already closed after failed delivery")

    async def resync_all(self) -> None:
        """Force browsers to fetch an authoritative snapshot after an unknown commit."""
        for connection in self.active_connections.copy():
            self.disconnect(connection)
            try:
                await asyncio.wait_for(connection.close(code=1012, reason="Canvas resynchronizing"), timeout=2.0)
            except Exception:
                logger.exception("WebSocket resynchronization close failed")


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    recovery_task = asyncio.create_task(recover_pending_work())
    try:
        yield
    finally:
        recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await recovery_task


app = FastAPI(title="Collaborative Pixel Canvas", lifespan=lifespan)
# Serializes state changes and their broadcasts with WebSocket state snapshots.
canvas_lock = asyncio.Lock()
tile_transfer_locks: dict[str, asyncio.Lock] = {}

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
    connection.execute("""CREATE TABLE IF NOT EXISTS clear_operations (
        operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, participants TEXT NOT NULL,
        phase TEXT NOT NULL, coordinates TEXT NOT NULL DEFAULT '[]',
        history_finalized INTEGER NOT NULL DEFAULT 0)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS user_action_counters (
        user_id TEXT PRIMARY KEY, next_action INTEGER NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS user_draw_actions (
        operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, action_order INTEGER NOT NULL,
        shard_id TEXT NOT NULL, tile_id TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
        color TEXT NOT NULL, previous_pixel TEXT, epoch INTEGER NOT NULL,
        sequence INTEGER, status TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0,
        UNIQUE(user_id, action_order))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS undo_attempts (
        attempt_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
        target_operation_id TEXT, response TEXT)""")
    clear_columns = {row[1] for row in connection.execute("PRAGMA table_info(clear_operations)")}
    if "history_finalized" not in clear_columns:
        connection.execute("ALTER TABLE clear_operations ADD COLUMN history_finalized INTEGER NOT NULL DEFAULT 0")
        # Earlier completed Clears were not ordered with tile operations and
        # cannot be backfilled into replay history without inventing an order.
        connection.execute("UPDATE clear_operations SET history_finalized=1 WHERE phase='committed'")
    return connection


async def finish_clear(operation_id: str) -> dict:
    """Resume a durable clear by preparing all shards before idempotent commits."""
    with ownership_db() as connection:
        op = connection.execute("SELECT * FROM clear_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise ShardRequestError("coordinator", "clear operation not found", 404)
    participants = json.loads(op["participants"])
    prepared = await asyncio.gather(*(request_shard(s, "POST", f"/clear/{operation_id}/prepare",
        params={"user_id": op["user_id"]}) for s in participants))
    if any(result.get("status") != "prepared" or result.get("operation_id") != operation_id
           or not isinstance(result.get("coordinates"), list) for result in prepared):
        raise ShardRequestError("coordinator", "a shard returned an invalid clear prepare receipt", 502)
    for result in prepared:
        if any(not isinstance(point, dict) or type(point.get("x")) is not int
               or type(point.get("y")) is not int for point in result["coordinates"]):
            raise ShardRequestError("coordinator", "a shard returned invalid clear coordinates", 502)
    coordinates = sorted({(p["x"], p["y"]) for result in prepared for p in result["coordinates"]})
    with ownership_db() as connection:
        connection.execute("UPDATE clear_operations SET phase='committing', coordinates=? WHERE operation_id=?",
                           (json.dumps(coordinates), operation_id))
    committed = await asyncio.gather(*(request_shard(s, "POST", f"/clear/{operation_id}/commit") for s in participants))
    if any(result.get("status") != "committed" or result.get("operation_id") != operation_id
           for result in committed):
        raise ShardRequestError("coordinator", "a shard returned an invalid clear commit receipt", 502)
    with ownership_db() as connection:
        connection.execute("UPDATE clear_operations SET phase='finalizing' WHERE operation_id=?", (operation_id,))
    finalized = await asyncio.gather(*(request_shard(s, "POST", f"/clear/{operation_id}/finalize")
                                       for s in participants))
    if any(result.get("status") != "finalized" or result.get("operation_id") != operation_id
           for result in finalized):
        raise ShardRequestError("coordinator", "a shard returned an invalid clear history receipt", 502)
    with ownership_db() as connection:
        connection.execute("UPDATE clear_operations SET phase='committed', history_finalized=1 WHERE operation_id=?", (operation_id,))
    positions = [position for result in finalized for position in result.get("positions", [])]
    return {"coordinates": coordinates, "participants": participants, "positions": positions}


def pending_transfer(tile_id: str) -> sqlite3.Row | None:
    with ownership_db() as connection:
        return connection.execute("SELECT * FROM transfers WHERE tile_id = ?", (tile_id,)).fetchone()


def tile_from_id(tile_id: str) -> TileAddress:
    try:
        row, column = tile_id.removeprefix("tile-").split("-")
        tile = tile_for_pixel(int(column) * 25, int(row) * 25)
    except (ValueError, IndexError) as exc:
        raise ValueError(f"invalid transfer tile ID: {tile_id}") from exc
    if tile.tile_id != tile_id:
        raise ValueError(f"invalid transfer tile ID: {tile_id}")
    return tile


async def recover_pending_transfers_once() -> int:
    """Retry each durable transfer without blocking unrelated tiles."""
    with ownership_db() as connection:
        transfers = connection.execute("SELECT tile_id, destination FROM transfers ORDER BY tile_id").fetchall()
    recovered = 0
    for transfer in transfers:
        try:
            await transfer_tile(tile_from_id(transfer["tile_id"]), transfer["destination"])
            recovered += 1
        except Exception:
            logger.exception("Tile transfer recovery pending for %s", transfer["tile_id"])
    return recovered


async def recover_pending_transfers() -> None:
    while True:
        try:
            await recover_pending_transfers_once()
        except Exception:
            logger.exception("Could not scan pending tile transfers; retrying")
        await asyncio.sleep(1)


async def recover_pending_work() -> None:
    """Retry durable clear intents and tile transfers after coordinator restart."""
    while True:
        try:
            with ownership_db() as connection:
                clears = connection.execute(
                    "SELECT operation_id FROM clear_operations WHERE phase != 'committed' OR history_finalized=0"
                ).fetchall()
            for clear in clears:
                try:
                    async with canvas_lock:
                        with ownership_db() as connection:
                            row = connection.execute(
                                "SELECT phase, history_finalized FROM clear_operations WHERE operation_id=?",
                                (clear["operation_id"],),
                            ).fetchone()
                        if row is None or (row["phase"] == "committed" and row["history_finalized"]):
                            continue
                        result = await finish_clear(clear["operation_id"])
                        try:
                            state = await load_canvas_snapshot()
                            pixels.clear()
                            pixels.update(state["cache"])
                            await manager.broadcast({"type": "state", "pixels": state["pixels"],
                                                     "positions": state["positions"]})
                        except (ShardRequestError, ValueError):
                            # Every participant committed; discard coordinates from
                            # the cache and let the next snapshot retry the full read.
                            for x, y in result["coordinates"]:
                                pixels.pop(f"{x},{y}", None)
                            await manager.broadcast({"type": "clear", "pixels": [list(point) for point in result["coordinates"]],
                                                     "positions": result["positions"]})
                except Exception:
                    logger.exception("Clear recovery pending for %s", clear["operation_id"])
            await recover_pending_transfers_once()
        except Exception:
            logger.exception("Could not scan pending durable work; retrying")
        await asyncio.sleep(1)


async def copy_tile_operations(source: str, destination: str, tile_id: str,
                               next_epoch: int, after: int, *, until: int | None = None,
                               max_pages: int | None = None) -> int:
    """Stream bounded log pages; return the last copied sequence."""
    pages = 0
    while until is None or after < until:
        page = await request_shard(source, "GET", f"/tiles/{tile_id}/operations",
                                   params={"after": after, "limit": 100})
        operations = [operation for operation in page["operations"]
                      if until is None or operation["sequence"] <= until]
        if not operations:
            break
        if any(operation["sequence"] != after + index + 1
               for index, operation in enumerate(operations)):
            raise ShardRequestError(source, "source operation log has a gap", 502)
        receipt = await request_shard(destination, "POST", f"/tiles/{tile_id}/import-operations",
                                      body={"epoch": next_epoch, "operations": operations})
        after = operations[-1]["sequence"]
        if receipt.get("last_sequence", 0) < after:
            raise ShardRequestError(destination, "destination did not import operations", 502)
        pages += 1
        if max_pages is not None and pages >= max_pages:
            break
    if until is not None and after < until:
        raise ShardRequestError(source, "source operation log ended before snapshot", 502)
    return after


async def transfer_tile(tile: TileAddress, destination: str) -> dict:
    """Copy while writes continue, then fence briefly and cut over."""
    async with tile_transfer_locks.setdefault(tile.tile_id, asyncio.Lock()):
        async with canvas_lock:
            with ownership_db() as connection:
                if connection.execute("SELECT 1 FROM clear_operations WHERE phase != 'committed' LIMIT 1").fetchone():
                    raise HTTPException(status_code=503, detail="clear recovery pending; retry later")
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
                    source, old_epoch, phase = owner, epoch, "preparing"
                else:
                    if transfer["destination"] != destination:
                        raise HTTPException(status_code=409, detail="different transfer pending")
                    source, old_epoch, phase = transfer["source"], transfer["old_epoch"], transfer["phase"]

        tile_path = f"/tiles/{tile.tile_id}"
        next_epoch = old_epoch + 1
        if phase == "preparing":
            snapshot = await request_shard(source, "GET", f"{tile_path}/snapshot")
            if snapshot["epoch"] != old_epoch:
                raise ShardRequestError(source, "source epoch changed during copy", 409)
            await request_shard(destination, "POST", f"{tile_path}/install",
                                body={"epoch": next_epoch, "last_sequence": snapshot["last_sequence"],
                                      "pixels": snapshot["pixels"], "operations": []})
            await copy_tile_operations(source, destination, tile.tile_id, next_epoch, 0,
                                       until=snapshot["last_sequence"])
            await copy_tile_operations(source, destination, tile.tile_id, next_epoch,
                                       snapshot["last_sequence"], max_pages=5)
            # All earlier draws finish before this phase change. New draws for
            # this tile pause, but unrelated tiles keep working.
            async with canvas_lock:
                with ownership_db() as connection:
                    connection.execute("UPDATE transfers SET phase = 'fencing' WHERE tile_id = ?",
                                       (tile.tile_id,))
            phase = "fencing"

        if phase == "fencing":
            await request_shard(source, "POST", f"{tile_path}/fence", params={"epoch": old_epoch})
            with ownership_db() as connection:
                connection.execute("UPDATE transfers SET phase = 'fenced' WHERE tile_id = ?",
                                   (tile.tile_id,))
            phase = "fenced"

        if phase == "fenced":
            source_snapshot = await request_shard(source, "GET", f"{tile_path}/snapshot")
            if source_snapshot["epoch"] != old_epoch or source_snapshot["active"]:
                raise ShardRequestError(source, "source fence is not durable", 409)
            try:
                destination_snapshot = await request_shard(destination, "GET", f"{tile_path}/snapshot")
            except ShardRequestError as exc:
                if exc.status_code != 404:
                    raise
                # A transfer created by the previous fenced protocol may not
                # have installed its destination before the coordinator died.
                await request_shard(destination, "POST", f"{tile_path}/install",
                                    body={"epoch": next_epoch,
                                          "last_sequence": source_snapshot["last_sequence"],
                                          "pixels": source_snapshot["pixels"], "operations": []})
                await copy_tile_operations(source, destination, tile.tile_id, next_epoch, 0,
                                           until=source_snapshot["last_sequence"])
                destination_snapshot = await request_shard(destination, "GET", f"{tile_path}/snapshot")
            if destination_snapshot["epoch"] != next_epoch:
                raise ShardRequestError(destination, "destination epoch mismatch", 409)
            await copy_tile_operations(source, destination, tile.tile_id, next_epoch,
                                       destination_snapshot["last_sequence"],
                                       until=source_snapshot["last_sequence"])
            with ownership_db() as connection:
                connection.execute("UPDATE transfers SET phase = 'installed' WHERE tile_id = ?",
                                   (tile.tile_id,))

        async with canvas_lock:
            await request_shard(destination, "POST", f"{tile_path}/activate", params={"epoch": next_epoch})
            with ownership_db() as connection:
                connection.execute("UPDATE tile_owners SET shard_id = ?, epoch = ? WHERE tile_id = ?",
                                   (destination, next_epoch, tile.tile_id))
                connection.execute("DELETE FROM transfers WHERE tile_id = ?", (tile.tile_id,))
        return {"tile_id": tile.tile_id, "owner": destination, "epoch": next_epoch}


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


def canonical_canvas_hash(pixel_colors: dict[str, str]) -> str:
    """Hash visible colors in stable tile-row, tile-column, local-row order."""
    serialized = bytearray(b"collaborative-pixel-canvas-v1|1000|1000\n")
    ordered = []
    for coordinate, color in pixel_colors.items():
        try:
            x_text, y_text = coordinate.split(",", 1)
            x, y = int(x_text), int(y_text)
            tile = tile_for_pixel(x, y)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid canvas pixel coordinate") from exc
        if coordinate != f"{x},{y}":
            raise ValueError("canvas coordinates must use canonical decimal form")
        if (not isinstance(color, str) or len(color) != 7 or color[0] != "#"
                or any(character not in "0123456789abcdefABCDEF" for character in color[1:])):
            raise ValueError("invalid canvas pixel color")
        ordered.append((tile.tile_index, y % 25, x % 25, color.upper()))
    for tile_index, local_y, local_x, color in sorted(ordered):
        serialized.extend(f"{tile_index:04d},{local_y:02d},{local_x:02d},{color}\n".encode("ascii"))
    return hashlib.sha256(serialized).hexdigest()


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


async def load_canvas_snapshot() -> dict:
    """Read pixels and per-tile cursors while the coordinator lock is held."""
    shard_ids = configured_shard_ids()
    pixel_results, position_results = await asyncio.gather(
        asyncio.gather(*(request_shard(shard_id, "GET", "/pixels") for shard_id in shard_ids)),
        asyncio.gather(*(request_shard(shard_id, "GET", "/tiles/positions") for shard_id in shard_ids)),
    )
    state: dict[str, str] = {}
    cache: dict[str, dict[str, str]] = {}
    for shard_id, result in zip(shard_ids, pixel_results):
        for pixel in result.get("pixels", []):
            tile = tile_for_pixel(pixel["x"], pixel["y"])
            if ownership_for_tile(tile)[0] == shard_id:
                key = f"{pixel['x']},{pixel['y']}"
                state[key] = pixel["color"]
                cache[key] = {"color": pixel["color"], "owner": pixel["owner"]}
    positions: dict[str, dict[str, int]] = {}
    for result in position_results:
        for position in result.get("positions", []):
            tile = tile_from_id(position["tile_id"])
            if ownership_for_tile(tile)[0] != result.get("shard_id"):
                continue
            positions[position["tile_id"]] = {
                "epoch": position["epoch"], "sequence": position["last_sequence"],
            }
    return {"pixels": state, "positions": positions, "cache": cache}


async def commit_user_draw_action(operation_id: str) -> dict:
    """Complete or retry the shard write attached to a durable per-user action."""
    with ownership_db() as connection:
        action = connection.execute(
            "SELECT * FROM user_draw_actions WHERE operation_id=?", (operation_id,)
        ).fetchone()
    if action is None:
        raise ShardRequestError("coordinator", "draw action not found", 404)
    tile = tile_from_id(action["tile_id"])
    shard_id, epoch = ownership_for_tile(tile)
    result = await request_shard(shard_id, "POST", "/pixels", body={
        "x": action["x"], "y": action["y"], "tile_id": action["tile_id"],
        "color": action["color"], "owner": action["user_id"],
        "epoch": epoch, "operation_id": operation_id,
    })
    expected = {"status": "committed", "tile_id": action["tile_id"],
                "color": action["color"], "owner": action["user_id"]}
    if (any(result.get(field) != value for field, value in expected.items())
            or (not result.get("duplicate") and result.get("shard_id") != shard_id)):
        raise ShardRequestError(shard_id, "owner shard returned an invalid commit receipt", 502)
    with ownership_db() as connection:
        connection.execute(
            "UPDATE user_draw_actions SET shard_id=?, epoch=?, sequence=?, status='committed' WHERE operation_id=?",
            (shard_id, result["epoch"], result["sequence"], operation_id),
        )
    return result


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
    try:
        return await transfer_tile(tile, destination)
    except ShardRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


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
            state = await load_canvas_snapshot()
            pixels.clear()
            pixels.update(state["cache"])
    except (ShardRequestError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="canvas storage is unavailable") from exc
    response = JSONResponse({"pixels": state["pixels"], "positions": state["positions"]})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response


@app.get("/hash")
async def get_canvas_hash():
    """Return a placement-independent SHA-256 hash of visible canvas pixels."""
    try:
        async with canvas_lock:
            state = await load_canvas_snapshot()
    except (ShardRequestError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="canvas storage is unavailable") from exc
    return {
        "algorithm": "sha256",
        "serialization": "collaborative-pixel-canvas-v1; tile row-major, local y/x; uppercase #RRGGBB; colors only",
        "pixels": len(state["pixels"]),
        "hash": canonical_canvas_hash(state["pixels"]),
    }


@app.post("/draw")
async def draw(request: DrawRequest, http_request: Request):
    token, user_id = session_for(http_request.cookies.get(SESSION_COOKIE))
    tile = tile_for_pixel(request.x, request.y)
    color = request.color.upper()
    operation_id = request.operation_id or str(uuid.uuid4())
    key = f"{request.x},{request.y}"
    try:
        async with canvas_lock:
            with ownership_db() as connection:
                if connection.execute("SELECT 1 FROM clear_operations WHERE phase != 'committed' LIMIT 1").fetchone():
                    raise HTTPException(status_code=503, detail="clear recovery pending; retry later")
            transfer = pending_transfer(tile.tile_id)
            if transfer is not None and transfer["phase"] != "preparing":
                raise HTTPException(status_code=503, detail="tile transfer pending; retry later")
            shard_id, epoch = ownership_for_tile(tile)
            with ownership_db() as connection:
                action = connection.execute(
                    "SELECT * FROM user_draw_actions WHERE operation_id=?", (operation_id,)
                ).fetchone()
            if action is not None:
                if (action["user_id"] != user_id or action["x"] != request.x
                        or action["y"] != request.y or action["color"] != color):
                    raise HTTPException(status_code=409, detail="draw operation ID reused with different action")
            else:
                shard_pixels = await request_shard(shard_id, "GET", "/pixels")
                prior = next((record for record in shard_pixels.get("pixels", [])
                              if record.get("x") == request.x and record.get("y") == request.y), None)
                with ownership_db() as connection:
                    row = connection.execute(
                        "SELECT next_action FROM user_action_counters WHERE user_id=?", (user_id,)
                    ).fetchone()
                    action_order = row["next_action"] if row else 1
                    connection.execute(
                        "INSERT INTO user_action_counters(user_id,next_action) VALUES(?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET next_action=excluded.next_action",
                        (user_id, action_order + 1),
                    )
                    connection.execute(
                        """INSERT INTO user_draw_actions(
                            operation_id,user_id,action_order,shard_id,tile_id,x,y,color,
                            previous_pixel,epoch,status) VALUES(?,?,?,?,?,?,?,?,?,?,'pending')""",
                        (operation_id, user_id, action_order, shard_id, tile.tile_id,
                         request.x, request.y, color, json.dumps(prior) if prior else None, epoch),
                    )
            committed = await commit_user_draw_action(operation_id)
            if not committed.get("duplicate"):
                pixels[key] = {"color": color, "owner": user_id}
                await manager.broadcast({"type": "draw", "x": request.x, "y": request.y, "color": color,
                                         "tile_id": tile.tile_id, "epoch": committed["epoch"],
                                         "sequence": committed["sequence"]})
    except ShardRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response = JSONResponse({
        "status": "ok", "x": request.x, "y": request.y, "color": color,
        "tile_id": tile.tile_id, "shard_id": shard_id,
        "operation_id": operation_id, "epoch": committed.get("epoch", 1), "sequence": committed.get("sequence"),
    })
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response


@app.post("/undo")
async def undo_last_draw(request: Request, x_operation_id: str | None = Header(default=None)):
    token, user_id = session_for(request.cookies.get(SESSION_COOKIE))
    attempt_id = x_operation_id or str(uuid.uuid4())
    if not attempt_id or len(attempt_id) > 100:
        raise HTTPException(status_code=422, detail="invalid undo operation ID")
    try:
        async with canvas_lock:
            with ownership_db() as connection:
                if connection.execute("SELECT 1 FROM clear_operations WHERE phase != 'committed' LIMIT 1").fetchone():
                    raise HTTPException(status_code=503, detail="clear recovery pending; retry later")
            with ownership_db() as connection:
                attempt = connection.execute(
                    "SELECT * FROM undo_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if attempt is not None and attempt["user_id"] != user_id:
                    raise HTTPException(status_code=409, detail="undo operation ID belongs to another session")
                if attempt is not None and attempt["status"] == "complete":
                    result = json.loads(attempt["response"])
                else:
                    used = connection.execute(
                        "SELECT COUNT(*) FROM undo_attempts WHERE user_id=?", (user_id,)
                    ).fetchone()[0]
                    if attempt is None and used >= 10:
                        raise HTTPException(status_code=429, detail="maximum of ten undo attempts reached")
                    if attempt is None:
                        connection.execute(
                            "INSERT INTO undo_attempts(attempt_id,user_id,status) VALUES(?,?,'pending')",
                            (attempt_id, user_id),
                        )
                    result = None
            if result is None:
                with ownership_db() as connection:
                    action = connection.execute(
                        "SELECT * FROM user_draw_actions WHERE user_id=? AND undone=0 "
                        "ORDER BY action_order DESC LIMIT 1", (user_id,),
                    ).fetchone()
                if action is None:
                    result = {"status": "empty", "operation_id": attempt_id,
                              "attempts_used": used + (1 if attempt is None else 0)}
                    target_operation_id = None
                else:
                    # Reconcile a draw whose shard receipt was lost after its
                    # durable intent was recorded, then undo it exactly once.
                    if action["status"] == "pending":
                        await commit_user_draw_action(action["operation_id"])
                        with ownership_db() as connection:
                            action = connection.execute(
                                "SELECT * FROM user_draw_actions WHERE operation_id=?",
                                (action["operation_id"],),
                            ).fetchone()
                    tile = tile_from_id(action["tile_id"])
                    transfer = pending_transfer(tile.tile_id)
                    if transfer is not None and transfer["phase"] != "preparing":
                        raise HTTPException(status_code=503, detail="tile transfer pending; retry later")
                    shard_id, epoch = ownership_for_tile(tile)
                    undo_id = f"undo-{hashlib.sha256(attempt_id.encode()).hexdigest()}"
                    result = await request_shard(shard_id, "POST", "/undo", body={
                        "x": action["x"], "y": action["y"], "tile_id": action["tile_id"],
                        "owner": user_id, "epoch": epoch,
                        "action_operation_id": action["operation_id"],
                        "operation_id": undo_id, "expected_color": action["color"],
                        "previous_pixel": json.loads(action["previous_pixel"])
                            if action["previous_pixel"] else None,
                    })
                    if (result.get("status") not in {"undone", "skipped"}
                            or result.get("operation_id") != undo_id
                            or result.get("tile_id") != action["tile_id"]
                            or type(result.get("sequence")) is not int):
                        raise ShardRequestError(shard_id, "owner shard returned an invalid undo receipt", 502)
                    target_operation_id = action["operation_id"]
                    if result["status"] == "undone":
                        restored = result.get("previous_pixel")
                        if restored is None:
                            pixels.pop(f"{action['x']},{action['y']}", None)
                        else:
                            pixels[f"{action['x']},{action['y']}"] = {
                                "color": restored["color"].upper(), "owner": restored["owner"],
                            }
                    result = {**result, "attempts_used": used + (1 if attempt is None else 0)}
                with ownership_db() as connection:
                    if target_operation_id is not None:
                        connection.execute(
                            "UPDATE user_draw_actions SET undone=1 WHERE operation_id=?",
                            (target_operation_id,),
                        )
                    connection.execute(
                        "UPDATE undo_attempts SET status='complete',target_operation_id=?,response=? "
                        "WHERE attempt_id=?",
                        (target_operation_id, json.dumps(result), attempt_id),
                    )
                if action is not None:
                    await manager.broadcast({
                        "type": "undo", "x": result["x"], "y": result["y"],
                        "tile_id": result["tile_id"], "epoch": result["epoch"],
                        "sequence": result["sequence"], "outcome": result["status"],
                        "pixel": result.get("previous_pixel"),
                    })
    except ShardRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response = JSONResponse(result)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response


@app.delete("/clear")
async def clear_my_pixels(request: Request):
    token, user_id = session_for(request.cookies.get(SESSION_COOKIE))
    shard_ids = configured_shard_ids()
    operation_id = request.headers.get("X-Operation-ID") or str(uuid.uuid4())
    try:
        async with canvas_lock:
            with ownership_db() as connection:
                if connection.execute("SELECT 1 FROM transfers LIMIT 1").fetchone():
                    raise HTTPException(status_code=503, detail="tile transfer pending; retry later")
            with ownership_db() as connection:
                old = connection.execute("SELECT user_id, phase, coordinates, history_finalized FROM clear_operations WHERE operation_id=?", (operation_id,)).fetchone()
                if old and old["user_id"] != user_id:
                    raise HTTPException(status_code=409, detail="operation ID already belongs to another session")
                if old is None and connection.execute(
                    "SELECT 1 FROM clear_operations WHERE phase != 'committed' LIMIT 1"
                ).fetchone():
                    raise HTTPException(status_code=503, detail="clear recovery pending; retry later")
                already_committed = old is not None and old["phase"] == "committed" and old["history_finalized"]
                connection.execute("INSERT OR IGNORE INTO clear_operations(operation_id,user_id,participants,phase) VALUES(?,?,?,'preparing')",
                                   (operation_id, user_id, json.dumps(shard_ids)))
                committed_coordinates = json.loads(old["coordinates"]) if already_committed else None
            if already_committed:
                coordinates = [list(point) for point in committed_coordinates]
            else:
                result = await finish_clear(operation_id)
                coordinates = [list(point) for point in result["coordinates"]]
            try:
                refreshed = await load_pixels_from_shards()
                pixels.clear()
                pixels.update(refreshed)
            except (ShardRequestError, ValueError):
                # All commit receipts were already validated, so the Clear
                # succeeded even if an unrelated state read is temporarily down.
                for x, y in coordinates:
                    pixels.pop(f"{x},{y}", None)
            if not already_committed:
                await manager.broadcast({"type": "clear", "pixels": coordinates,
                                         "positions": result["positions"]})
    except ShardRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response = JSONResponse({"status": "ok", "cleared": len(coordinates), "operation_id": operation_id})
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
                state = await load_canvas_snapshot()
                pixels.clear()
                pixels.update(state["cache"])
            except (ShardRequestError, ValueError):
                await websocket.close(code=1011, reason="Canvas storage is unavailable")
                return
            manager.active_connections.append(websocket)
            await asyncio.wait_for(websocket.send_json({"type": "state", "pixels": state["pixels"],
                                                        "positions": state["positions"]}), timeout=2.0)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # Failed sends and other connection errors must not leak stale sockets.
        logger.exception("WebSocket session ended unexpectedly")
    finally:
        manager.disconnect(websocket)
