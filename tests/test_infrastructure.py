from pathlib import Path
import uuid

from fastapi.testclient import TestClient

from frontend import main as coordinator
from shard.main import create_app


def test_shard_health_reports_identity_database_and_version():
    database = Path(__file__).with_name(f".shard-test-{uuid.uuid4().hex}.sqlite3")
    try:
        app = create_app("shard-test", 9101, database)
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {
                "shard_id": "shard-test",
                "status": "healthy",
                "database": "connected",
                "version": "0.2.0",
            }
            assert client.get("/info").json()["configured_port"] == 9101
    finally:
        database.unlink(missing_ok=True)


def test_shard_records_are_transactional_and_databases_are_isolated():
    suffix = uuid.uuid4().hex
    db_one = Path(__file__).with_name(f".shard-one-{suffix}.sqlite3")
    db_two = Path(__file__).with_name(f".shard-two-{suffix}.sqlite3")
    try:
        shard_one = create_app("shard-1", 8001, db_one)
        shard_two = create_app("shard-2", 8002, db_two)

        with TestClient(shard_one) as first, TestClient(shard_two) as second:
            write_response = first.post(
                "/records", json={"record_key": "only-one", "value": "persist-me"}
            )
            assert write_response.status_code == 201
            assert first.get("/records/only-one").json()["value"] == "persist-me"
            assert second.get("/records/only-one").status_code == 404
            assert first.get("/info").json()["database_location"] != second.get("/info").json()["database_location"]
    finally:
        for database in (db_one, db_two):
            database.unlink(missing_ok=True)


def test_coordinator_reports_live_shard_checks_and_unavailable_shards(monkeypatch):
    monkeypatch.setenv(
        "SHARD_URLS",
        "shard-1=http://shard-1:8001,shard-2=http://shard-2:8002,shard-3=http://shard-3:8003",
    )

    async def fake_inspect(shard_id: str, url: str) -> dict:
        if shard_id == "shard-2":
            return {"shard_id": shard_id, "url": url, "status": "unavailable", "detail": "timeout"}
        return {"shard_id": shard_id, "url": url, "status": "healthy", "database": "connected"}

    monkeypatch.setattr(coordinator, "inspect_shard", fake_inspect)
    with TestClient(coordinator.app) as client:
        response = client.get("/admin/shards")

    assert response.status_code == 200
    assert [shard["shard_id"] for shard in response.json()["shards"]] == ["shard-1", "shard-2", "shard-3"]
    assert [shard["status"] for shard in response.json()["shards"]] == ["healthy", "unavailable", "healthy"]
    assert response.json()["shards"][0]["url"] == "http://shard-1:8001"


def test_coordinator_shard_registry_can_include_a_configured_fourth_shard(monkeypatch):
    monkeypatch.setenv(
        "SHARD_URLS",
        "shard-1=http://shard-1:8001,shard-2=http://shard-2:8002,"
        "shard-3=http://shard-3:8003,shard-4=http://shard-4:8004",
    )

    assert coordinator.configured_shards()[-1] == ("shard-4", "http://shard-4:8004")
