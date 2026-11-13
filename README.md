# Collaborative Pixel Canvas

A FastAPI collaborative canvas with a Docker Compose coordinator and three shard services. The browser talks only to the coordinator. Stage 3A routes each draw to the deterministic tile owner's shard, where the current pixel colour and session owner are stored in that shard's SQLite database.

For the current system diagram and consistency boundaries, see [ARCHITECTURE.md](ARCHITECTURE.md). For the student-oriented file/function walkthrough and reproducible demonstrations, see [FOLLOW_ALONG.md](FOLLOW_ALONG.md).

Tile routing and shard pixel storage are implemented in Stage 3A. Session mappings and active WebSockets remain coordinator-local. Ownership epochs, operation IDs/deduplication, cross-shard atomic Clear, live migration, and crash recovery are not implemented. The opt-in Docker integration test exercises two real browser sessions, shard routing/storage, state refresh, shard-side rejection, and WebSocket snapshots/updates; run it against a live Compose project to verify the current Docker environment.

## Requirements

- Docker Desktop for Windows with the WSL2 backend enabled (or Docker Engine and Compose v2 on macOS/Linux).
- Python 3.11+ for local development and tests.
- A modern browser.

On Windows, install/start Docker Desktop and wait until its engine is running. The Compose files build Linux containers and use a named bridge network; no Linux shell is needed for the normal PowerShell workflow.

## Start with Docker Compose

From the repository root in PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The first build downloads the Python base image and dependencies. The coordinator is available at:

- Canvas: <http://localhost:8000/>
- API docs: <http://localhost:8000/docs>
- Coordinator health: <http://localhost:8000/health>
- Live shard overview: <http://localhost:8000/admin/shards>

Only port 8000 is published to the host. The coordinator reaches shards over Compose DNS at `http://shard-1:8001`, `http://shard-2:8002`, and `http://shard-3:8003` on the private `canvas-net` bridge network.

Useful PowerShell commands:

```powershell
docker compose ps
docker compose logs -f coordinator
docker compose logs -f shard-1
docker compose logs -f shard-2
docker compose logs -f shard-3
docker compose stop shard-2
docker compose start shard-2
docker compose restart shard-1
```

Stopping the project while keeping data:

```powershell
docker compose down
```

This preserves `shard1_data`, `shard2_data`, and `shard3_data`. To intentionally erase all development shard data, run `docker compose down -v`; this permanently deletes those named volumes.

## Verify health and persistence

`GET /admin/shards` makes concurrent HTTP health requests to every configured shard. An unavailable service is reported as `unavailable`; it does not take down the coordinator. Each shard's internal API includes `GET /health`, `GET /info`, `GET /records`, `GET /records/{key}`, and `POST /records`.

Shard ports are intentionally not published to the host. You can write a verification record inside shard-1 from PowerShell:

```powershell
docker compose exec shard-1 python -c "import json,urllib.request; data=json.dumps({'record_key':'demo','value':'kept'}).encode(); req=urllib.request.Request('http://127.0.0.1:8001/records',data=data,headers={'Content-Type':'application/json'},method='POST'); print(urllib.request.urlopen(req).read().decode())"
docker compose restart shard-1
docker compose exec shard-1 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8001/records/demo').read().decode())"
```

The record should remain after the restart. Each Compose service mounts a different named volume at `/data`; the databases are not shared. Container recreation also keeps the named volume unless you remove it with `down -v`.

## Add shard-4 later

Dynamic container creation is not implemented. To add a fourth fixed service:

1. Add `shard-4` to `docker-compose.yml` by copying a shard service, setting `SHARD_ID: shard-4`, `PORT: "8004"`, a health-check URL on port 8004, and a new `shard4_data:/data` volume. Keep it attached to `canvas-net` and do not publish its port.
2. Add `shard4_data:` under the Compose `volumes:` section.
3. Append `shard-4=http://shard-4:8004` to `SHARD_URLS` in `.env`.
4. Start it with `docker compose up -d --build shard-4 coordinator`.

The coordinator reads `SHARD_URLS` at request time, so the new service can appear in `/admin/shards` after it is healthy. Keep `SHARD_IDS` unchanged if you only want to check the new service's health. **Do not add shard-4 to `SHARD_IDS` while existing pixel data matters:** Stage 3A's modulo mapping would reassign many tiles, and migration is not implemented. A safe active routing change must wait for a later migration phase.

## Tests

On macOS or Linux, with Python 3.12 and Docker running, the full unit, Docker, and Chromium suite runs from one command:

```sh
./scripts/test-all.sh
```

The script installs test dependencies in `.venv`, starts a separate temporary Compose project, and removes its containers and volumes afterward. Port 8000 must be free; stop the normal canvas with `docker compose down` first. The first run downloads dependencies and Chromium.

Install dependencies and run the unit suite from the repository root:

```powershell
python -m pip install -r requirements.txt
python -m pytest -q tests -p no:cacheprovider
```

The ordinary suite contains mocked coordinator-health tests and local shard-app tests. Real-container integration tests are separate. Start Compose in another terminal, then run:

```powershell
$env:RUN_DOCKER_INTEGRATION = "1"
python -m pytest -q tests/integration -p no:cacheprovider
Remove-Item Env:RUN_DOCKER_INTEGRATION
```

The integration test checks actual Docker DNS health results and distinct mounted volume names. It creates two independent browser sessions, connects both to WebSockets, checks their shard-backed initial snapshots, routes a draw at `(25, 0)` to `tile-00-01` on shard-2, verifies the shard record and coordinator state, checks shard-1 rejects that tile, and confirms both sessions receive each other's draw updates. It restores any existing pixel records at its test coordinates and clears its session-owned writes. It also checks infrastructure-record isolation and persistence across `docker compose restart shard-1`.

Browser tests use Playwright Chromium and restart the coordinator and shard-2. Run them only against a disposable Compose project, with port 8000 free:

```sh
python -m pip install playwright==1.65.1
python -m playwright install chromium
COMPOSE_PROJECT_NAME=canvasbrowser docker compose up --build -d
COMPOSE_PROJECT_NAME=canvasbrowser RUN_BROWSER_INTEGRATION=1 python -m pytest -q tests/browser -p no:cacheprovider
COMPOSE_PROJECT_NAME=canvasbrowser docker compose down
```

## Local development without Docker

```powershell
python -m pip install -r requirements.txt
python -m uvicorn frontend.main:app --reload
```

Open <http://127.0.0.1:8000/>. Without Compose, `/admin/shards` attempts the default Docker service names and reports them unavailable.

## Troubleshooting

- **`docker` is not recognized:** Install Docker Desktop, enable its WSL2 backend, start it, and reopen PowerShell.
- **Docker daemon is unavailable:** Wait for Docker Desktop to report that its engine is running, then retry `docker compose up --build`.
- **Port 8000 is busy:** Stop the process using it or change the coordinator host mapping in Compose, for example `8001:8000`, then visit `http://localhost:8001/`.
- **A shard is unavailable:** Check `docker compose ps` and `docker compose logs shard-1` (or the affected shard). The coordinator remains available while the shard is down.
- **Shard health is unavailable after startup:** Allow a few seconds for health checks; inspect the shard logs and `/admin/shards`. Service-to-service URLs must use Compose service names, not host `localhost`.
- **Records disappear:** Confirm you used `docker compose down`, not `docker compose down -v`, and that the shard is mounting its own named volume at `/data`.
