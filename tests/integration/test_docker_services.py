"""Real-container checks. Start the Compose project and set RUN_DOCKER_INTEGRATION=1."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
import uuid

import pytest
from websockets.sync.client import connect as websocket_connect

pytestmark = pytest.mark.integration

if os.getenv("RUN_DOCKER_INTEGRATION") != "1":
    pytest.skip("set RUN_DOCKER_INTEGRATION=1 to run against real containers", allow_module_level=True)
if shutil.which("docker") is None:
    pytest.skip("Docker CLI is not installed", allow_module_level=True)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://127.0.0.1:8000")


def request_json(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def browser_json(
    browser, url: str, method: str = "GET", payload: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with browser.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=PROJECT_ROOT, check=check,
        capture_output=True, text=True, timeout=60,
    )


def call_container_shard(shard: str, port: int, path: str, method: str = "GET", payload: dict | None = None):
    script = (
        "import json, urllib.request; "
        f"data={json.dumps(json.dumps(payload))}; "
        f"body=data.encode() if {payload is not None!r} else None; "
        f"request=urllib.request.Request('http://127.0.0.1:{port}{path}', data=body, method={method!r}); "
        "request.add_header('Content-Type','application/json') if body else None; "
        "print(urllib.request.urlopen(request, timeout=5).read().decode())"
    )
    result = compose("exec", "-T", shard, "python", "-c", script)
    return json.loads(result.stdout)


def post_container_shard_status(shard: str, port: int, path: str, payload: dict) -> tuple[int, dict]:
    script = f"""import json, urllib.request, urllib.error
payload = json.loads({json.dumps(json.dumps(payload))})
data = json.dumps(payload).encode()
request = urllib.request.Request('http://127.0.0.1:{port}{path}', data=data, method='POST')
request.add_header('Content-Type', 'application/json')
try:
    response = urllib.request.urlopen(request, timeout=5)
    code, body = response.status, response.read()
except urllib.error.HTTPError as error:
    code, body = error.code, error.read()
