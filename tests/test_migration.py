import asyncio

import pytest
from fastapi.testclient import TestClient

from frontend import main as coordinator
from shard.main import create_app
from shared.tiles import tile_for_pixel


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
        assert source.post("/pixels", json={**payload, "operation_id": "late"}).status_code == 409

        with pytest.raises(coordinator.ShardRequestError):
            asyncio.run(coordinator.transfer_tile(tile, "shard-2"))
        assert coordinator.pending_transfer(tile.tile_id) is not None
        result = asyncio.run(coordinator.transfer_tile(tile, "shard-2"))
        assert result["epoch"] == 2
        assert coordinator.pending_transfer(tile.tile_id) is None
        assert asyncio.run(coordinator.transfer_tile(tile, "shard-2"))["epoch"] == 2
        assert destination.get(f"/tiles/{tile.tile_id}/snapshot").json()["pixels"][0]["color"] == "#FF0000"
        assert destination.get(f"/tiles/{tile.tile_id}/operations").json()["operations"][0]["operation_id"] == "first"
        assert destination.post("/pixels", json={**payload, "epoch": 1, "operation_id": "stale"}).status_code == 409
        assert destination.post("/pixels", json={**payload, "epoch": 2, "operation_id": "second"}).json()["sequence"] == 2

        back = asyncio.run(coordinator.transfer_tile(tile, "shard-1"))
        assert back["epoch"] == 3
        assert source.get(f"/tiles/{tile.tile_id}/snapshot").json()["last_sequence"] == 2
        assert [op["operation_id"] for op in source.get(f"/tiles/{tile.tile_id}/operations").json()["operations"]] == ["first", "second"]
