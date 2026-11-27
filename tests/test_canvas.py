import asyncio
import json
import sqlite3
import time
from contextlib import ExitStack

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect
from starlette.requests import Request as StarletteRequest

from frontend import main
from shared.tiles import configured_shard_ids, shard_for_tile, tile_for_pixel
from shard.main import create_app as create_shard_app


@pytest.fixture(autouse=True)
def reset_memory_state(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "OWNERSHIP_DB_PATH", tmp_path / "ownership.sqlite3")
    main.pixels.clear()
    main.sessions.clear()
    main.manager.active_connections.clear()
    main.canvas_lock = asyncio.Lock()
    shard_pixels = {shard_id: {} for shard_id in configured_shard_ids()}
    prepared_clears = {shard_id: {} for shard_id in configured_shard_ids()}
    tile_positions = {shard_id: {} for shard_id in configured_shard_ids()}
    draw_operations = {shard_id: {} for shard_id in configured_shard_ids()}
    undo_operations = {shard_id: {} for shard_id in configured_shard_ids()}

    async def fake_request_shard(shard_id, method, path, *, params=None, body=None):
        records = shard_pixels[shard_id]
        if path.startswith("/clear/"):
            operation_id, action = path.removeprefix("/clear/").split("/")
            if action == "prepare":
                previous = prepared_clears[shard_id].get(operation_id)
                coords = previous["coordinates"] if previous is not None else [
                    {"x": row["x"], "y": row["y"]} for row in records.values()
                    if row["owner"] == params["user_id"]
                ]
                prepared_clears[shard_id][operation_id] = {"user_id": params["user_id"], "coordinates": coords}
                return {"status": "prepared", "operation_id": operation_id, "coordinates": coords}
            if action == "commit":
                prepared = prepared_clears[shard_id][operation_id]
                coords = prepared["coordinates"]
                for point in coords:
                    row = records.get((point["x"], point["y"]))
                    if row and row["owner"] == prepared["user_id"]:
                        records.pop((point["x"], point["y"]))
                return {"status": "committed", "operation_id": operation_id, "coordinates": coords}
            if action == "finalize":
                prepared = prepared_clears[shard_id][operation_id]
                if prepared.get("finalized"):
                    return {"status": "finalized", "operation_id": operation_id,
                            "positions": prepared["positions"]}
                positions = []
                tile_ids = sorted({tile_for_pixel(point["x"], point["y"]).tile_id
                                   for point in prepared["coordinates"]})
                for tile_id in tile_ids:
                    position = tile_positions[shard_id].setdefault(tile_id, {"epoch": 1, "sequence": 0})
                    position["sequence"] += 1
                    positions.append({"tile_id": tile_id, **position})
                prepared["finalized"] = True
                prepared["positions"] = positions
                return {"status": "finalized", "operation_id": operation_id, "positions": positions}
            raise AssertionError(f"Unexpected clear action: {action}")
        if path == "/tiles/positions":
            return {"shard_id": shard_id, "positions": [
                {"tile_id": tile_id, "epoch": value["epoch"], "last_sequence": value["sequence"]}
                for tile_id, value in tile_positions[shard_id].items()
            ]}
        if path == "/undo" and method == "POST":
            if body["operation_id"] in undo_operations[shard_id]:
                return {**undo_operations[shard_id][body["operation_id"]], "duplicate": True}
            key = (body["x"], body["y"])
            current = records.get(key)
            tile_id = body["tile_id"]
            position = tile_positions[shard_id].setdefault(tile_id, {"epoch": body["epoch"], "sequence": 0})
            status = "undone" if current and current["owner"] == body["owner"] and current["color"] == body["expected_color"] else "skipped"
            sequence = position["sequence"] + 1
            if status == "undone":
                if body["previous_pixel"] is None:
                    records.pop(key, None)
                else:
                    records[key] = {**body["previous_pixel"], "tile_id": tile_id}
            position["sequence"] = sequence
            result = {"status": status, "operation_id": body["operation_id"],
                      "tile_id": tile_id, "epoch": position["epoch"], "sequence": sequence,
                      "x": body["x"], "y": body["y"], "previous_pixel": body["previous_pixel"]}
            undo_operations[shard_id][body["operation_id"]] = result
            return result
        if path != "/pixels":
            raise AssertionError(f"Unexpected shard path: {path}")
        if method == "POST":
            tile = tile_for_pixel(body["x"], body["y"])
            assert body["tile_id"] == tile.tile_id
            assert shard_for_tile(tile) == shard_id
            if body["operation_id"] in draw_operations[shard_id]:
                return {**draw_operations[shard_id][body["operation_id"]], "duplicate": True}
            record = {
                "x": body["x"], "y": body["y"], "tile_id": tile.tile_id,
                "color": body["color"], "owner": body["owner"],
            }
            records[(record["x"], record["y"])] = record
            position = tile_positions[shard_id].setdefault(tile.tile_id, {"epoch": body["epoch"], "sequence": 0})
            position["sequence"] += 1
            result = {"status": "committed", "shard_id": shard_id, "epoch": position["epoch"],
                      "sequence": position["sequence"], **record}
            draw_operations[shard_id][body["operation_id"]] = result
            return result
        if method == "GET":
            return {"pixels": list(records.values())}
        if method == "DELETE":
            owner = params["user_id"]
            removed = [record for record in records.values() if record["owner"] == owner]
            for record in removed:
                records.pop((record["x"], record["y"]))
            return {"removed": [{"x": r["x"], "y": r["y"]} for r in removed]}
        raise AssertionError(f"Unexpected shard method: {method}")

    monkeypatch.setattr(main, "request_shard", fake_request_shard)
    yield


