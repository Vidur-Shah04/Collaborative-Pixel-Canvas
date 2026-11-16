import asyncio
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from frontend import main as coordinator
from shard.main import create_app
from shared.tiles import tile_for_pixel


def test_draw_during_snapshot_copy_is_caught_up_before_cutover(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "OWNERSHIP_DB_PATH", tmp_path / "ownership.sqlite3")
    source = TestClient(create_app("shard-1", 8001, tmp_path / "one.sqlite3"))
    destination = TestClient(create_app("shard-2", 8002, tmp_path / "two.sqlite3"))
    tile = tile_for_pixel(0, 0)
    token, _ = coordinator.session_for(None)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/draw", "raw_path": b"/draw",
        "query_string": b"", "headers": [(b"cookie", f"{coordinator.SESSION_COOKIE}={token}".encode())],
        "server": ("testserver", 80), "client": ("testclient", 123),
    }
    copied_while_drawing = False

    async def request(shard_id, method, path, *, params=None, body=None):
        nonlocal copied_while_drawing
        client = {"shard-1": source, "shard-2": destination}[shard_id]
        response = client.request(method, path, params=params, json=body)
        if response.status_code >= 400:
            raise coordinator.ShardRequestError(shard_id, response.text, response.status_code)
        if shard_id == "shard-2" and path.endswith("/install") and not copied_while_drawing:
            copied_while_drawing = True
            result = await coordinator.draw(
                coordinator.DrawRequest(x=0, y=0, color="#0000ff", operation_id="during-copy"),
                StarletteRequest(scope),
            )
            assert result.status_code == 200
        return response.json()

    monkeypatch.setattr(coordinator, "request_shard", request)
    with source, destination:
        first = {"x": 0, "y": 0, "tile_id": tile.tile_id, "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "before-copy"}
        assert source.post("/pixels", json=first).status_code == 201
        result = asyncio.run(coordinator.transfer_tile(tile, "shard-2"))
        assert copied_while_drawing
        assert result["epoch"] == 2
        snapshot = destination.get(f"/tiles/{tile.tile_id}/snapshot").json()
        assert snapshot["last_sequence"] == 2
        assert snapshot["pixels"][0]["color"] == "#0000FF"
        assert source.post("/pixels", json={**first, "operation_id": "stale"}).status_code == 409


def test_fence_recovery_and_return_transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "OWNERSHIP_DB_PATH", tmp_path / "ownership.sqlite3")
    source = TestClient(create_app("shard-1", 8001, tmp_path / "one.sqlite3"))
    destination = TestClient(create_app("shard-2", 8002, tmp_path / "two.sqlite3"))
    tile = tile_for_pixel(0, 0)
    payload = {"x": 0, "y": 0, "tile_id": tile.tile_id, "color": "#ff0000",
               "owner": "alice", "epoch": 1, "operation_id": "first"}

    with source, destination:
        assert source.post("/pixels", json=payload).status_code == 201
        failed_once = False
        activation_failed_once = False

        async def request(shard_id, method, path, *, params=None, body=None):
            nonlocal failed_once, activation_failed_once
            if shard_id == "shard-2" and path.endswith("/install") and not failed_once:
                failed_once = True
                raise coordinator.ShardRequestError(shard_id, "simulated destination failure")
            client = {"shard-1": source, "shard-2": destination}[shard_id]
            response = client.request(method, path, params=params, json=body)
            if response.status_code >= 400:
                raise coordinator.ShardRequestError(shard_id, response.text, response.status_code)
            if shard_id == "shard-2" and path.endswith("/activate") and not activation_failed_once:
                activation_failed_once = True
                raise coordinator.ShardRequestError(shard_id, "simulated lost activation response")
            return response.json()

        monkeypatch.setattr(coordinator, "request_shard", request)
        with pytest.raises(coordinator.ShardRequestError):
            asyncio.run(coordinator.transfer_tile(tile, "shard-2"))
        assert coordinator.pending_transfer(tile.tile_id) is not None
        # The source keeps accepting writes while the destination is down.
        assert source.post("/pixels", json={**payload, "operation_id": "late"}).status_code == 201

        with pytest.raises(coordinator.ShardRequestError):
            asyncio.run(coordinator.transfer_tile(tile, "shard-2"))
        assert coordinator.pending_transfer(tile.tile_id) is not None
        assert asyncio.run(coordinator.recover_pending_transfers_once()) == 1
        assert coordinator.pending_transfer(tile.tile_id) is None
        assert coordinator.ownership_for_tile(tile) == ("shard-2", 2)
        assert asyncio.run(coordinator.transfer_tile(tile, "shard-2"))["epoch"] == 2
        assert destination.get(f"/tiles/{tile.tile_id}/snapshot").json()["pixels"][0]["color"] == "#FF0000"
        assert destination.get(f"/tiles/{tile.tile_id}/operations").json()["operations"][0]["operation_id"] == "first"
        assert destination.post("/pixels", json={**payload, "epoch": 1, "operation_id": "stale"}).status_code == 409
        assert destination.post("/pixels", json={**payload, "epoch": 2, "operation_id": "second"}).json()["sequence"] == 3

        back = asyncio.run(coordinator.transfer_tile(tile, "shard-1"))
        assert back["epoch"] == 3
        assert source.get(f"/tiles/{tile.tile_id}/snapshot").json()["last_sequence"] == 3
        assert [op["operation_id"] for op in source.get(f"/tiles/{tile.tile_id}/operations").json()["operations"]] == ["first", "late", "second"]


