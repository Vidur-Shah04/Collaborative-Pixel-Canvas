import asyncio

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect
from starlette.requests import Request as StarletteRequest

from frontend import main


@pytest.fixture(autouse=True)
def reset_memory_state():
    main.pixels.clear()
    main.sessions.clear()
    main.manager.active_connections.clear()
    main.canvas_lock = asyncio.Lock()
    yield


def test_different_sessions_draw_and_clear_only_their_current_pixels():
    alice = TestClient(main.app)
    bob = TestClient(main.app)

    assert alice.get("/").status_code == 200
    assert bob.get("/").status_code == 200
    alice.post("/draw", json={"x": 4, "y": 5, "color": "#ff0000"})
    bob.post("/draw", json={"x": 4, "y": 5, "color": "#00ff00"})
    alice.post("/draw", json={"x": 9, "y": 7, "color": "#0000ff"})

    result = alice.delete("/clear")

    assert result.json() == {"status": "ok", "cleared": 1}
    assert alice.get("/state").json()["pixels"] == {"4,5": "#00FF00"}
    assert bob.get("/state").json()["pixels"] == {"4,5": "#00FF00"}


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
