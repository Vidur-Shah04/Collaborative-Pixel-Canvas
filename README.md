# Collaborative Pixel Canvas

A FastAPI collaborative canvas with a Phase 2 Docker execution environment. The browser still talks only to the coordinator. Three independent shard containers expose health and infrastructure-record APIs, each backed by a separate SQLite database and Docker volume.

For the student-oriented, file-by-file walkthrough, exact function explanations, and reproducible demonstrations, see [FOLLOW_ALONG.md](FOLLOW_ALONG.md).

**Phase 2 does not implement canvas tile routing, ownership epochs, migration, or recovery protocols.** The canvas and user sessions remain in coordinator memory. Shard records are only for verifying container identity, database isolation, and persistent volumes.

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

The coordinator reads `SHARD_URLS` at request time, so the new service appears in `/admin/shards` after it is healthy. Use a unique service name, shard ID, port, and volume for every shard.

## Tests

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

That integration test checks actual Docker DNS health results, distinct mounted volume names, record isolation, and record persistence across `docker compose restart shard-1`.

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
