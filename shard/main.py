"""Small, independently deployable shard service for infrastructure checks."""

from contextlib import asynccontextmanager, contextmanager
import os
from pathlib import Path
import sqlite3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.2.0")


class RecordRequest(BaseModel):
    record_key: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=4000)


def create_app(
    shard_id: str | None = None,
    port: int | None = None,
    database_path: str | Path | None = None,
) -> FastAPI:
    identity = shard_id or os.getenv("SHARD_ID", "shard-local")
    service_port = port or int(os.getenv("PORT", "8001"))
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

    return service


app = create_app()
