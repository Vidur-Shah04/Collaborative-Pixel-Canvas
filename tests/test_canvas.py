from fastapi.testclient import TestClient
import pytest

from frontend import main


@pytest.fixture(autouse=True)
def reset_memory_state():
    main.pixels.clear()
    main.sessions.clear()
    main.manager.active_connections.clear()
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
    assert client.post("/draw", json={"x": 1000, "y": 0, "color": "#ffffff"}).status_code == 422
    assert client.post("/draw", json={"x": 0, "y": 0, "color": "red"}).status_code == 422


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