def test_different_sessions_draw_and_clear_only_their_current_pixels():
    alice = TestClient(main.app)
    bob = TestClient(main.app)

    assert alice.get("/").status_code == 200
    assert bob.get("/").status_code == 200
    alice.post("/draw", json={"x": 12, "y": 12, "color": "#ff0000"})
    bob.post("/draw", json={"x": 12, "y": 12, "color": "#00ff00"})
    alice.post("/draw", json={"x": 9, "y": 7, "color": "#0000ff"})

    result = alice.delete("/clear")

    assert result.json()["status"] == "ok"
    assert result.json()["cleared"] == 1
    assert result.json()["operation_id"]
    assert alice.get("/state").json()["pixels"] == {"12,12": "#00FF00"}
    assert bob.get("/state").json()["pixels"] == {"12,12": "#00FF00"}

    # The new owner may clear the overwritten pixel.
    result = bob.delete("/clear").json()
    assert result["status"] == "ok"
    assert result["cleared"] == 1
    assert alice.get("/state").json()["pixels"] == {}


def test_coordinator_routes_each_draw_to_the_deterministic_tile_owner():
    client = TestClient(main.app)
    client.get("/")

    origin_tile = client.post("/draw", json={"x": 0, "y": 0, "color": "#ff0000"}).json()
    next_tile = client.post("/draw", json={"x": 25, "y": 0, "color": "#00ff00"}).json()

    assert (origin_tile["tile_id"], origin_tile["shard_id"]) == ("tile-00-00", "shard-1")
    assert (next_tile["tile_id"], next_tile["shard_id"]) == ("tile-00-01", "shard-2")


def test_unavailable_owner_shard_is_not_replaced_by_another_shard(monkeypatch):
    client = TestClient(main.app)
    client.get("/")
    selected_shards = []

    async def unavailable_owner(shard_id, method, path, *, params=None, body=None):
        selected_shards.append(shard_id)
        raise main.ShardRequestError(shard_id, "owner shard is unavailable")

    monkeypatch.setattr(main, "request_shard", unavailable_owner)
    response = client.post("/draw", json={"x": 25, "y": 0, "color": "#00ff00"})

    assert response.status_code == 503
    assert selected_shards == ["shard-2"]
    assert main.pixels == {}


