import asyncio
import sqlite3

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect
from starlette.requests import Request as StarletteRequest

from frontend import main
from shared.tiles import configured_shard_ids, shard_for_tile, tile_for_pixel


@pytest.fixture(autouse=True)
def reset_memory_state(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "OWNERSHIP_DB_PATH", tmp_path / "ownership.sqlite3")
    main.pixels.clear()
    main.sessions.clear()
    main.manager.active_connections.clear()
    main.canvas_lock = asyncio.Lock()
    shard_pixels = {shard_id: {} for shard_id in configured_shard_ids()}

    async def fake_request_shard(shard_id, method, path, *, params=None, body=None):
        records = shard_pixels[shard_id]
        if path != "/pixels":
            raise AssertionError(f"Unexpected shard path: {path}")
        if method == "POST":
            tile = tile_for_pixel(body["x"], body["y"])
            assert body["tile_id"] == tile.tile_id
            assert shard_for_tile(tile) == shard_id
            record = {
                "x": body["x"], "y": body["y"], "tile_id": tile.tile_id,
                "color": body["color"], "owner": body["owner"],
            }
            records[(record["x"], record["y"])] = record
            return {"status": "committed", "shard_id": shard_id, **record}
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

    assert result.json() == {"status": "ok", "cleared": 1}
    assert alice.get("/state").json()["pixels"] == {"12,12": "#00FF00"}
    assert bob.get("/state").json()["pixels"] == {"12,12": "#00FF00"}

    # The new owner may clear the overwritten pixel.
    assert bob.delete("/clear").json() == {"status": "ok", "cleared": 1}
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

    assert client.delete("/clear").json() == {"status": "ok", "cleared": 2}
    assert client.get("/state").json()["pixels"] == {}
    assert client.delete("/clear").json() == {"status": "ok", "cleared": 0}


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
        assert socket.receive_json() == {
            "type": "state", "pixels": {"1,2": "#123456"}
        }
        client.delete("/clear")
        client.post("/draw", json={"x": 3, "y": 4, "color": "#abcdef"})

        assert socket.receive_json() == {"type": "clear", "pixels": [[1, 2]]}
        assert socket.receive_json() == {
            "type": "draw", "x": 3, "y": 4, "color": "#ABCDEF"
        }


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
            {"type": "state", "pixels": {}},
            {"type": "draw", "x": 7, "y": 8, "color": "#ABCDEF"},
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

        assert clear_response.body == b'{"status":"ok","cleared":1}'
        assert main.pixels == {}
        assert [message["type"] for message in websocket.messages] == ["draw", "clear"]

    asyncio.run(scenario())


def test_websocket_receives_state_and_committed_draw_update():
    client = TestClient(main.app)
    client.get("/")
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json() == {"type": "state", "pixels": {}}
        response = client.post("/draw", json={"x": 12, "y": 3, "color": "#abcdef"})
        assert response.status_code == 200
        assert socket.receive_json() == {
            "type": "draw", "x": 12, "y": 3, "color": "#ABCDEF"
        }
def test_session_survives_coordinator_memory_reset_and_retains_clear_rights():
    client = TestClient(main.app)
    client.get("/")
    token = client.cookies[main.SESSION_COOKIE]
    owner = main.sessions[token]
    client.post("/draw", json={"x": 4, "y": 5, "color": "#ff0000"})
    main.sessions.clear()
    assert main.session_for(token) == (token, owner)
    assert client.delete("/clear").json() == {"status": "ok", "cleared": 1}


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
        if method == "DELETE" and shard_id == "shard-2":
            raise main.ShardRequestError(shard_id, "simulated failure")
        return await original(shard_id, method, path, params=params, body=body)

    monkeypatch.setattr(main, "request_shard", fail_one_clear)
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "state"
        response = client.delete("/clear")
        assert response.status_code == 503
        assert response.json()["status"] == "partial"
        assert socket.receive_json() == {"type": "state", "pixels": {}}