print(json.dumps({{'status': code, 'body': json.loads(body)}}))
"""
    result = compose("exec", "-T", shard, "python", "-c", script)
    response = json.loads(result.stdout)
    return response["status"], response["body"]


def open_browser_session():
    cookie_jar = CookieJar()
    browser = build_opener(HTTPCookieProcessor(cookie_jar))
    with browser.open(f"{COORDINATOR_URL}/", timeout=5):
        pass
    session_cookie = next(cookie.value for cookie in cookie_jar if cookie.name == "canvas_session")
    return browser, session_cookie


def websocket_url() -> str:
    base_url = COORDINATOR_URL.rstrip("/")
    return base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/ws"


def volume_name(shard: str) -> str:
    container_id = compose("ps", "-q", shard).stdout.strip()
    details = subprocess.check_output(["docker", "inspect", container_id], text=True)
    container = json.loads(details)[0]
    return next(mount["Name"] for mount in container["Mounts"] if mount["Destination"] == "/data")


def test_compose_shards_use_isolated_volumes_and_keep_records_after_restart():
    try:
        with urlopen(f"{COORDINATOR_URL}/health", timeout=2) as response:
            assert response.status == 200
    except (URLError, TimeoutError) as exc:
        pytest.fail(f"Coordinator is not running at {COORDINATOR_URL}: {exc}")

    status, overview = request_json(f"{COORDINATOR_URL}/admin/shards")
    assert status == 200
    shards = overview["shards"]
    assert [shard["shard_id"] for shard in shards] == ["shard-1", "shard-2", "shard-3"]
    assert all(shard["status"] == "healthy" for shard in shards)
    assert [shard["url"] for shard in shards] == [
        "http://shard-1:8001", "http://shard-2:8002", "http://shard-3:8003"
    ]

    volumes = [volume_name(shard) for shard in ("shard-1", "shard-2", "shard-3")]
    assert len(set(volumes)) == 3

    browser_a, cookie_a = open_browser_session()
    browser_b, cookie_b = open_browser_session()
    assert cookie_a != cookie_b

    # Keep the required test coordinates, but restore any pre-existing canvas
    # records after the test so repeated runs do not leave test pixels behind.
    test_coordinates = {(25, 0), (26, 0)}
    original_records = {
        (pixel["x"], pixel["y"]): pixel
        for pixel in call_container_shard("shard-2", 8002, "/pixels")["pixels"]
        if (pixel["x"], pixel["y"]) in test_coordinates
    }
    baseline_status, baseline = request_json(f"{COORDINATOR_URL}/state")
    assert baseline_status == 200

    try:
        with (
            websocket_connect(
                websocket_url(), additional_headers={"Cookie": f"canvas_session={cookie_a}"},
                open_timeout=5, close_timeout=5,
            ) as socket_a,
            websocket_connect(
                websocket_url(), additional_headers={"Cookie": f"canvas_session={cookie_b}"},
                open_timeout=5, close_timeout=5,
            ) as socket_b,
        ):
            initial_a = json.loads(socket_a.recv(timeout=5))
            initial_b = json.loads(socket_b.recv(timeout=5))
            # Both /state and the WebSocket snapshot expose public pixels as
            # coordinate-to-colour strings, e.g. {"25,0": "#ABCDEF"}.
            expected_snapshot = baseline["pixels"]
            assert initial_a == {"type": "state", "pixels": expected_snapshot}
            assert initial_b == {"type": "state", "pixels": expected_snapshot}

            status, operation_a = browser_json(
                browser_a, f"{COORDINATOR_URL}/draw", "POST",
                {"x": 25, "y": 0, "color": "#abcdef"},
            )
            assert status == 200
            assert operation_a["tile_id"] == "tile-00-01"
            assert operation_a["shard_id"] == "shard-2"
            expected_a = {"type": "draw", "x": 25, "y": 0, "color": "#ABCDEF"}
            assert json.loads(socket_a.recv(timeout=5)) == expected_a
            assert json.loads(socket_b.recv(timeout=5)) == expected_a

            shard_two_pixels = call_container_shard("shard-2", 8002, "/pixels")["pixels"]
            shard_one_pixels = call_container_shard("shard-1", 8001, "/pixels")["pixels"]
            record_a = next(
                pixel for pixel in shard_two_pixels
                if (pixel["x"], pixel["y"]) == (25, 0)
            )
            assert record_a["tile_id"] == "tile-00-01"
            assert record_a["color"] == "#ABCDEF"
            assert not any((pixel["x"], pixel["y"]) == (25, 0) for pixel in shard_one_pixels)

            rejected_status, _ = post_container_shard_status(
                "shard-1", 8001, "/pixels",
                {
                    "x": 25, "y": 0, "tile_id": "tile-00-01",
                    "color": "#112233", "owner": f"integration-rejected-{uuid.uuid4().hex}",
                },
            )
            assert rejected_status == 409
            assert not any(
                (pixel["x"], pixel["y"]) == (25, 0)
                for pixel in call_container_shard("shard-1", 8001, "/pixels")["pixels"]
            )

            refreshed_status, refreshed = browser_json(browser_b, f"{COORDINATOR_URL}/state")
            assert refreshed_status == 200
            assert refreshed["pixels"]["25,0"] == "#ABCDEF"

            status, operation_b = browser_json(
                browser_b, f"{COORDINATOR_URL}/draw", "POST",
                {"x": 26, "y": 0, "color": "#fedcba"},
            )
            assert status == 200
            assert operation_b["tile_id"] == "tile-00-01"
            assert operation_b["shard_id"] == "shard-2"
            expected_b = {"type": "draw", "x": 26, "y": 0, "color": "#FEDCBA"}
            assert json.loads(socket_a.recv(timeout=5)) == expected_b
            assert json.loads(socket_b.recv(timeout=5)) == expected_b
    finally:
        # These sessions were created only for this test, so owner-scoped clear
        # removes its writes without affecting records owned by other sessions.
        browser_json(browser_a, f"{COORDINATOR_URL}/clear", "DELETE")
        browser_json(browser_b, f"{COORDINATOR_URL}/clear", "DELETE")
        for record in original_records.values():
            call_container_shard("shard-2", 8002, "/pixels", "POST", record)

    record_key = f"integration-{uuid.uuid4().hex}"
    record = {"record_key": record_key, "value": "survives-restart"}
    written = call_container_shard("shard-1", 8001, "/records", "POST", record)
    assert written["value"] == "survives-restart"
    with pytest.raises(subprocess.CalledProcessError):
        call_container_shard("shard-2", 8002, f"/records/{record_key}")

    compose("restart", "shard-1")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            health_status, current = request_json(f"{COORDINATOR_URL}/admin/shards")
            if health_status == 200 and all(s["status"] == "healthy" for s in current["shards"]):
                break
        except (URLError, TimeoutError):
            pass
        time.sleep(1)
    else:
        pytest.fail("shard-1 did not return to healthy status after restart")

    restored = call_container_shard("shard-1", 8001, f"/records/{record_key}")
    assert restored["value"] == "survives-restart"