def test_clear_removes_unoverwritten_pixels_and_repeated_clear_is_a_noop():
    client = TestClient(main.app)
    client.get("/")
    client.post("/draw", json={"x": 12, "y": 12, "color": "#ff0000"})
    client.post("/draw", json={"x": 13, "y": 12, "color": "#0000ff"})

    result = client.delete("/clear").json()
    assert result["status"] == "ok"
    assert result["cleared"] == 2
    assert result["operation_id"]
    assert client.get("/state").json()["pixels"] == {}
    result = client.delete("/clear").json()
    assert result["status"] == "ok"
    assert result["cleared"] == 0


def test_clear_only_removes_its_owner_when_users_have_distinct_pixels():
    alice = TestClient(main.app)
    bob = TestClient(main.app)
    alice.get("/")
    bob.get("/")
    alice.post("/draw", json={"x": 1, "y": 1, "color": "#ff0000"})
    alice.post("/draw", json={"x": 2, "y": 2, "color": "#ff0000"})
    bob.post("/draw", json={"x": 3, "y": 3, "color": "#00ff00"})

    assert alice.delete("/clear").json()["cleared"] == 2
    assert bob.get("/state").json()["pixels"] == {"3,3": "#00FF00"}
    assert bob.delete("/clear").json()["cleared"] == 1
    assert alice.get("/state").json()["pixels"] == {}


def test_draw_validates_canvas_coordinates_and_hex_color():
    client = TestClient(main.app)
    invalid_draws = [
        {"x": -1, "y": 0, "color": "#ffffff"},
        {"x": 1000, "y": 0, "color": "#ffffff"},
        {"x": 1.5, "y": 0, "color": "#ffffff"},
        {"x": True, "y": 0, "color": "#ffffff"},
        {"x": 0, "y": 0, "color": "red"},
        {"x": 0, "y": 0, "color": "#fffffff"},
    ]
    for payload in invalid_draws:
        assert client.post("/draw", json=payload).status_code == 422


def test_session_cookie_is_secure_for_local_http_and_survives_refresh():
    client = TestClient(main.app)
    first_page = client.get("/")
    cookie = first_page.cookies[main.SESSION_COOKIE]
    attributes = first_page.headers["set-cookie"].lower()

    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert "secure" not in attributes  # Local HTTP development.
    user_id = main.sessions[cookie]

    refreshed_page = client.get("/")
    assert refreshed_page.cookies[main.SESSION_COOKIE] == cookie
    assert client.get("/state").status_code == 200
    assert main.sessions[cookie] == user_id


def test_two_browser_sessions_have_distinct_server_generated_ids():
    alice = TestClient(main.app)
    bob = TestClient(main.app)
    alice.get("/")
    bob.get("/")

    alice_id = main.sessions[alice.cookies[main.SESSION_COOKIE]]
    bob_id = main.sessions[bob.cookies[main.SESSION_COOKIE]]

    assert alice_id != bob_id
    assert alice_id != alice.cookies[main.SESSION_COOKIE]
    assert bob_id != bob.cookies[main.SESSION_COOKIE]

    # An extra client-supplied identity field cannot claim another session's pixels.
    alice.post("/draw", json={"x": 20, "y": 20, "color": "#ff0000", "user_id": bob_id})
    assert main.pixels["20,20"]["owner"] == alice_id