def test_coordinator_starts_transfer_recovery_task(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "OWNERSHIP_DB_PATH", tmp_path / "ownership.sqlite3")
    called = threading.Event()

    async def recover_once():
        called.set()
        return 0

    monkeypatch.setattr(coordinator, "recover_pending_transfers_once", recover_once)
    with TestClient(coordinator.app):
        assert called.wait(timeout=2)


def test_pending_transfer_stays_fenced_until_destination_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "OWNERSHIP_DB_PATH", tmp_path / "ownership.sqlite3")
    source = TestClient(create_app("shard-1", 8001, tmp_path / "one.sqlite3"))
    destination = TestClient(create_app("shard-2", 8002, tmp_path / "two.sqlite3"))
    tile = tile_for_pixel(0, 0)
    unavailable = True

    async def request(shard_id, method, path, *, params=None, body=None):
        if shard_id == "shard-2" and unavailable:
            raise coordinator.ShardRequestError(shard_id, "destination down")
        client = {"shard-1": source, "shard-2": destination}[shard_id]
        response = client.request(method, path, params=params, json=body)
        if response.status_code >= 400:
            raise coordinator.ShardRequestError(shard_id, response.text, response.status_code)
        return response.json()

    monkeypatch.setattr(coordinator, "request_shard", request)
    with source, destination:
        with pytest.raises(coordinator.ShardRequestError):
            asyncio.run(coordinator.transfer_tile(tile, "shard-2"))
        assert asyncio.run(coordinator.recover_pending_transfers_once()) == 0
        assert coordinator.pending_transfer(tile.tile_id) is not None
        assert source.post("/pixels", json={"x": 0, "y": 0, "tile_id": tile.tile_id,
                                            "color": "#ff0000", "owner": "alice", "epoch": 1,
                                            "operation_id": "late"}).status_code == 201
        unavailable = False
        assert asyncio.run(coordinator.recover_pending_transfers_once()) == 1
        assert coordinator.ownership_for_tile(tile) == ("shard-2", 2)
        assert coordinator.pending_transfer(tile.tile_id) is None
        assert destination.get(f"/tiles/{tile.tile_id}/snapshot").json()["last_sequence"] == 1


@pytest.mark.parametrize("failure_point", ["snapshot", "operations", "import-operations", "fence", "activate"])
def test_lost_response_at_each_transfer_phase_recovers(tmp_path, monkeypatch, failure_point):
    monkeypatch.setattr(coordinator, "OWNERSHIP_DB_PATH", tmp_path / "ownership.sqlite3")
    source = TestClient(create_app("shard-1", 8001, tmp_path / "one.sqlite3"))
    destination = TestClient(create_app("shard-2", 8002, tmp_path / "two.sqlite3"))
    tile = tile_for_pixel(0, 0)
    failed = False

    async def request(shard_id, method, path, *, params=None, body=None):
        nonlocal failed
        client = {"shard-1": source, "shard-2": destination}[shard_id]
        response = client.request(method, path, params=params, json=body)
        if response.status_code >= 400:
            raise coordinator.ShardRequestError(shard_id, response.text, response.status_code)
        if path.endswith("/" + failure_point) and not failed:
            failed = True
            raise coordinator.ShardRequestError(shard_id, "simulated lost response")
        return response.json()

    monkeypatch.setattr(coordinator, "request_shard", request)
    with source, destination:
        write = {"x": 0, "y": 0, "tile_id": tile.tile_id, "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "original"}
        assert source.post("/pixels", json=write).status_code == 201
        with pytest.raises(coordinator.ShardRequestError):
            asyncio.run(coordinator.transfer_tile(tile, "shard-2"))
        assert failed
        assert coordinator.pending_transfer(tile.tile_id) is not None
        assert asyncio.run(coordinator.recover_pending_transfers_once()) == 1
        assert coordinator.ownership_for_tile(tile) == ("shard-2", 2)
        assert destination.get(f"/tiles/{tile.tile_id}/snapshot").json()["pixels"][0]["color"] == "#FF0000"
        assert destination.post("/pixels", json={**write, "epoch": 2}).json()["duplicate"] is True
