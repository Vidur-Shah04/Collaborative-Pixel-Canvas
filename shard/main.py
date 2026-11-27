"""Small, independently deployable shard service for infrastructure checks."""

from contextlib import asynccontextmanager, contextmanager
import os
from pathlib import Path
import sqlite3
import json
import uuid
from fastapi import FastAPI, HTTPException
from fastapi import Query
from pydantic import BaseModel, Field
from shared.tiles import configured_shard_ids, shard_for_tile, tile_for_pixel

SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.2.0")


class RecordRequest(BaseModel):
    record_key: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=4000)


class PixelWriteRequest(BaseModel):
    x: int = Field(strict=True, ge=0, lt=1000)
    y: int = Field(strict=True, ge=0, lt=1000)
    tile_id: str = Field(min_length=1, max_length=32)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    owner: str = Field(min_length=1, max_length=100)
    epoch: int = Field(default=1, ge=1)
    operation_id: str | None = Field(default=None, min_length=1, max_length=100)


class TileInstallRequest(BaseModel):
    epoch: int = Field(ge=2)
    last_sequence: int = Field(ge=0)
    pixels: list[dict]
    operations: list[dict] = []


class TileOperationsRequest(BaseModel):
    epoch: int = Field(ge=2)
    operations: list[dict] = Field(max_length=1000)


class UndoRequest(BaseModel):
    x: int = Field(strict=True, ge=0, lt=1000)
    y: int = Field(strict=True, ge=0, lt=1000)
    tile_id: str = Field(min_length=1, max_length=32)
    owner: str = Field(min_length=1, max_length=100)
    epoch: int = Field(ge=1)
    action_operation_id: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=1, max_length=100)
    expected_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    previous_pixel: dict | None = None