def test_websocket_requires_a_server_registered_session():
    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_websocket_snapshot_and_broadcast_order_across_clear_and_draw():
    client = TestClient(main.app)
    client.get("/")
    client.post("/draw", json={"x": 1, "y": 2, "color": "#123456"})

    with client.websocket_connect("/ws") as socket:
        initial = socket.receive_json()
        assert initial["type"] == "state"
        assert initial["pixels"] == {"1,2": "#123456"}
        assert initial["positions"]["tile-00-00"] == {"epoch": 1, "sequence": 1}
        client.delete("/clear")
        client.post("/draw", json={"x": 3, "y": 4, "color": "#abcdef"})

        clear = socket.receive_json()
        assert clear["type"] == "clear" and clear["pixels"] == [[1, 2]]
        assert clear["positions"] == [{"tile_id": "tile-00-00", "epoch": 1, "sequence": 2}]
        draw = socket.receive_json()
        assert draw == {"type": "draw", "x": 3, "y": 4, "color": "#ABCDEF",
                        "tile_id": "tile-00-00", "epoch": 1, "sequence": 3}


def test_disconnected_websocket_is_removed_from_connection_manager():
    client = TestClient(main.app)
    client.get("/")
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "state"
        assert len(main.manager.active_connections) == 1

    assert main.manager.active_connections == []


def test_draw_waits_until_websocket_initial_snapshot_is_sent():
    async def scenario():
        token, user_id = main.session_for(None)
        state_started = asyncio.Event()
        allow_state_send = asyncio.Event()
        allow_disconnect = asyncio.Event()

        class PausingWebSocket:
            cookies = {main.SESSION_COOKIE: token}
            messages = []

            async def accept(self):
                pass

            async def send_json(self, message):
                if message["type"] == "state":
                    state_started.set()
                    await allow_state_send.wait()
                self.messages.append(message)

            async def receive_text(self):
                await allow_disconnect.wait()
                raise WebSocketDisconnect()

        websocket = PausingWebSocket()
        socket_task = asyncio.create_task(main.websocket_endpoint(websocket))
        await state_started.wait()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/draw", "raw_path": b"/draw",
            "query_string": b"", "headers": [(b"cookie", f"{main.SESSION_COOKIE}={token}".encode())],
            "server": ("testserver", 80), "client": ("testclient", 123),
        }
        draw_task = asyncio.create_task(
            main.draw(main.DrawRequest(x=7, y=8, color="#abcdef"), StarletteRequest(scope))
        )
        await asyncio.sleep(0)
        assert not draw_task.done()

        allow_state_send.set()
        await draw_task
        allow_disconnect.set()
        await socket_task

        assert user_id == main.pixels["7,8"]["owner"]
        assert websocket.messages == [
            {"type": "state", "pixels": {}, "positions": {}},
            {"type": "draw", "x": 7, "y": 8, "color": "#ABCDEF",
             "tile_id": "tile-00-00", "epoch": 1, "sequence": 1},
        ]
        assert main.manager.active_connections == []

    asyncio.run(scenario())


def test_concurrent_clear_waits_for_inflight_draw_broadcast_and_leaves_consistent_state():
    async def scenario():
        token, user_id = main.session_for(None)
        draw_broadcast_started = asyncio.Event()
        allow_draw_broadcast = asyncio.Event()

        class PausingWebSocket:
            messages = []

            async def send_json(self, message):
                self.messages.append(message)
                if message["type"] == "draw":
                    draw_broadcast_started.set()
                    await allow_draw_broadcast.wait()

        websocket = PausingWebSocket()
        main.manager.active_connections.append(websocket)
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/draw", "raw_path": b"/draw",
            "query_string": b"", "headers": [(b"cookie", f"{main.SESSION_COOKIE}={token}".encode())],
            "server": ("testserver", 80), "client": ("testclient", 123),
        }
        draw_task = asyncio.create_task(
            main.draw(main.DrawRequest(x=12, y=12, color="#ff0000"), StarletteRequest(scope))
        )
        await draw_broadcast_started.wait()
        assert main.pixels["12,12"] == {"color": "#FF0000", "owner": user_id}

        clear_scope = {**scope, "method": "DELETE", "path": "/clear", "raw_path": b"/clear"}
        clear_task = asyncio.create_task(main.clear_my_pixels(StarletteRequest(clear_scope)))
        await asyncio.sleep(0)
        assert not clear_task.done()

        allow_draw_broadcast.set()
        await draw_task
        clear_response = await clear_task

        clear_result = json.loads(clear_response.body)
        assert clear_result["status"] == "ok"
        assert clear_result["cleared"] == 1
        assert clear_result["operation_id"]
        assert main.pixels == {}
        assert [message["type"] for message in websocket.messages] == ["draw", "clear"]

    asyncio.run(scenario())


