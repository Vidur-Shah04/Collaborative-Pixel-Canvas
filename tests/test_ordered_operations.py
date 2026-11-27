from fastapi.testclient import TestClient

from frontend.main import canonical_canvas_hash
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
        operations = client.get("/tiles/tile-00-00/operations", params={"after": 1}).json()["operations"]
        assert [(item["operation_id"], item["sequence"]) for item in operations] == [("op-2", 2)]


def test_stale_epoch_and_reused_id_are_rejected(tmp_path):
    app = create_app("shard-1", 8001, tmp_path / "shard.sqlite3")
    with TestClient(app) as client:
        write = {"x": 0, "y": 0, "tile_id": "tile-00-00", "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "op-1"}
        assert client.post("/pixels", json={**write, "epoch": 2}).status_code == 409
        assert client.post("/pixels", json=write).status_code == 201
        assert client.post("/pixels", json={**write, "color": "#0000ff"}).status_code == 409
        assert client.get("/tiles/tile-00-00/snapshot").json()["last_sequence"] == 1


def test_fenced_source_keeps_its_browser_cursor_until_owner_cutover(tmp_path):
    app = create_app("shard-1", 8001, tmp_path / "shard.sqlite3")
    with TestClient(app) as client:
        tile = "tile-00-00"
        write = {"x": 0, "y": 0, "tile_id": tile, "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "draw-before-fence"}
        assert client.post("/pixels", json=write).status_code == 201
        assert client.post(f"/tiles/{tile}/fence", params={"epoch": 1}).status_code == 200
        assert client.get("/tiles/positions").json() == {
            "shard_id": "shard-1",
            "positions": [{"tile_id": tile, "epoch": 1, "last_sequence": 1}],
        }


def test_import_rejects_gap_without_changing_prepared_snapshot(tmp_path):
    app = create_app("shard-2", 8002, tmp_path / "shard.sqlite3")
    with TestClient(app) as client:
        tile = "tile-00-00"
        assert client.post(f"/tiles/{tile}/install", json={
            "epoch": 2, "last_sequence": 1,
            "pixels": [{"x": 0, "y": 0, "color": "#FF0000", "owner": "alice"}],
            "operations": [],
        }).status_code == 200
        operation = {"operation_id": "gap", "epoch": 1, "sequence": 3,
                     "payload": [0, 0, tile, "#0000FF", "alice"],
                     "result": {"sequence": 3}}
        assert client.post(f"/tiles/{tile}/import-operations", json={
            "epoch": 2, "operations": [operation],
        }).status_code == 409
        snapshot = client.get(f"/tiles/{tile}/snapshot").json()
        assert snapshot["last_sequence"] == 1
        assert snapshot["pixels"][0]["color"] == "#FF0000"


def test_clear_prepare_and_commit_are_idempotent_and_keep_new_owner(tmp_path):
    database = tmp_path / "shard.sqlite3"
    app = create_app("shard-1", 8001, database)
    with TestClient(app) as client:
        alice = {"x": 0, "y": 0, "tile_id": "tile-00-00", "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "alice-write"}
        assert client.post("/pixels", json=alice).status_code == 201
        baseline = client.get("/tiles/tile-00-00/snapshot").json()
        prepared = client.post("/clear/clear-1/prepare", params={"user_id": "alice"}).json()
        assert prepared["operation_id"] == "clear-1"
        assert prepared["coordinates"] == [{"x": 0, "y": 0}]
        assert client.post("/clear/clear-1/prepare", params={"user_id": "alice"}).json() == prepared
        bob = {"x": 0, "y": 0, "tile_id": "tile-00-00", "color": "#0000ff",
               "owner": "bob", "epoch": 1, "operation_id": "bob-write"}
        assert client.post("/pixels", json=bob).status_code == 409
        assert client.post("/clear/clear-1/commit").json()["status"] == "committed"
    # A fresh app instance represents a shard process restart using the same volume.
    restarted = create_app("shard-1", 8001, database)
    with TestClient(restarted) as client:
        assert client.post("/clear/clear-1/commit").json()["status"] == "committed"
        assert client.post("/clear/clear-1/finalize").json()["status"] == "finalized"
        assert client.post("/clear/clear-1/finalize").json()["status"] == "finalized"
        assert client.get("/tiles/tile-00-00/snapshot").json()["last_sequence"] == 2
        later = client.get("/tiles/tile-00-00/operations", params={"after": 1}).json()["operations"]
        assert len(later) == 1 and later[0]["payload"]["kind"] == "clear"
        assert later[0]["payload"]["clear_operation_id"] == "clear-1"
        assert client.post("/pixels", json=bob).status_code == 201
        destination_path = tmp_path / "destination.sqlite3"
        destination = create_app("shard-3", 8003, destination_path)
        with TestClient(destination) as restored:
            assert restored.post("/tiles/tile-00-00/install", json={
                "epoch": 2, "last_sequence": baseline["last_sequence"],
                "pixels": baseline["pixels"], "operations": [],
            }).status_code == 200
            replay_batch = later + client.get(
                    "/tiles/tile-00-00/operations", params={"after": 2}
                ).json()["operations"]
            assert restored.post("/tiles/tile-00-00/import-operations", json={
                "epoch": 2, "operations": replay_batch,
            }).status_code == 200
            # Repeating the same replay batch must not apply it or advance again.
            assert restored.post("/tiles/tile-00-00/import-operations", json={
                "epoch": 2, "operations": replay_batch,
            }).status_code == 200
            snapshot = restored.get("/tiles/tile-00-00/snapshot").json()
            assert snapshot["last_sequence"] == 3
            assert snapshot["pixels"][0]["owner"] == "bob"
            source_pixels = {"0,0": client.get("/pixels").json()["pixels"][0]["color"]}
            restored_pixels = {"0,0": snapshot["pixels"][0]["color"]}
            assert canonical_canvas_hash(source_pixels) == canonical_canvas_hash(restored_pixels)

        # Reopening the restored shard database repeatedly preserves both the
        # checkpoint boundary and replayed current pixels.
        for _ in range(2):
            restarted = create_app("shard-3", 8003, destination_path)
            with TestClient(restarted) as restored_again:
                repeated = restored_again.get("/tiles/tile-00-00/snapshot").json()
                assert repeated["last_sequence"] == 3
                assert canonical_canvas_hash({"0,0": repeated["pixels"][0]["color"]}) == canonical_canvas_hash(source_pixels)


def test_partial_cross_shard_clear_is_not_logged_until_all_commits_then_replays(tmp_path):
    shard_one = create_app("shard-1", 8001, tmp_path / "one.sqlite3")
    shard_two = create_app("shard-2", 8002, tmp_path / "two.sqlite3")
    destination = create_app("shard-3", 8003, tmp_path / "destination.sqlite3")
    with TestClient(shard_one) as one, TestClient(shard_two) as two, TestClient(destination) as restored:
        sources = [(one, "tile-00-00", 0), (two, "tile-00-01", 25)]
        baselines = []
        for client, tile_id, x in sources:
            assert client.post("/pixels", json={
                "x": x, "y": 0, "tile_id": tile_id, "color": "#ff0000",
                "owner": "alice", "epoch": 1, "operation_id": f"draw-{x}",
            }).status_code == 201
            baselines.append(client.get(f"/tiles/{tile_id}/snapshot").json())
            assert client.post("/clear/cross-shard/prepare", params={"user_id": "alice"}).status_code == 200

        # The first participant deletes pixels, but does not publish a history
        # event before all participants have committed.
        assert one.post("/clear/cross-shard/commit").status_code == 200
        assert one.get("/tiles/tile-00-00/operations", params={"after": 1}).json()["operations"] == []

        assert two.post("/clear/cross-shard/commit").status_code == 200
        assert one.post("/clear/cross-shard/finalize").status_code == 200
        assert two.post("/clear/cross-shard/finalize").status_code == 200

        for (source, tile_id, _), baseline in zip(sources, baselines):
            assert restored.post(f"/tiles/{tile_id}/install", json={
                "epoch": 2, "last_sequence": baseline["last_sequence"],
                "pixels": baseline["pixels"], "operations": [],
            }).status_code == 200
            history = source.get(f"/tiles/{tile_id}/operations", params={"after": 1}).json()["operations"]
            assert len(history) == 1
            assert history[0]["payload"]["kind"] == "clear"
            assert history[0]["payload"]["clear_operation_id"] == "cross-shard"
            assert restored.post(f"/tiles/{tile_id}/import-operations", json={
                "epoch": 2, "operations": history,
            }).status_code == 200
            assert restored.get(f"/tiles/{tile_id}/snapshot").json()["pixels"] == []


def test_undo_is_a_durable_replayable_tile_operation(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = create_app("shard-1", 8001, source_path)
    destination_path = tmp_path / "destination.sqlite3"
    destination = create_app("shard-2", 8002, destination_path)
    with TestClient(source) as owner, TestClient(destination) as restored:
        tile = "tile-00-00"
        alice = {"x": 0, "y": 0, "tile_id": tile, "color": "#ff0000",
                 "owner": "alice", "epoch": 1, "operation_id": "alice-red"}
        bob = {**alice, "color": "#0000ff", "owner": "bob", "operation_id": "bob-blue"}
        assert owner.post("/pixels", json=alice).status_code == 201
        baseline = owner.get(f"/tiles/{tile}/snapshot").json()
        assert owner.post("/pixels", json=bob).status_code == 201
        undo_body = {
            "x": 0, "y": 0, "tile_id": tile, "owner": "bob", "epoch": 1,
            "action_operation_id": "bob-blue", "operation_id": "undo-bob-blue",
            "expected_color": "#0000FF",
            "previous_pixel": {"x": 0, "y": 0, "color": "#FF0000", "owner": "alice"},
        }
        undone = owner.post("/undo", json=undo_body).json()
        assert undone["status"] == "undone" and undone["sequence"] == 3
        assert owner.post("/undo", json=undo_body).json()["duplicate"] is True
        events = owner.get(f"/tiles/{tile}/operations", params={"after": baseline["last_sequence"]}).json()["operations"]

        assert restored.post(f"/tiles/{tile}/install", json={
            "epoch": 2, "last_sequence": baseline["last_sequence"],
            "pixels": baseline["pixels"], "operations": [],
        }).status_code == 200
        batch = {"epoch": 2, "operations": events}
        assert restored.post(f"/tiles/{tile}/import-operations", json=batch).status_code == 200
        assert restored.post(f"/tiles/{tile}/import-operations", json=batch).status_code == 200
        source_snapshot = owner.get(f"/tiles/{tile}/snapshot").json()
        restored_snapshot = restored.get(f"/tiles/{tile}/snapshot").json()
        assert source_snapshot["pixels"] == restored_snapshot["pixels"]
        colors = lambda snapshot: {f"{p['x']},{p['y']}": p["color"] for p in snapshot["pixels"]}
        assert canonical_canvas_hash(colors(source_snapshot)) == canonical_canvas_hash(colors(restored_snapshot))

    for _ in range(2):
        restarted = create_app("shard-2", 8002, destination_path)
        with TestClient(restarted) as client:
            snapshot = client.get(f"/tiles/{tile}/snapshot").json()
            assert snapshot["last_sequence"] == 3
            assert snapshot["pixels"][0]["owner"] == "alice"


def test_saved_snapshot_restores_through_clear_replay_after_repeated_restarts(tmp_path):
    database = tmp_path / "recover.sqlite3"
    app = create_app("shard-1", 8001, database)
    tile = "tile-00-00"
    with TestClient(app) as client:
        draw = {"x": 1, "y": 1, "tile_id": tile, "color": "#ff0000",
                "owner": "alice", "epoch": 1, "operation_id": "snapshot-draw"}
        assert client.post("/pixels", json=draw).status_code == 201
        checkpoint = client.post(f"/tiles/{tile}/snapshots").json()
        assert checkpoint["epoch"] == 1 and checkpoint["last_sequence"] == 1
        blue = {**draw, "color": "#0000ff", "owner": "bob", "operation_id": "after-snapshot"}
        assert client.post("/pixels", json=blue).status_code == 201
        prepared = client.post("/clear/after-snapshot-clear/prepare", params={"user_id": "bob"}).json()
        assert prepared["coordinates"] == [{"x": 1, "y": 1}]
        assert client.post("/clear/after-snapshot-clear/commit").status_code == 200
        assert client.post("/clear/after-snapshot-clear/finalize").status_code == 200
        reference = client.get(f"/tiles/{tile}/snapshot").json()
        assert reference["last_sequence"] == 3 and reference["pixels"] == []
        reference_hash = canonical_canvas_hash({})

    for restart in range(3):
        restarted = create_app("shard-1", 8001, database)
        with TestClient(restarted) as client:
            restored = client.post(
                f"/tiles/{tile}/snapshots/{checkpoint['snapshot_id']}/restore"
            )
            assert restored.status_code == 200
            snapshot = client.get(f"/tiles/{tile}/snapshot").json()
            assert snapshot["epoch"] == reference["epoch"]
            assert snapshot["last_sequence"] == reference["last_sequence"]
            assert snapshot["pixels"] == reference["pixels"]
            assert canonical_canvas_hash({}) == reference_hash
            # A replayed operation ID is still a duplicate and cannot resurrect
            # pixels removed by the later Clear event.
            assert client.post("/pixels", json=draw).json()["duplicate"] is True
            assert client.get(f"/tiles/{tile}/snapshot").json()["pixels"] == []