def create_app(
    shard_id: str | None = None,
    port: int | None = None,
    database_path: str | Path | None = None,
) -> FastAPI:
    identity = shard_id or os.getenv("SHARD_ID", "shard-1")
    service_port = port or int(os.getenv("PORT", "8001"))
    shard_ids = configured_shard_ids()
    default_database = Path(__file__).parent / "data" / "shard.sqlite3"
    db_path = Path(database_path or os.getenv("SHARD_DB_PATH", str(default_database)))

    def connect() -> sqlite3.Connection:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def database_connection():
        connection = connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize_database() -> None:
        with database_connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS infrastructure_records (
                       record_key TEXT PRIMARY KEY,
                       value TEXT NOT NULL,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pixels (
                       x INTEGER NOT NULL,
                       y INTEGER NOT NULL,
                       tile_id TEXT NOT NULL,
                       color TEXT NOT NULL,
                       owner TEXT NOT NULL,
                       PRIMARY KEY (x, y)
                   )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS pixels_owner_idx ON pixels(owner)")
            connection.execute("""CREATE TABLE IF NOT EXISTS clear_operations (
                operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, phase TEXT NOT NULL,
                coordinates TEXT NOT NULL DEFAULT '[]', history_finalized INTEGER NOT NULL DEFAULT 0)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(clear_operations)")}
            if "history_finalized" not in columns:
                connection.execute("ALTER TABLE clear_operations ADD COLUMN history_finalized INTEGER NOT NULL DEFAULT 0")
                # Old completed Clear rows have no event ordering, so do not
                # hold draws forever or pretend they can be replayed exactly.
                connection.execute("UPDATE clear_operations SET history_finalized=1 WHERE phase='committed'")
            connection.execute("""CREATE TABLE IF NOT EXISTS tile_ownership (
                tile_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL,
                next_sequence INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS pixel_operations (
                operation_id TEXT PRIMARY KEY, tile_id TEXT NOT NULL,
                epoch INTEGER NOT NULL, sequence INTEGER NOT NULL,
                payload TEXT NOT NULL, result TEXT NOT NULL,
                UNIQUE(tile_id, epoch, sequence))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS tile_snapshots (
                snapshot_id TEXT PRIMARY KEY, tile_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                last_sequence INTEGER NOT NULL, pixels TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        initialize_database()
        yield

    service = FastAPI(title=f"Canvas Shard {identity}", lifespan=lifespan)

    @service.get("/health")
    def health() -> dict:
        try:
            with database_connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {
            "shard_id": identity,
            "status": "healthy",
            "database": "connected",
            "version": SERVICE_VERSION,
        }

    @service.get("/info")
    def info() -> dict:
        return {
            "shard_id": identity,
            "configured_port": service_port,
            "database_location": str(db_path),
            "service": "collaborative-pixel-canvas-shard",
            "version": SERVICE_VERSION,
        }

    @service.post("/records", status_code=201)
    def write_record(record: RecordRequest) -> dict:
        with database_connection() as connection:
            connection.execute(
                """INSERT INTO infrastructure_records(record_key, value)
                   VALUES (?, ?)
                   ON CONFLICT(record_key) DO UPDATE SET value = excluded.value""",
                (record.record_key, record.value),
            )
            row = connection.execute(
                "SELECT record_key, value, created_at FROM infrastructure_records WHERE record_key = ?",
                (record.record_key,),
            ).fetchone()
        return dict(row)

    @service.get("/records")
    def list_records() -> dict:
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT record_key, value, created_at FROM infrastructure_records ORDER BY record_key"
            ).fetchall()
        return {"records": [dict(row) for row in rows]}

    @service.get("/records/{record_key}")
    def get_record(record_key: str) -> dict:
        with database_connection() as connection:
            row = connection.execute(
                "SELECT record_key, value, created_at FROM infrastructure_records WHERE record_key = ?",
                (record_key,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="record not found")
        return dict(row)

    @service.post("/pixels", status_code=201)
    def write_pixel(pixel: PixelWriteRequest) -> dict:
        tile = tile_for_pixel(pixel.x, pixel.y)
        if pixel.tile_id != tile.tile_id:
            raise HTTPException(status_code=422, detail="tile_id does not match pixel coordinates")
        expected_owner = shard_for_tile(tile, shard_ids)
        color = pixel.color.upper()
        operation_id = pixel.operation_id or str(uuid.uuid4())
        payload = json.dumps([pixel.x, pixel.y, tile.tile_id, color, pixel.owner], separators=(",", ":"))
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT payload, result FROM pixel_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise HTTPException(status_code=409, detail="operation ID reused with different payload")
                return {**json.loads(previous["result"]), "duplicate": True}
            if connection.execute(
                "SELECT 1 FROM clear_operations WHERE history_finalized=0 LIMIT 1"
            ).fetchone():
                raise HTTPException(status_code=409, detail="clear in progress")
            if expected_owner == identity:
                connection.execute("INSERT OR IGNORE INTO tile_ownership(tile_id, epoch, next_sequence, active) VALUES (?, 1, 1, 1)", (tile.tile_id,))
            ownership = connection.execute("SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id = ?", (tile.tile_id,)).fetchone()
            if ownership is None or not ownership["active"] or pixel.epoch != ownership["epoch"]:
                raise HTTPException(status_code=409, detail="stale or inactive tile epoch")
            previous_row = connection.execute(
                "SELECT x, y, color, owner FROM pixels WHERE x=? AND y=?", (pixel.x, pixel.y)
            ).fetchone()
            previous_pixel = dict(previous_row) if previous_row is not None else None
            sequence = ownership["next_sequence"]
            connection.execute(
                """INSERT INTO pixels(x, y, tile_id, color, owner)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(x, y) DO UPDATE SET
                     tile_id = excluded.tile_id,
                     color = excluded.color,
                     owner = excluded.owner""",
                (pixel.x, pixel.y, tile.tile_id, color, pixel.owner),
            )
            result = {
            "status": "committed",
            "x": pixel.x,
            "y": pixel.y,
            "tile_id": tile.tile_id,
            "shard_id": identity,
            "color": color,
            "owner": pixel.owner,
            "epoch": pixel.epoch,
            "sequence": sequence,
            "operation_id": operation_id,
                "previous_pixel": previous_pixel,
            }
            connection.execute("UPDATE tile_ownership SET next_sequence = ? WHERE tile_id = ?", (sequence + 1, tile.tile_id))
            connection.execute("INSERT INTO pixel_operations VALUES (?, ?, ?, ?, ?, ?)",
                               (operation_id, tile.tile_id, pixel.epoch, sequence, payload, json.dumps(result)))
        return result

    @service.post("/undo")
    def undo_pixel(undo: UndoRequest) -> dict:
        tile = tile_for_pixel(undo.x, undo.y)
        if undo.tile_id != tile.tile_id:
            raise HTTPException(status_code=422, detail="tile_id does not match pixel coordinates")
        color = undo.expected_color.upper()
        payload = {
            "kind": "undo", "undo_operation_id": undo.operation_id,
            "target_operation_id": undo.action_operation_id, "user_id": undo.owner,
            "x": undo.x, "y": undo.y, "expected_color": color,
            "previous_pixel": undo.previous_pixel,
        }
        encoded = json.dumps(payload, separators=(",", ":"))
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous_operation = connection.execute(
                "SELECT payload, result FROM pixel_operations WHERE operation_id=?", (undo.operation_id,)
            ).fetchone()
            if previous_operation is not None:
                stored_payload = json.loads(previous_operation["payload"])
                if any(stored_payload.get(key) != value for key, value in payload.items()):
                    raise HTTPException(status_code=409, detail="undo operation ID reused with different payload")
                return {**json.loads(previous_operation["result"]), "duplicate": True}
            if connection.execute("SELECT 1 FROM clear_operations WHERE history_finalized=0 LIMIT 1").fetchone():
                raise HTTPException(status_code=409, detail="clear in progress")
            ownership = connection.execute(
                "SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id=?", (tile.tile_id,)
            ).fetchone()
            if ownership is None or not ownership["active"] or ownership["epoch"] != undo.epoch:
                raise HTTPException(status_code=409, detail="stale or inactive tile epoch")
            if undo.previous_pixel is not None:
                previous = undo.previous_pixel
                try:
                    valid_previous = (
                        type(previous["x"]) is int and previous["x"] == undo.x and
                        type(previous["y"]) is int and previous["y"] == undo.y and
                        tile_for_pixel(previous["x"], previous["y"]).tile_id == tile.tile_id and
                        isinstance(previous["owner"], str) and bool(previous["owner"]) and
                        isinstance(previous["color"], str) and len(previous["color"]) == 7 and
                        previous["color"].startswith("#")
                    )
                except (KeyError, TypeError, ValueError):
                    valid_previous = False
                if not valid_previous:
                    raise HTTPException(status_code=422, detail="invalid previous pixel for undo")
            current = connection.execute(
                "SELECT color, owner FROM pixels WHERE x=? AND y=?", (undo.x, undo.y)
            ).fetchone()
            sequence = ownership["next_sequence"]
            if current is None or current["owner"] != undo.owner or current["color"] != color:
                payload["outcome"] = "skipped"
                result = {"status": "skipped", "operation_id": undo.operation_id,
                          "tile_id": tile.tile_id, "epoch": ownership["epoch"],
                          "sequence": sequence, "x": undo.x, "y": undo.y,
                          "previous_pixel": undo.previous_pixel,
                          "detail": "pixel was changed by a newer operation"}
                encoded = json.dumps(payload, separators=(",", ":"))
                connection.execute("UPDATE tile_ownership SET next_sequence=? WHERE tile_id=?",
                                   (sequence + 1, tile.tile_id))
                connection.execute("INSERT INTO pixel_operations VALUES (?, ?, ?, ?, ?, ?)",
                    (undo.operation_id, tile.tile_id, ownership["epoch"], sequence,
                     encoded, json.dumps(result)))
                return result
            if undo.previous_pixel is None:
                connection.execute("DELETE FROM pixels WHERE x=? AND y=?", (undo.x, undo.y))
            else:
                previous = undo.previous_pixel
                connection.execute(
                    "UPDATE pixels SET color=?, owner=? WHERE x=? AND y=?",
                    (previous["color"].upper(), previous["owner"], undo.x, undo.y),
                )
            payload["outcome"] = "undone"
            encoded = json.dumps(payload, separators=(",", ":"))
            result = {"status": "undone", "operation_id": undo.operation_id,
                      "tile_id": tile.tile_id, "epoch": ownership["epoch"],
                      "sequence": sequence, "x": undo.x, "y": undo.y,
                      "previous_pixel": undo.previous_pixel}
            connection.execute("UPDATE tile_ownership SET next_sequence=? WHERE tile_id=?",
                               (sequence + 1, tile.tile_id))
            connection.execute("INSERT INTO pixel_operations VALUES (?, ?, ?, ?, ?, ?)",
                (undo.operation_id, tile.tile_id, ownership["epoch"], sequence,
                 encoded, json.dumps(result)))
        return result

    @service.get("/tiles/{tile_id}/snapshot")
    def tile_snapshot(tile_id: str) -> dict:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ownership = connection.execute("SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id = ?", (tile_id,)).fetchone()
            if ownership is None:
                try:
                    row_part, column_part = tile_id.removeprefix("tile-").split("-")
                    tile = tile_for_pixel(int(column_part) * 25, int(row_part) * 25)
                except (ValueError, IndexError) as exc:
                    raise HTTPException(status_code=422, detail="invalid tile ID") from exc
                if tile.tile_id != tile_id or shard_for_tile(tile, shard_ids) != identity:
                    raise HTTPException(status_code=404, detail="tile not initialized")
                connection.execute("INSERT INTO tile_ownership VALUES (?, 1, 1, 1)", (tile_id,))
                ownership = connection.execute("SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id = ?", (tile_id,)).fetchone()
            rows = connection.execute("SELECT x, y, color, owner FROM pixels WHERE tile_id = ? ORDER BY y, x", (tile_id,)).fetchall()
            return {"tile_id": tile_id, "epoch": ownership["epoch"],
                    "last_sequence": ownership["next_sequence"] - 1,
                    "active": bool(ownership["active"]), "pixels": [dict(row) for row in rows]}

    @service.post("/tiles/{tile_id}/snapshots")
    def create_tile_snapshot(tile_id: str) -> dict:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ownership = connection.execute(
                "SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id=?", (tile_id,)
            ).fetchone()
            if ownership is None or not ownership["active"]:
                raise HTTPException(status_code=409, detail="only an active tile owner can create a snapshot")
            rows = connection.execute(
                "SELECT x,y,color,owner FROM pixels WHERE tile_id=? ORDER BY y,x", (tile_id,)
            ).fetchall()
            snapshot_id = str(uuid.uuid4())
            last_sequence = ownership["next_sequence"] - 1
            connection.execute(
                "INSERT INTO tile_snapshots(snapshot_id,tile_id,epoch,last_sequence,pixels) VALUES(?,?,?,?,?)",
                (snapshot_id, tile_id, ownership["epoch"], last_sequence,
                 json.dumps([dict(row) for row in rows], separators=(",", ":"))),
            )
        return {"snapshot_id": snapshot_id, "tile_id": tile_id,
                "epoch": ownership["epoch"], "last_sequence": last_sequence,
                "pixel_count": len(rows)}

    @service.post("/tiles/{tile_id}/snapshots/{snapshot_id}/restore")
    def restore_tile_snapshot(tile_id: str, snapshot_id: str) -> dict:
        """Restore a saved baseline, then replay its contiguous later tile log."""
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = connection.execute(
                "SELECT epoch,last_sequence,pixels FROM tile_snapshots WHERE snapshot_id=? AND tile_id=?",
                (snapshot_id, tile_id),
            ).fetchone()
            ownership = connection.execute(
                "SELECT epoch,next_sequence,active FROM tile_ownership WHERE tile_id=?", (tile_id,)
            ).fetchone()
            if saved is None:
                raise HTTPException(status_code=404, detail="tile snapshot not found")
            if ownership is None or ownership["epoch"] < saved["epoch"] or not ownership["active"]:
                raise HTTPException(status_code=409, detail="tile is not active at a compatible ownership epoch")
            pixels_at_snapshot = json.loads(saved["pixels"])
            connection.execute("DELETE FROM pixels WHERE tile_id=?", (tile_id,))
            connection.executemany("INSERT INTO pixels VALUES(?,?,?,?,?)", [
                (pixel["x"], pixel["y"], tile_id, pixel["color"], pixel["owner"])
                for pixel in pixels_at_snapshot
            ])
            operations = connection.execute(
                "SELECT sequence,payload FROM pixel_operations WHERE tile_id=? AND sequence>? ORDER BY sequence",
                (tile_id, saved["last_sequence"]),
            ).fetchall()
            sequence = saved["last_sequence"]
            for operation in operations:
                if operation["sequence"] != sequence + 1:
                    raise HTTPException(status_code=409, detail="tile operation log has a gap after snapshot")
                payload = json.loads(operation["payload"])
                if isinstance(payload, list) and len(payload) == 5:
                    x, y, operation_tile, color, owner = payload
                    if tile_for_pixel(x, y).tile_id != tile_id or operation_tile != tile_id:
                        raise HTTPException(status_code=409, detail="invalid draw in tile operation log")
                    connection.execute(
                        "INSERT INTO pixels VALUES(?,?,?,?,?) ON CONFLICT(x,y) DO UPDATE SET "
                        "tile_id=excluded.tile_id,color=excluded.color,owner=excluded.owner",
                        (x, y, tile_id, color, owner),
                    )
                elif isinstance(payload, dict) and payload.get("kind") == "clear":
                    for point in payload["coordinates"]:
                        connection.execute(
                            "DELETE FROM pixels WHERE x=? AND y=? AND tile_id=? AND owner=?",
                            (point["x"], point["y"], tile_id, payload["user_id"]),
                        )
                elif isinstance(payload, dict) and payload.get("kind") == "undo":
                    if payload.get("outcome") == "undone":
                        x, y = payload["x"], payload["y"]
                        current = connection.execute(
                            "SELECT color,owner FROM pixels WHERE x=? AND y=?", (x, y)
                        ).fetchone()
                        if (current is not None and current["owner"] == payload["user_id"]
                                and current["color"] == payload["expected_color"]):
                            previous = payload["previous_pixel"]
                            if previous is None:
                                connection.execute("DELETE FROM pixels WHERE x=? AND y=?", (x, y))
                            else:
                                connection.execute(
                                    "UPDATE pixels SET color=?,owner=? WHERE x=? AND y=?",
                                    (previous["color"].upper(), previous["owner"], x, y),
                                )
                else:
                    raise HTTPException(status_code=409, detail="unknown operation in tile history")
                sequence = operation["sequence"]
            if sequence != ownership["next_sequence"] - 1:
                raise HTTPException(status_code=409, detail="tile operation log is incomplete after snapshot")
            restored_count = connection.execute(
                "SELECT COUNT(*) FROM pixels WHERE tile_id=?", (tile_id,)
            ).fetchone()[0]
        return {"snapshot_id": snapshot_id, "tile_id": tile_id,
                "epoch": ownership["epoch"], "last_sequence": sequence,
                "pixel_count": restored_count, "status": "restored"}

    @service.get("/tiles/positions")
    def tile_positions() -> dict:
        with database_connection() as connection:
            rows = connection.execute(
                # Include a fenced source row until the coordinator records the
                # new owner. This keeps pixels and cursor paired during cutover.
                "SELECT tile_id, epoch, next_sequence FROM tile_ownership ORDER BY tile_id"
            ).fetchall()
        return {"shard_id": identity, "positions": [
            {"tile_id": row["tile_id"], "epoch": row["epoch"],
             "last_sequence": row["next_sequence"] - 1}
            for row in rows
        ]}

    @service.post("/tiles/{tile_id}/fence")
    def fence_tile(tile_id: str, epoch: int = Query(ge=1)) -> dict:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if epoch == 1:
                try:
                    row_part, column_part = tile_id.removeprefix("tile-").split("-")
                    tile = tile_for_pixel(int(column_part) * 25, int(row_part) * 25)
                except (ValueError, IndexError) as exc:
                    raise HTTPException(status_code=422, detail="invalid tile ID") from exc
                if tile.tile_id != tile_id or shard_for_tile(tile, shard_ids) != identity:
                    raise HTTPException(status_code=409, detail="not initial owner")
                connection.execute("INSERT OR IGNORE INTO tile_ownership VALUES (?, 1, 1, 1)", (tile_id,))
            row = connection.execute("SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id = ?", (tile_id,)).fetchone()
            if row is None or row["epoch"] != epoch:
                raise HTTPException(status_code=409, detail="tile epoch mismatch")
            connection.execute("UPDATE tile_ownership SET active = 0 WHERE tile_id = ?", (tile_id,))
            return {"tile_id": tile_id, "epoch": epoch, "last_sequence": row["next_sequence"] - 1}

    @service.post("/tiles/{tile_id}/install")
    def install_tile(tile_id: str, install: TileInstallRequest) -> dict:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT epoch, active FROM tile_ownership WHERE tile_id = ?", (tile_id,)).fetchone()
            if row is not None and (row["epoch"] > install.epoch or row["active"]):
                raise HTTPException(status_code=409, detail="destination tile already active or has different epoch")
            for pixel in install.pixels:
                if tile_for_pixel(pixel["x"], pixel["y"]).tile_id != tile_id:
                    raise HTTPException(status_code=422, detail="snapshot pixel outside tile")
            connection.execute("DELETE FROM pixels WHERE tile_id = ?", (tile_id,))
            connection.executemany("INSERT INTO pixels VALUES (?, ?, ?, ?, ?)",
                                   [(p["x"], p["y"], tile_id, p["color"], p["owner"]) for p in install.pixels])
            for operation in install.operations:
                if operation["sequence"] > install.last_sequence:
                    raise HTTPException(status_code=422, detail="operation beyond snapshot boundary")
                connection.execute("INSERT OR IGNORE INTO pixel_operations VALUES (?, ?, ?, ?, ?, ?)",
                                   (operation["operation_id"], tile_id, operation["epoch"],
                                    operation["sequence"], json.dumps(operation["payload"], separators=(",", ":")),
                                    json.dumps(operation["result"])))
            connection.execute("INSERT INTO tile_ownership VALUES (?, ?, ?, 0) ON CONFLICT(tile_id) DO UPDATE SET epoch = excluded.epoch, next_sequence = excluded.next_sequence, active = 0",
                               (tile_id, install.epoch, install.last_sequence + 1))
        return {"tile_id": tile_id, "epoch": install.epoch, "status": "prepared"}

    @service.post("/tiles/{tile_id}/activate")
    def activate_tile(tile_id: str, epoch: int = Query(ge=2)) -> dict:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT epoch FROM tile_ownership WHERE tile_id = ?", (tile_id,)).fetchone()
            if row is None or row["epoch"] != epoch:
                raise HTTPException(status_code=409, detail="destination tile not prepared")
            connection.execute("UPDATE tile_ownership SET active = 1 WHERE tile_id = ?", (tile_id,))
        return {"tile_id": tile_id, "epoch": epoch, "status": "active"}

    @service.post("/tiles/{tile_id}/import-operations")
    def import_operations(tile_id: str, batch: TileOperationsRequest) -> dict:
        """Copy and replay draw or Clear history after a prepared tile snapshot."""
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ownership = connection.execute("SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id = ?", (tile_id,)).fetchone()
            if ownership is None or ownership["epoch"] != batch.epoch or ownership["active"]:
                raise HTTPException(status_code=409, detail="destination tile is not prepared")
            next_sequence = ownership["next_sequence"]
            for operation in batch.operations:
                try:
                    operation_id = operation["operation_id"]
                    sequence = operation["sequence"]
                    source_epoch = operation["epoch"]
                    payload = operation["payload"]
                    result = operation["result"]
                    common_valid = (isinstance(operation_id, str) and operation_id and
                                    type(sequence) is int and sequence >= 1 and
                                    type(source_epoch) is int and 1 <= source_epoch < batch.epoch and
                                    isinstance(result, dict) and result.get("sequence") == sequence)
                    if isinstance(payload, list) and len(payload) == 5:
                        x, y, operation_tile, color, owner = payload
                        kind = "draw"
                        valid = (common_valid and tile_for_pixel(x, y).tile_id == tile_id == operation_tile
                                 and isinstance(color, str) and isinstance(owner, str))
                    elif isinstance(payload, dict) and payload.get("kind") == "clear":
                        kind = "clear"
                        clear_id = payload.get("clear_operation_id")
                        owner = payload.get("user_id")
                        points = payload.get("coordinates")
                        valid = (common_valid and isinstance(clear_id, str) and clear_id
                                 and operation_id == f"clear:{clear_id}:{tile_id}"
                                 and isinstance(owner, str) and owner
                                 and isinstance(points, list) and len(points) > 0
                                 and all(isinstance(point, dict)
                                         and type(point.get("x")) is int and type(point.get("y")) is int
                                         and tile_for_pixel(point["x"], point["y"]).tile_id == tile_id
                                         for point in points)
                                 and result.get("kind") == "clear"
                                 and result.get("operation_id") == clear_id
                                 and result.get("tile_id") == tile_id)
                    elif isinstance(payload, dict) and payload.get("kind") == "undo":
                        kind = "undo"
                        x, y = payload.get("x"), payload.get("y")
                        owner = payload.get("user_id")
                        previous_pixel = payload.get("previous_pixel")
                        outcome = payload.get("outcome")
                        valid_previous = (previous_pixel is None or
                            isinstance(previous_pixel, dict) and
                            previous_pixel.get("x") == x and previous_pixel.get("y") == y and
                            isinstance(previous_pixel.get("owner"), str) and
                            isinstance(previous_pixel.get("color"), str))
                        valid = (common_valid and operation_id == payload.get("undo_operation_id")
                                 and isinstance(payload.get("target_operation_id"), str)
                                 and isinstance(owner, str) and owner
                                 and type(x) is int and type(y) is int
                                 and tile_for_pixel(x, y).tile_id == tile_id
                                 and isinstance(payload.get("expected_color"), str)
                                 and valid_previous and outcome in {"undone", "skipped"}
                                 and result.get("status") == outcome
                                 and result.get("operation_id") == operation_id
                                 and result.get("tile_id") == tile_id)
                    else:
                        valid = False
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid or sequence > next_sequence:
                    raise HTTPException(status_code=409, detail="invalid or gapped operation batch")
                encoded = json.dumps(payload, separators=(",", ":"))
                previous = connection.execute(
                    "SELECT tile_id, epoch, sequence, payload FROM pixel_operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if previous is not None:
                    if (previous["tile_id"], previous["epoch"], previous["sequence"], previous["payload"]) != (tile_id, source_epoch, sequence, encoded):
                        raise HTTPException(status_code=409, detail="operation ID conflict")
                else:
                    try:
                        connection.execute("INSERT INTO pixel_operations VALUES (?, ?, ?, ?, ?, ?)",
                                           (operation_id, tile_id, source_epoch, sequence, encoded, json.dumps(result)))
                    except sqlite3.IntegrityError as exc:
                        raise HTTPException(status_code=409, detail="operation sequence conflict") from exc
                if sequence == next_sequence:
                    if kind == "draw":
                        connection.execute(
                            "INSERT INTO pixels(x, y, tile_id, color, owner) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(x, y) DO UPDATE SET tile_id = excluded.tile_id, color = excluded.color, owner = excluded.owner",
                            (x, y, tile_id, color, owner),
                        )
                    elif kind == "clear":
                        for point in points:
                            connection.execute(
                                "DELETE FROM pixels WHERE x=? AND y=? AND tile_id=? AND owner=?",
                                (point["x"], point["y"], tile_id, owner),
                            )
                    elif payload["outcome"] == "undone":
                        current = connection.execute(
                            "SELECT color, owner FROM pixels WHERE x=? AND y=?", (x, y)
                        ).fetchone()
                        if (current is not None and current["owner"] == owner
                                and current["color"] == payload["expected_color"]):
                            if previous_pixel is None:
                                connection.execute("DELETE FROM pixels WHERE x=? AND y=?", (x, y))
                            else:
                                connection.execute(
                                    "UPDATE pixels SET color=?,owner=? WHERE x=? AND y=?",
                                    (previous_pixel["color"].upper(), previous_pixel["owner"], x, y),
                                )
                    next_sequence += 1
            connection.execute("UPDATE tile_ownership SET next_sequence = ? WHERE tile_id = ?",
                               (next_sequence, tile_id))
        return {"tile_id": tile_id, "last_sequence": next_sequence - 1}

    @service.get("/tiles/{tile_id}/operations")
    def tile_operations(tile_id: str, after: int = Query(default=0, ge=0),
                        limit: int = Query(default=100, ge=1, le=1000)) -> dict:
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT operation_id, epoch, sequence, payload, result FROM pixel_operations "
                "WHERE tile_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (tile_id, after, limit),
            ).fetchall()
        return {"tile_id": tile_id, "operations": [
            {"operation_id": row["operation_id"], "epoch": row["epoch"],
             "sequence": row["sequence"], "payload": json.loads(row["payload"]),
             "result": json.loads(row["result"])} for row in rows]}

    @service.get("/pixels")
    def list_pixels() -> dict:
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT x, y, tile_id, color, owner FROM pixels ORDER BY y, x"
            ).fetchall()
        return {"pixels": [dict(row) for row in rows]}

    @service.delete("/pixels")
    def clear_owned_pixels(user_id: str = Query(min_length=1, max_length=100)) -> dict:
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT x, y FROM pixels WHERE owner = ? ORDER BY y, x", (user_id,)
            ).fetchall()
            connection.execute("DELETE FROM pixels WHERE owner = ?", (user_id,))
        return {"removed": [{"x": row["x"], "y": row["y"]} for row in rows]}

    @service.post("/clear/{operation_id}/prepare")
    def prepare_clear(operation_id: str, user_id: str = Query(min_length=1, max_length=100)) -> dict:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT user_id, coordinates FROM clear_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if previous:
                if previous["user_id"] != user_id:
                    raise HTTPException(status_code=409, detail="clear operation ID conflict")
                return {"status": "prepared", "operation_id": operation_id,
                        "coordinates": json.loads(previous["coordinates"])}
            rows = connection.execute(
                """SELECT p.x, p.y, p.tile_id FROM pixels p
                   JOIN tile_ownership o ON o.tile_id = p.tile_id
                   WHERE p.owner = ? AND o.active = 1 ORDER BY p.y, p.x""", (user_id,)
            ).fetchall()
            coords = [{"x": row["x"], "y": row["y"]} for row in rows]
            connection.execute("INSERT INTO clear_operations(operation_id,user_id,phase,coordinates) VALUES(?,?,?,?)",
                               (operation_id, user_id, "prepared", json.dumps(coords)))
        return {"status": "prepared", "operation_id": operation_id, "coordinates": coords}

    @service.post("/clear/{operation_id}/commit")
    def commit_clear(operation_id: str) -> dict:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute("SELECT * FROM clear_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if operation is None:
                raise HTTPException(status_code=404, detail="clear was not prepared")
            coords = json.loads(operation["coordinates"])
            if operation["phase"] not in {"committed", "finalized"}:
                for point in coords:
                    tile = tile_for_pixel(point["x"], point["y"])
                    connection.execute("DELETE FROM pixels WHERE x = ? AND y = ? AND owner = ? AND tile_id = ?",
                                       (point["x"], point["y"], operation["user_id"], tile.tile_id))
                connection.execute("UPDATE clear_operations SET phase='committed' WHERE operation_id=?", (operation_id,))
        return {"status": "committed", "operation_id": operation_id, "coordinates": coords}

    @service.post("/clear/{operation_id}/finalize")
    def finalize_clear(operation_id: str) -> dict:
        """Append per-tile ordered history after the coordinator's all-shard commit phase."""
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute("SELECT * FROM clear_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if operation is None or operation["phase"] not in {"committed", "finalized"}:
                raise HTTPException(status_code=409, detail="clear participants have not all committed")
            # Use the durable prepared coordinates, not the now-deleted pixel rows.
            coords = json.loads(operation["coordinates"])
            grouped: dict[str, list[dict]] = {}
            positions: list[dict] = []
            for point in coords:
                tile = tile_for_pixel(point["x"], point["y"])
                grouped.setdefault(tile.tile_id, []).append(point)
            for tile_id, tile_coords in sorted(grouped.items()):
                event_id = f"clear:{operation_id}:{tile_id}"
                payload = {
                    "kind": "clear", "clear_operation_id": operation_id,
                    "user_id": operation["user_id"], "coordinates": tile_coords,
                }
                existing = connection.execute(
                    "SELECT tile_id, epoch, sequence, payload FROM pixel_operations WHERE operation_id = ?", (event_id,)
                ).fetchone()
                if existing is not None:
                    if (existing["tile_id"] != tile_id
                            or existing["payload"] != json.dumps(payload, separators=(",", ":"))):
                        raise HTTPException(status_code=409, detail="clear history ID conflict")
                    positions.append({"tile_id": tile_id, "epoch": existing["epoch"],
                                      "sequence": existing["sequence"]})
                    continue
                ownership = connection.execute(
                    "SELECT epoch, next_sequence, active FROM tile_ownership WHERE tile_id = ?", (tile_id,)
                ).fetchone()
                if ownership is None or not ownership["active"]:
                    raise HTTPException(status_code=409, detail="clear tile is no longer active")
                sequence = ownership["next_sequence"]
                result = {"status": "participant_finalized", "kind": "clear", "operation_id": operation_id,
                          "tile_id": tile_id, "epoch": ownership["epoch"], "sequence": sequence}
                connection.execute("INSERT INTO pixel_operations VALUES (?, ?, ?, ?, ?, ?)",
                    (event_id, tile_id, ownership["epoch"], sequence,
                     json.dumps(payload, separators=(",", ":")), json.dumps(result)))
                connection.execute("UPDATE tile_ownership SET next_sequence = ? WHERE tile_id = ?",
                                   (sequence + 1, tile_id))
                positions.append({"tile_id": tile_id, "epoch": ownership["epoch"], "sequence": sequence})
            connection.execute("UPDATE clear_operations SET phase='finalized', history_finalized=1 WHERE operation_id=?", (operation_id,))
        return {"status": "finalized", "operation_id": operation_id, "positions": positions}

    return service


app = create_app()