def test_websocket_receives_state_and_committed_draw_update():
    client = TestClient(main.app)
    client.get("/")
    with client.websocket_connect("/ws") as socket:
        first_state = socket.receive_json()
        assert first_state == {"type": "state", "pixels": {}, "positions": {}}
        response = client.post("/draw", json={"x": 12, "y": 3, "color": "#abcdef"})
        assert response.status_code == 200
        assert socket.receive_json() == {
            "type": "draw", "x": 12, "y": 3, "color": "#ABCDEF",
            "tile_id": "tile-00-00", "epoch": 1, "sequence": 1,
        }


def test_canvas_hash_is_stable_for_pixel_order_and_color_case():
    first = {"25,0": "#abcdef", "0,0": "#123456"}
    reversed_order = {"0,0": "#123456", "25,0": "#ABCDEF"}
    assert main.canonical_canvas_hash(first) == main.canonical_canvas_hash(reversed_order)


def test_hash_endpoint_reports_canonical_sha256_for_authoritative_canvas():
    client = TestClient(main.app)
    client.get("/")
    client.post("/draw", json={"x": 25, "y": 0, "color": "#abcdef"})
    result = client.get("/hash").json()
    assert result == {
        "algorithm": "sha256",
        "serialization": "collaborative-pixel-canvas-v1; tile row-major, local y/x; uppercase #RRGGBB; colors only",
        "pixels": 1,
        "hash": main.canonical_canvas_hash({"25,0": "#ABCDEF"}),
    }


def test_undo_skips_pixel_overwritten_by_another_user():
    alice = TestClient(main.app)
    bob = TestClient(main.app)
    alice.get("/")
    bob.get("/")
    alice.post("/draw", json={"x": 4, "y": 4, "color": "#ff0000"})
    bob.post("/draw", json={"x": 4, "y": 4, "color": "#0000ff"})
    result = alice.post("/undo", headers={"X-Operation-ID": "alice-undo-1"})
    assert result.status_code == 200
    assert result.json()["status"] == "skipped"
    assert main.pixels["4,4"]["owner"] == main.sessions[bob.cookies[main.SESSION_COOKIE]]


def test_undo_restores_prior_owner_when_current_user_undoes_overwrite():
    alice = TestClient(main.app)
    bob = TestClient(main.app)
    alice.get("/")
    bob.get("/")
    alice.post("/draw", json={"x": 7, "y": 8, "color": "#ff0000"})
    bob.post("/draw", json={"x": 7, "y": 8, "color": "#0000ff"})
    result = bob.post("/undo", headers={"X-Operation-ID": "bob-undo-1"})
    assert result.status_code == 200
    assert result.json()["status"] == "undone"
    assert main.pixels["7,8"]["color"] == "#FF0000"
    assert main.pixels["7,8"]["owner"] == main.sessions[alice.cookies[main.SESSION_COOKIE]]


def test_undo_attempt_limit_is_ten_per_session():
    client = TestClient(main.app)
    client.get("/")
    for number in range(1, 11):
        result = client.post("/undo", headers={"X-Operation-ID": f"empty-undo-{number}"})
        assert result.status_code == 200
        assert result.json()["attempts_used"] == number
    assert client.post("/undo", headers={"X-Operation-ID": "empty-undo-11"}).status_code == 429


