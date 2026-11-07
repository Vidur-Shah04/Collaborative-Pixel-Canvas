from fastapi.testclient import TestClient

from shard.main import create_app


def test_retry_after_later_write_does_not_rewind_state(tmp_path):
    app = create_app("shard-1", 8001, tmp_path / "shard.sqlite3")
    with TestClient(app) as client:
        first = {"x": 0, "y": 0, "tile_id": "tile-00-00", "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "op-1"}
        second = {**first, "color": "#0000ff", "operation_id": "op-2"}
        assert client.post("/pixels", json=first).json()["sequence"] == 1
        assert client.post("/pixels", json=second).json()["sequence"] == 2
        retry = client.post("/pixels", json=first).json()
        assert retry["duplicate"] is True
        assert retry["sequence"] == 1
        assert client.get("/pixels").json()["pixels"][0]["color"] == "#0000FF"
        snapshot = client.get("/tiles/tile-00-00/snapshot").json()
        assert snapshot["last_sequence"] == 2
        assert snapshot["pixels"][0]["color"] == "#0000FF"


def test_stale_epoch_and_reused_id_are_rejected(tmp_path):
    app = create_app("shard-1", 8001, tmp_path / "shard.sqlite3")
    with TestClient(app) as client:
        write = {"x": 0, "y": 0, "tile_id": "tile-00-00", "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "op-1"}
        assert client.post("/pixels", json={**write, "epoch": 2}).status_code == 409
        assert client.post("/pixels", json=write).status_code == 201
        assert client.post("/pixels", json={**write, "color": "#0000ff"}).status_code == 409
        assert client.get("/tiles/tile-00-00/snapshot").json()["last_sequence"] == 1
