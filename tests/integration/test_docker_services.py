"""Real-container checks. Start the Compose project and set RUN_DOCKER_INTEGRATION=1."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

import pytest

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