def test_undo_action_survives_coordinator_restart():
    client = TestClient(main.app)
    client.get("/")
    token = client.cookies[main.SESSION_COOKIE]
    client.post("/draw", json={"x": 11, "y": 12, "color": "#abcdef"})
    client.close()
    main.sessions.clear()
    restarted = TestClient(main.app)
    restarted.cookies.set(main.SESSION_COOKIE, token)
    result = restarted.post("/undo", headers={"X-Operation-ID": "restart-undo"})
    assert result.status_code == 200
    assert result.json()["status"] == "undone"
    assert restarted.get("/state").json()["pixels"] == {}


def test_reconnect_snapshot_recovers_missed_draw_and_clear():
    client = TestClient(main.app)
    client.get("/")
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "state"
        client.post("/draw", json={"x": 1, "y": 1, "color": "#ff0000"})
        assert socket.receive_json()["sequence"] == 1

    # Both operations happen while this browser has no WebSocket connection.
    client.post("/draw", json={"x": 2, "y": 2, "color": "#00ff00"})
    client.delete("/clear")
    client.post("/draw", json={"x": 3, "y": 3, "color": "#0000ff"})
    with client.websocket_connect("/ws") as socket:
        state = socket.receive_json()
        assert state["type"] == "state"
        assert state["pixels"] == {"3,3": "#0000FF"}
        assert state["positions"] == {
            "tile-00-00": {"epoch": 1, "sequence": 4},
        }


def test_clients_connecting_at_different_times_get_current_cursors():
    early = TestClient(main.app)
    late = TestClient(main.app)
    early.get("/")
    late.get("/")
    with early.websocket_connect("/ws") as early_socket:
        assert early_socket.receive_json()["positions"] == {}
        early.post("/draw", json={"x": 5, "y": 6, "color": "#ff0000"})
        event = early_socket.receive_json()
        assert event["sequence"] == 1
        with late.websocket_connect("/ws") as late_socket:
            late_state = late_socket.receive_json()
            assert late_state["pixels"] == {"5,6": "#FF0000"}
            assert late_state["positions"] == {"tile-00-00": {"epoch": 1, "sequence": 1}}
            late.post("/draw", json={"x": 6, "y": 6, "color": "#00ff00"})
            assert early_socket.receive_json()["sequence"] == 2
            assert late_socket.receive_json()["sequence"] == 2


def test_duplicate_draw_request_does_not_emit_duplicate_websocket_event():
    client = TestClient(main.app)
    client.get("/")

    class Recorder:
        messages = []

        async def send_json(self, message):
            self.messages.append(message)

    recorder = Recorder()
    main.manager.active_connections.append(recorder)
    payload = {"x": 5, "y": 5, "color": "#ff0000", "operation_id": "same-draw"}
    assert client.post("/draw", json=payload).status_code == 200
    assert client.post("/draw", json=payload).status_code == 200
    assert len(recorder.messages) == 1
    assert recorder.messages[0]["sequence"] == 1


def test_reconnect_after_coordinator_memory_restart_uses_durable_shard_state():
    client = TestClient(main.app)
    client.get("/")
    client.post("/draw", json={"x": 10, "y": 10, "color": "#abcdef"})
    token = client.cookies[main.SESSION_COOKIE]
    client.close()
    main.sessions.clear()
    main.manager.active_connections.clear()

    restarted_client = TestClient(main.app)
    restarted_client.cookies.set(main.SESSION_COOKIE, token)
    with restarted_client.websocket_connect("/ws") as socket:
        state = socket.receive_json()
        assert state["pixels"] == {"10,10": "#ABCDEF"}
        assert state["positions"] == {"tile-00-00": {"epoch": 1, "sequence": 1}}
def test_session_survives_coordinator_memory_reset_and_retains_clear_rights():
    client = TestClient(main.app)
    client.get("/")
    token = client.cookies[main.SESSION_COOKIE]
    owner = main.sessions[token]
    client.post("/draw", json={"x": 4, "y": 5, "color": "#ff0000"})
    main.sessions.clear()
    assert main.session_for(token) == (token, owner)
    result = client.delete("/clear").json()
    assert result["status"] == "ok"
    assert result["cleared"] == 1
    assert result["operation_id"]


def test_existing_session_table_is_migrated_without_losing_identity():
    main.OWNERSHIP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(main.OWNERSHIP_DB_PATH) as connection:
        connection.execute("CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        connection.execute("INSERT INTO sessions VALUES ('old-token', 'old-owner')")
    assert main.session_for("old-token") == ("old-token", "old-owner")
    assert main.known_session("old-token")


def test_partial_clear_reconciles_clients_and_reports_failure(monkeypatch):
    client = TestClient(main.app)
    client.get("/")
    client.post("/draw", json={"x": 0, "y": 0, "color": "#ff0000"})
    original = main.request_shard

    async def fail_one_clear(shard_id, method, path, *, params=None, body=None):
        if path.endswith("/prepare") and shard_id == "shard-2":
            raise main.ShardRequestError(shard_id, "simulated failure")
        return await original(shard_id, method, path, params=params, body=body)

    monkeypatch.setattr(main, "request_shard", fail_one_clear)
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "state"
        response = client.delete("/clear", headers={"X-Operation-ID": "retry-me"})
        assert response.status_code == 503
        assert main.pixels == {"0,0": {"color": "#FF0000", "owner": next(iter(main.sessions.values()))}}
        monkeypatch.setattr(main, "request_shard", original)
        retried = client.delete("/clear", headers={"X-Operation-ID": "retry-me"})
        assert retried.status_code == 200
        assert retried.json()["cleared"] == 1
        assert socket.receive_json() == {
            "type": "clear", "pixels": [[0, 0]],
            "positions": [{"tile_id": "tile-00-00", "epoch": 1, "sequence": 2}],
        }
        client.post("/draw", json={"x": 0, "y": 0, "color": "#0000FF"})
        duplicate = client.delete("/clear", headers={"X-Operation-ID": "retry-me"})
        assert duplicate.status_code == 200
        assert main.pixels["0,0"]["color"] == "#0000FF"


def test_clear_recovery_finishes_after_a_participant_commit_failure(monkeypatch):
    original = main.request_shard

    class ConnectedBrowser:
        messages = []

        async def send_json(self, message):
            self.messages.append(message)

    browser = ConnectedBrowser()
    main.manager.active_connections.append(browser)

    async def fail_one_commit(shard_id, method, path, *, params=None, body=None):
        if path.endswith("/commit") and shard_id == "shard-2":
            raise main.ShardRequestError(shard_id, "simulated commit outage")
        return await original(shard_id, method, path, params=params, body=body)

    monkeypatch.setattr(main, "request_shard", fail_one_commit)
    with TestClient(main.app) as client:
        client.get("/")
        client.post("/draw", json={"x": 0, "y": 0, "color": "#ff0000"})
        response = client.delete("/clear", headers={"X-Operation-ID": "commit-recovery"})
        assert response.status_code == 503
        with sqlite3.connect(main.OWNERSHIP_DB_PATH) as connection:
            phase = connection.execute(
                "SELECT phase FROM clear_operations WHERE operation_id='commit-recovery'"
            ).fetchone()[0]
        assert phase == "committing"

    # A new application lifespan retries the durable intent from the same coordinator DB.
    monkeypatch.setattr(main, "request_shard", original)
    with TestClient(main.app) as restarted_client:
        import time
        deadline = time.monotonic() + 3
        phase = "committing"
        while phase != "committed" and time.monotonic() < deadline:
            with sqlite3.connect(main.OWNERSHIP_DB_PATH) as connection:
                phase = connection.execute(
                    "SELECT phase FROM clear_operations WHERE operation_id='commit-recovery'"
                ).fetchone()[0]
            if phase != "committed":
                time.sleep(0.05)
        assert phase == "committed"
        assert restarted_client.get("/state").json()["pixels"] == {}
    assert browser.messages[-1] == {"type": "state", "pixels": {},
                                    "positions": {"tile-00-00": {"epoch": 1, "sequence": 2}}}
    main.manager.disconnect(browser)


def test_recovered_cross_shard_clear_replays_from_tile_snapshots(tmp_path, monkeypatch):
    with ExitStack() as stack:
        shards = {
            shard_id: stack.enter_context(TestClient(create_shard_app(
                shard_id, 8001 + index, tmp_path / f"{shard_id}.sqlite3"
            )))
            for index, shard_id in enumerate(configured_shard_ids())
        }
        failure = {"enabled": True}

        async def request_real_shard(shard_id, method, path, *, params=None, body=None):
            if failure["enabled"] and shard_id == "shard-2" and path.endswith("/finalize"):
                raise main.ShardRequestError(shard_id, "injected partial Clear history finalization")
            response = shards[shard_id].request(method, path, params=params, json=body)
            if response.status_code >= 400:
                raise main.ShardRequestError(shard_id, response.text, response.status_code)
            return response.json()

        monkeypatch.setattr(main, "request_shard", request_real_shard)
        with TestClient(main.app) as coordinator:
            coordinator.get("/")
            assert coordinator.post("/draw", json={"x": 0, "y": 0, "color": "#ff0000"}).status_code == 200
            assert coordinator.post("/draw", json={"x": 25, "y": 0, "color": "#0000ff"}).status_code == 200
            baselines = {
                "tile-00-00": shards["shard-1"].get("/tiles/tile-00-00/snapshot").json(),
                "tile-00-01": shards["shard-2"].get("/tiles/tile-00-01/snapshot").json(),
            }
            response = coordinator.delete("/clear", headers={"X-Operation-ID": "history-recovery"})
            assert response.status_code == 503
            with sqlite3.connect(main.OWNERSHIP_DB_PATH) as connection:
                assert connection.execute(
                    "SELECT phase FROM clear_operations WHERE operation_id='history-recovery'"
                ).fetchone()[0] == "finalizing"
            # One participant has a local event, but global completion is pending.
            first_history = shards["shard-1"].get(
                "/tiles/tile-00-00/operations", params={"after": 1}
            ).json()["operations"]
            second_history = shards["shard-2"].get(
                "/tiles/tile-00-01/operations", params={"after": 1}
            ).json()["operations"]
            assert len(first_history) == 1
            assert second_history == []

        failure["enabled"] = False
        with TestClient(main.app) as restarted_coordinator:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with sqlite3.connect(main.OWNERSHIP_DB_PATH) as connection:
                    phase = connection.execute(
                        "SELECT phase FROM clear_operations WHERE operation_id='history-recovery'"
                    ).fetchone()[0]
                if phase == "committed":
                    break
                time.sleep(0.05)
            assert phase == "committed"
            assert restarted_coordinator.get("/state").json()["pixels"] == {}

        destination = stack.enter_context(TestClient(create_shard_app(
            "shard-3", 8003, tmp_path / "restore.sqlite3"
        )))
        for tile_id, owner_shard in (("tile-00-00", "shard-1"), ("tile-00-01", "shard-2")):
            baseline = baselines[tile_id]
            assert destination.post(f"/tiles/{tile_id}/install", json={
                "epoch": 2, "last_sequence": baseline["last_sequence"],
                "pixels": baseline["pixels"], "operations": [],
            }).status_code == 200
            events = shards[owner_shard].get(
                f"/tiles/{tile_id}/operations", params={"after": baseline["last_sequence"]}
            ).json()["operations"]
            assert len(events) == 1
            assert events[0]["payload"]["clear_operation_id"] == "history-recovery"
            assert destination.post(f"/tiles/{tile_id}/import-operations", json={
                "epoch": 2, "operations": events,
            }).status_code == 200
            assert destination.get(f"/tiles/{tile_id}/snapshot").json()["pixels"] == []
