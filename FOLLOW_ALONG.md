# Follow Along: Collaborative Pixel Canvas

This guide describes the repository as it exists today. It is a learning companion to the short [README](README.md), not a claim that every goal in the course proposal is already implemented.

## A. Project overview

The browser shows a 1000 by 1000 HTML canvas. A browser session can choose a colour and draw at a coordinate. Other browsers connected to the same coordinator see committed draw and clear messages over a WebSocket.

The project currently demonstrates several useful distributed-systems building blocks:

- **Identity and ownership:** the coordinator issues an opaque browser cookie and maps it to a server-side session ID. Every occupied coordinate stores its current colour and the session that last drew it.
- **Concurrent updates:** one `asyncio.Lock` serializes canvas reads, draws, clears, WebSocket registration, initial snapshots, and broadcasts inside the coordinator process.
- **Service separation:** Docker Compose describes a coordinator and three independently configured shard services, connected by a bridge network and DNS names.
- **Deterministic tile routing:** a shared routing module maps each pixel to a row-major tile and assigns each tile to one configured shard without Python's randomized `hash()`.
- **Shard-owned pixel records:** the coordinator sends each draw to its selected shard. That shard verifies tile ownership and writes the pixel's colour and session owner into SQLite.
- **Storage isolation:** each shard service has a distinct SQLite file in its own named Docker volume. The same DB also contains sample infrastructure records.

The browser talks to the coordinator only. It accepts drawing/Clear HTTP requests, routes pixels to shards, and sends WebSocket updates only after the assigned shard confirms a write. It also reads pixel state from all assigned shards. Session mappings and a coordinator pixel cache remain in memory. Docker source/configuration exists, but live container verification must be established on a machine with Docker.

Docker containers simulate distinct processes and service identities on one physical computer. A Compose network gives each service a DNS name, and named volumes keep each shard database across container restarts. This is a useful local distributed-systems lab, but it is not a simulation of separate physical computers or networks with independent failures.

## B. Prerequisites and setup

### Known versions and prerequisites

- Windows 11 development environment.
- Python 3.11 or later is the documented minimum; the original development machine used Python 3.12.4.
- Docker Desktop for Windows using Linux containers and its WSL 2 backend; Docker Compose v2 is used by the commands below. Versions are not pinned in the repository.
- A modern browser and VS Code are useful.
- Package versions in `requirements.txt` are not pinned. Install them into a virtual environment for local Python work.

For official setup details, see [Microsoft's WSL installation guide](https://learn.microsoft.com/en-us/windows/wsl/install) and [Docker Desktop's WSL 2 backend instructions](https://docs.docker.com/desktop/features/wsl/).

### Windows 11: WSL 2 and Docker Desktop

In an elevated PowerShell terminal if Windows asks for administrator permission:

```powershell
wsl --install
wsl --update
wsl --status
```

Restart Windows if prompted. Install and start Docker Desktop. In Docker Desktop, select **Settings → General → Use the WSL 2 based engine**, then **Apply**. If you also use a Linux distribution terminal, enable it under **Settings → Resources → WSL Integration**. Docker Desktop can also be used from PowerShell without installing a separate Linux distribution. Confirm the engine is available:

```powershell
docker version
docker compose version
```

The application images use Linux containers. If Docker commands are not found, install/start Docker Desktop and reopen the terminal. If the CLI exists but reports a daemon error, wait for Docker Desktop's engine to start.

### Python environment for tests or local coordinator work

Run these commands from the project root, the directory containing `requirements.txt`:

```powershell
py --version
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, invoke the virtual environment interpreter directly or use Command Prompt's `.venv\Scripts\activate.bat`. The dependency list currently includes FastAPI, Uvicorn, HTTPX, pytest, and pytest-asyncio.

### Start the Docker system

Still in the project root:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The coordinator is published on host port 8000:

- Canvas: <http://localhost:8000/>
- FastAPI docs: <http://localhost:8000/docs>
- Coordinator health: <http://localhost:8000/health>
- Live shard health overview: <http://localhost:8000/admin/shards>

The `.env` file sets `SHARD_URLS` and `SHARD_IDS`. The coordinator uses entries such as `shard-1=http://shard-1:8001`; Docker DNS resolves those service names inside `canvas-net`. `SHARD_IDS` defines the active route set. Shard ports are not published to Windows.

Useful lifecycle and inspection commands:

```powershell
docker compose ps
docker compose logs -f coordinator
docker compose logs -f shard-1
docker compose stop shard-2
docker compose start shard-2
docker compose restart shard-1
docker compose down
```

`docker compose down` keeps the three named data volumes. `docker compose down -v` deletes them and their records; reserve it for an intentional data reset. The test guide below does not delete volumes.

### Run tests

Local unit and API tests (the Docker integration test is skipped unless explicitly enabled):

```powershell
python -m pytest -q tests -p no:cacheprovider
```

Start the Compose services in one terminal. In another terminal, from the project root, run the real-container test:

```powershell
$env:RUN_DOCKER_INTEGRATION = "1"
python -m pytest -q tests/integration -p no:cacheprovider
Remove-Item Env:RUN_DOCKER_INTEGRATION
```

The integration test calls the host coordinator, runs HTTP requests inside actual shard containers with `docker compose exec`, inspects their mounted volume names, restarts shard-1, and checks that its record remains. It requires a running project. A skipped integration test is not evidence that Docker works.

## C. Architecture and data flow

```mermaid
flowchart LR
    Browser[Browser page and canvas] -->|GET page, POST draw, DELETE clear| Coordinator[Coordinator FastAPI :8000]
    Browser <-->|WebSocket /ws state and updates| Coordinator
    Coordinator -->|health and routed pixel HTTP| S1[shard-1 :8001]
    Coordinator -->|health and routed pixel HTTP| S2[shard-2 :8002]
    Coordinator -->|health and routed pixel HTTP| S3[shard-3 :8003]
    S1 --> DB1[(shard1_data: SQLite)]
    S2 --> DB2[(shard2_data: SQLite)]
    S3 --> DB3[(shard3_data: SQLite)]
```

### Browser to coordinator

1. `GET /` serves `frontend/index.html` and issues the `canvas_session` cookie if this browser has no registered session. The cookie is HTTP-only and SameSite=Lax; Secure is unset because the documented local service uses plain HTTP.
2. The browser opens `/ws` on the same host. The coordinator accepts only a token already present in its session registry.
3. The coordinator sends one initial `state` message. The page renders that message, then applies later `draw` and `clear` messages.
4. A pointer click posts `x`, `y`, and a HEX colour to `/draw`. The server gets the owner from the cookie, not from request JSON.
5. `DELETE /clear` removes only records currently owned by that session.

### Tile calculation and shard assignment

`shared/tiles.py` is copied into both images and imported by both coordinator and shards. It accepts only real integer coordinates (not booleans or fractional numbers) within the 1000×1000 grid. For pixel `(x,y)`, `tile_x=x//25`, `tile_y=y//25`, and `tile_index=tile_y*40+tile_x`. The ID format is `tile-{row:02d}-{column:02d}`: `(25,0)` is `tile-00-01`, and `(999,999)` is `tile-39-39`.

`SHARD_IDS` is sorted lexically, then `tile_index % number_of_shards` selects one owner. With three default shards, tile index 0 routes to shard-1, index 1 to shard-2, index 2 to shard-3, and index 3 returns to shard-1. All 1,600 tiles are assigned once; counts are 533, 533, and 534. Reordering `SHARD_IDS` has no effect. Changing the set does: modulo can remap many tiles, and there is no migration yet, so do not change active IDs while data matters.

### Pixel ownership and synchronization

The coordinator obtains a session owner from the HTTP-only cookie, never a browser-supplied `user_id`. A draw includes `x`, `y`, calculated `tile_id`, normalized colour, and that session owner in an internal request. The target shard recalculates the tile, verifies the ID and verifies that the routing algorithm selects itself. A wrong tile ID gets 422; a tile assigned elsewhere gets 409.

Each shard stores one row keyed by `(x,y)` with both colour and owner in one SQLite upsert. This preserves the most recent writer as current owner. The coordinator waits for the owning shard's committed response, checks its shard ID/tile/colour/owner receipt, then updates its in-memory cache and broadcasts. A failure or invalid receipt produces an error and is not broadcast.

`GET /state` and new WebSocket snapshots combine records from all active shards; state reads fail instead of returning a silently incomplete canvas when a shard is unavailable. `DELETE /clear` asks every routing shard to delete rows for the caller's server-managed session ID, then broadcasts the removed coordinates when all calls succeed. Each shard's delete is locally transactional, but there is no distributed transaction across shards: a failure mid-clear can leave partial deletion and returns an error without a broadcast. A later state read reloads the stored result.

One `asyncio.Lock` covers coordinator state reads, shard operations, cache updates, WebSocket registration/snapshots, and broadcasts. In a single coordinator process it orders a draw and a clear and prevents a snapshot from racing with a write. It is not a guarantee across multiple coordinator processes.

### Coordinator to shards and SQLite

Compose places all four services on `canvas-net`. The coordinator checks configured URLs such as `http://shard-1:8001/health`; each shard checks its own SQLite connection and returns its configured ID and service version. `/admin/shards` makes the probes concurrently with short timeouts and returns per-shard health, including unavailable nodes.

Each shard has `infrastructure_records` and `pixels` tables in its own `/data/shard.sqlite3`. `POST /pixels` validates the deterministic owner before upserting; `GET /pixels` returns that shard's pixel records; `DELETE /pixels?user_id=...` clears only that user on that shard. `shard1_data`, `shard2_data`, and `shard3_data` are separate Docker-managed named volumes mounted at `/data`. Pixel records are durable when those volumes work, but no operation history, epochs, snapshots, or migration state is present.

## D. File-by-file guide

| File | Responsibility and relationships |
|---|---|
| `frontend/main.py` | Coordinator app, HTTP routes, cookie sessions, current pixel ownership, WebSocket manager, and live shard health probes. Serves `index.html`; imports HTTPX for shard probes. |
| `frontend/index.html` | Browser UI, Canvas drawing, HTTP draw/clear requests, WebSocket state/update rendering. |
| `shared/tiles.py` | Shared strict coordinate validation, stable row-major tile IDs, canonical shard ID configuration, and deterministic owner selection. Both coordinator and shard import it. |
| `shared/__init__.py` | Marks `shared` as an importable package so both images can load the same routing code. |
| `shard/main.py` | Factory for the common shard FastAPI app, SQLite setup, infrastructure record APIs, deterministic per-pixel owner validation, and pixel read/write/clear APIs. Compose starts this same module three times. |
| `shard/__init__.py` | Marks `shard` as an importable Python package for Uvicorn and tests. |
| `docker/coordinator.Dockerfile` | Builds the coordinator image with Python dependencies and `frontend/`; starts Uvicorn on port 8000. |
| `docker/shard.Dockerfile` | Builds the common shard image and starts Uvicorn on the environment-provided internal port. |
| `docker-compose.yml` | Defines coordinator, three shard services, health checks, restart policies, private bridge network, published coordinator port, and separate named volumes. |
| `.env.example` | Template for `SHARD_URLS` (service addresses) and `SHARD_IDS` (active tile-routing set); copy to `.env`. |
| `.dockerignore` | Keeps local virtual environments, tests, caches, databases, and secrets out of image build contexts. |
| `.gitignore` | Excludes local environments, database files, cache output, and `.env` from normal version control. |
| `requirements.txt` | Runtime and test dependencies, including the synchronous WebSocket client used by the real-container integration test. |
| `pytest.ini` | Registers the `integration` pytest marker. |
| `tests/test_canvas.py` | Session, ownership/Clear, validation, WebSocket ordering, and lock interleaving regressions. |
| `tests/test_infrastructure.py` | Local shard API and mocked coordinator health/discovery tests. It does not start containers. |
| `tests/test_tiles.py` | Pure tests for coordinate boundaries, stable tile IDs, deterministic assignment, and shard balance. |
| `tests/integration/test_docker_services.py` | Opt-in test that uses a live Compose project and real shard containers. |
| `README.md` | Short setup, command, and troubleshooting entry point linking here. |
| `ARCHITECTURE.md` | Concise system boundary, network, health, storage, and Phase 3 scope description. |
| `decision.md` | Chronological architecture decision records, including proposals not yet implemented. |
| `document.pdf`, `collab pixel doc.pdf` | Course proposal/reference documents describing intended future distributed behavior. Proposal content is not proof of implementation. |
| `frontend/__pycache__/*`, `tests/__pycache__/*` | Generated Python bytecode artifacts. They are not source-of-truth files and should not be hand-edited. |
| `.gitattributes` | Requests Git's standard text-file line-ending normalization. |

## E. Function-by-function guide

### `frontend/main.py`

**`ConnectionManager.__init__(self)`** initializes `active_connections`, the in-process list of connected WebSocket objects. It returns `None` and does not touch the canvas. `manager` is the one coordinator instance.

**`ConnectionManager.disconnect(self, websocket)`** removes the given socket if present. It returns `None`; it is idempotent for an already-removed socket. `websocket_endpoint` calls it in `finally`, and `broadcast` calls it after a failed send. It does not change pixel state.

**`ConnectionManager.broadcast(self, message)`** sends a JSON event to a copy of `active_connections`. It returns `None`; a failed send is logged and that connection is removed. Draw/clear call it while holding `canvas_lock`, so messages follow mutation order in this process. A slow socket can hold up other operations because sends are sequential and no send timeout is configured.

**`session_for(token)`** accepts a cookie token or `None`; it returns `(token, user_id)`. A registered token reuses its existing ID. Any missing/unknown token causes a random URL-safe token and UUID session ID to be generated and saved in the server dictionary. It never trusts an ID in a request body. The registry is in memory, has no expiration/authentication, and resets on coordinator restart.

**`public_pixels()`** takes no arguments and returns a new coordinate-to-colour dictionary from the coordinator cache, omitting owner metadata. `GET /state` and the WebSocket snapshot call it after reloading the cache from shards while holding `canvas_lock`.

**`configured_shards()`** takes no arguments and returns `list[tuple[shard_id, base_url]]`, parsed from `SHARD_URLS`. If it is unset or blank, it uses the three Compose service-name defaults. Malformed entries raise `ValueError`. Both health discovery and internal URL lookup use it.

**`shard_urls_by_id()`** takes no arguments and returns a dictionary from shard ID to base URL. It checks that every active `SHARD_IDS` routing owner has a URL, while allowing extra health-only services. A mismatch raises `ValueError` before a draw can be sent to an ambiguous destination.

**`request_shard(shard_id, method, path, *, params=None, body=None)`** sends one bounded HTTPX request to the explicitly named shard URL. It returns a JSON object. HTTP rejections become `ShardRequestError` with 502; network, timeout, or invalid JSON failures become 503. Draw, Clear, and state loading call it. It never retries or selects a different shard.

**`load_pixels_from_shards()`** takes no arguments and concurrently requests `/pixels` from each active shard. It returns a coordinate-keyed dictionary containing internal color and owner fields. It is used by `/state` and WebSocket setup. Any request failure prevents an incomplete combined state from being returned.

**`inspect_shard(shard_id, url)`** asynchronously performs one HTTP GET to `{url}/health` with the configured connect/read/write/pool timeouts. It returns a health dictionary. Matching ID and `healthy` status produce a healthy result with DB/version fields; network/HTTP/JSON failures return `unavailable`, and a valid but mismatched identity/status returns `unhealthy`. It logs HTTP-level failures and does not raise ordinary shard outage errors to the caller.

**`coordinator_health()` — `GET /health`** takes no arguments and returns a small coordinator status object. It has no state mutation or shard dependency; Docker uses it for the coordinator's container health check.

**`shard_health_overview()` — `GET /admin/shards`** takes no arguments and returns `{"shards": [...]}`. It gathers `inspect_shard` calls concurrently for each configured target. Each shard's failures are isolated in its own result. Bad `SHARD_URLS` syntax is not caught here and causes a server error, so keep the environment value valid.

**`home(request)` — `GET /`** reads the request cookie, calls `session_for`, serves the adjacent `index.html`, and sets `canvas_session` with `httponly=True` and `samesite="lax"`. It returns a `FileResponse`; it creates a session only when needed. The cookie is not Secure in this local HTTP setup.

**`get_state(request)` route `GET /state` resolves/creates the browser session, acquires `canvas_lock`, reloads records from every active shard into the coordinator cache, then returns coordinate/color data plus the cookie. It returns HTTP 503 if shard state cannot be read completely. The browser gets its initial render from WebSocket instead.

**`draw(request, http_request)` route `POST /draw` validates strict integer coordinates and HEX color through `DrawRequest`, gets owner identity from the cookie, calls `tile_for_pixel` and `shard_for_tile`, then under `canvas_lock` sends `POST /pixels` to exactly that shard. It checks the commit receipt status, shard ID, tile ID, color, and owner. Only after the receipt matches does it update the local cache and broadcast. It returns status, coordinates, color, tile ID, shard ID, and cookie; shard failure returns an error without cache update or broadcast. The lock serializes this single coordinator but provides no operation IDs or cross-coordinator ordering.

**`clear_my_pixels(request)` route `DELETE /clear` gets owner from the session cookie and under `canvas_lock` sends a delete-by-user request to every active shard. Each shard removes only rows currently owned by that session. The coordinator updates its cache and broadcasts combined coordinates only when every request succeeds, then returns the removed count and cookie. A previous owner cannot remove an overwrite. A repeated clear returns zero when all shards respond. There is no atomic transaction across shards: a failure after one shard deletes can leave partial deletion, return an error, and suppress the event. This is a Stage 3A limitation.

**`websocket_endpoint(websocket)` route `WS /ws` reads and validates the registered cookie, accepts the socket, then under `canvas_lock` reloads pixels from every active shard and sends a full color-only snapshot before later operations can proceed. If shard reads fail, it closes with code 1011. It waits for client messages only to keep the socket open; draw commands use HTTP. Disconnects and errors are cleaned up in `finally`. This prevents snapshot/update races in the single coordinator process.

**`DrawRequest`** is the Pydantic input model used by `/draw`. Its three fields validate bounds and HEX syntax before the route mutates shared state. It has no custom methods or side effects.

### `shard/main.py`

**`RecordRequest`** validates infrastructure write JSON: a nonempty key up to 200 characters and a value up to 4000 characters. FastAPI returns a validation response for invalid values; the model itself does no I/O.

**`PixelWriteRequest`** validates internal routed pixel JSON. It requires strict in-bounds integers, a tile ID, six-digit HEX color, and nonempty owner string. It does not choose the owner; the coordinator sets it from its cookie session.

**`create_app(shard_id=None, port=None, database_path=None)`** builds and returns one FastAPI app. Optional arguments support isolated tests; otherwise identity, port, active shard IDs, DB path, and version come from environment/defaults. It defines DB helpers and route handlers, then attaches them to this app. Each Compose container uses the same code with its own identity, port, and volume. It imports the same `shared.tiles` assignment logic as the coordinator.

**Nested `connect()`** takes no arguments and returns a configured `sqlite3.Connection`. It creates the parent directory, opens only this app's database with a five-second timeout, uses row objects, and sets SQLite's busy timeout.

**Nested `database_connection()`** is a context manager with no parameters. It opens one connection, commits/rolls back through the SQLite context, and always closes in `finally`. Shard endpoints use it so requests do not leak file handles.

**Nested `initialize_database()`** creates `infrastructure_records`, `pixels`, and an owner lookup index if absent. Pixel rows have coordinate primary key and store tile ID, color, and owner together. It runs during application startup and does not delete existing records.

**Nested `lifespan(app)`** initializes the DB before serving and then yields control to FastAPI. It has no response body; a DB initialization failure prevents the service from starting healthy.

**`health()` — shard `GET /health`** opens the configured database and executes `SELECT 1`. It returns shard ID, healthy status, DB connection status, and service version; SQLite errors become HTTP 503. It does not write a test record.

**`info()` — shard `GET /info`** returns identity, configured port, DB path, service label, and version. It does not open or mutate the DB.

**`write_record(record)` — shard `POST /records`** performs a parameterized SQLite insert with conflict-update semantics inside a transaction, then reads and returns the stored row. The side effect is isolated to this shard's database. Reusing a key updates its value.

**`list_records()` — shard `GET /records`** returns all stored records ordered by key. It reads only and returns `{"records": [...]}`.

**`get_record(record_key)` — shard `GET /records/{record_key}`** parameterizes the lookup. It returns the row or raises HTTP 404 if absent; it never creates data.

**`write_pixel(pixel)` route `POST /pixels`** converts the pixel coordinates to a `TileAddress`, rejects a mismatched tile ID with 422, and calculates the expected owner shard. If this service is not the owner it returns 409 without writing. Otherwise it performs one parameterized SQLite upsert that replaces the entire coordinate row (color and owner together), commits, and returns a commit receipt. It relies on the coordinator to provide the session owner and does not implement operation IDs or epochs.

**`list_pixels()` route `GET /pixels`** reads this shard's rows ordered by y then x and returns them with owner metadata for coordinator aggregation. It does not query another shard.

**`clear_owned_pixels(user_id)` route `DELETE /pixels?user_id=...`** selects the caller's coordinates and deletes rows for that user in one local SQLite transaction, then returns removed coordinates. It only guarantees atomicity on this one database, not across all shards.

The module-level **`app = create_app()`** is the object Uvicorn imports in each shard container. The factory's default local DB path is under `shard/data`; Compose overrides it to `/data/shard.sqlite3` inside each named volume.

### `shared/tiles.py`

**`TileAddress`** is an immutable dataclass containing tile column (`tile_x`), tile row (`tile_y`), row-major ordinal (`tile_index`), and stable ID (`tile_id`). It distinguishes pixel coordinates from tile coordinates.

**`tile_for_pixel(x, y)`** accepts two values and returns a `TileAddress`. It rejects values whose exact Python type is not `int` (including booleans), and coordinates outside the 1000?1000 grid. It divides by 25 and computes `tile_index = tile_y * 40 + tile_x`; the ID is `tile-{row:02d}-{column:02d}`. It is pure and has no I/O.

**`configured_shard_ids(value=None)`** accepts an optional comma-separated string; otherwise it reads `SHARD_IDS` or uses the default three IDs. It rejects empty entries and duplicates, sorts the IDs, and returns a tuple. Sorting means deployment configuration order cannot change routing. It does not read shard health.

**`shard_for_tile(tile, shard_ids=None)`** accepts one `TileAddress` and optionally a shard tuple, then returns exactly one ID at `tile_index % len(sorted IDs)`. It raises `ValueError` for an explicitly empty list. The function uses integer arithmetic rather than Python's randomized hash. It is stable only while the active shard set is unchanged; adding/removing IDs requires migration, which is not implemented.

### `frontend/index.html`

**`paint(x, y, color)`** fills a small visible 3 by 3 rectangle at the logical coordinate. It returns nothing and only changes the local canvas pixels.

The anonymous **WebSocket `open` callback** updates the status text after the connection opens; it receives the browser event and does not change server state.

The **WebSocket `message` callback** parses event JSON. For `state`, it paints the canvas white and redraws each returned coordinate. For `draw`, it calls `paint` with the event's coordinate and colour. For `clear`, it paints the listed coordinates white. Its parameter is the browser message event; its side effect is UI rendering only.

The **WebSocket `close` callback** changes status to disconnected. The **`error` callback** reports a connection error. Both receive browser events and do not retry automatically.

The **canvas `pointerdown` callback** maps pointer position within the displayed canvas to integer coordinates in the 1000×1000 logical grid, reads the selected colour, and posts JSON to `/draw`. It updates status on success and reports fetch/HTTP failure. The server remains authoritative; the callback does not locally paint the requested pixel.

The **Clear button `click` callback** sends `DELETE /clear`, checks HTTP success, reads the `cleared` count, and updates status. It doesn't blank the whole canvas itself; the server broadcast carries exact removed coordinates to all connected pages.

The constants and event handlers together ensure the browser connects to the same host as the page, selecting `ws:` or `wss:` based on the page scheme.

### `tests/test_canvas.py`

Each `test_*()` function below takes no explicit arguments except where a fixture is named, returns no application value (`None`), and establishes behavior through assertions. FastAPI TestClient calls may mutate module-level coordinator state; `reset_memory_state()` clears it and replaces `request_shard` with a per-test fake shard store. The fake uses the real tile routing function but no network or SQLite. The inner `scenario()` coroutines take no arguments and return no value; they coordinate deterministic async events.

**`reset_memory_state(monkeypatch)`** is an autouse pytest fixture. Before each test, it clears coordinator pixels, sessions, connections, and replaces the async lock. It creates a per-test dictionary for each configured shard and monkeypatches the network request helper to use that fake. It yields to the test; pytest restores the patch afterward.

Its nested **`fake_request_shard(shard_id, method, path, *, params=None, body=None)`** takes the coordinator's exact internal request arguments. It simulates POST upsert, GET listing, and DELETE by owner, returning JSON-shaped dictionaries. For a POST it checks tile ID and expected owner using the real routing function. Unexpected methods/paths raise `AssertionError`. It returns no HTTP response and performs no network or SQLite I/O.

**`test_different_sessions_draw_and_clear_only_their_current_pixels()`** uses separate TestClients as users A and B. It checks the requested `(12,12)` red-to-green overwrite, A's clear preserving B's green pixel, then B's clear removing it. It returns no value; failures identify ownership regressions.

**`test_coordinator_routes_each_draw_to_the_deterministic_tile_owner()`** posts pixels in tile 0 and tile 1 and checks the response tile IDs and selected shard IDs. The fixture's fake shard store verifies the same expected owner before accepting.

**`test_unavailable_owner_shard_is_not_replaced_by_another_shard(monkeypatch)`** replaces `request_shard` with a failure fake and checks a tile assigned to shard-2 returns 503, attempts only shard-2, and does not modify the cache.

**`test_clear_removes_unoverwritten_pixels_and_repeated_clear_is_a_noop()`** draws two coordinates in one session, confirms one clear removes both, and confirms a second clear returns zero. It covers ordinary ownership deletion and idempotent repeated requests.

**`test_clear_only_removes_its_owner_when_users_have_distinct_pixels()`** gives Alice two pixels and Bob one, then verifies Alice's clear leaves Bob's pixel and Bob can clear it.

**`test_draw_validates_canvas_coordinates_and_hex_color()`** submits out-of-range, fractional, boolean, and invalid HEX values. It asserts HTTP 422 and ensures malformed inputs cannot enter the canvas.

**`test_session_cookie_is_secure_for_local_http_and_survives_refresh()`** verifies the HTTP-only and SameSite=Lax attributes, the deliberate absence of Secure for local HTTP, and reuse of the registered cookie/session after refresh.

**`test_two_browser_sessions_have_distinct_server_generated_ids()`** creates two clients and checks their mapped session IDs differ and are not the raw cookie tokens. It then submits a forged `user_id` in Alice's draw body and verifies the pixel is still owned by Alice's cookie session.

**`test_websocket_requires_a_server_registered_session()`** attempts a socket without first creating a session. It expects a disconnect rather than an accepted unauthenticated socket.

**`test_websocket_snapshot_and_broadcast_order_across_clear_and_draw()`** draws before connect, verifies that draw is in the initial snapshot, then checks clear and a later draw arrive in order.

**`test_disconnected_websocket_is_removed_from_connection_manager()`** opens and closes a socket and asserts the connection list is empty afterward.

**`test_draw_waits_until_websocket_initial_snapshot_is_sent()`** contains an inner `scenario()` coroutine and a fake socket that pauses the initial send. It starts a draw while the snapshot lock is held, verifies the draw waits, releases the snapshot, and checks state then draw event order and cleanup. The outer test runs the coroutine with `asyncio.run`.

Its fake **`PausingWebSocket.accept()`** takes no arguments beyond `self`, returns `None`, and stands in for ASGI's handshake without network I/O. **`send_json(message)`** sets an event and waits only when sending the initial `state`; then it records the JSON message. **`receive_text()`** waits until the scenario allows disconnect and raises `WebSocketDisconnect`, exercising endpoint cleanup. These are test doubles for ASGI methods; they do not open a network connection.

**`test_concurrent_clear_waits_for_inflight_draw_broadcast_and_leaves_consistent_state()`** contains an inner coroutine and pausing fake WebSocket. It stalls a draw broadcast while the canvas lock is held, starts a clear, and verifies clear waits. After release, draw then clear are broadcast, and the final pixel dictionary is empty. This establishes serialization for this in-process interleaving; it is not a load or multi-process stress test.

Its fake socket's **`send_json(message)`** appends each event and pauses after the draw event until the test releases its gate. It lets the test hold the actual application lock across the draw broadcast so a clear request can be shown waiting behind it.

**`test_websocket_receives_state_and_committed_draw_update()`** checks the empty initial state and normalized draw event on a live TestClient WebSocket.

### `tests/test_tiles.py`

**`test_pixel_to_tile_examples(x, y, tile_x, tile_y, tile_index, tile_id)`** is parameterized with the six requested boundary examples. It checks the full conversion tuple; each parameter set returns no value.

**`test_pixel_to_tile_rejects_invalid_or_ambiguous_coordinates(x, y)`** is parameterized over negative, out-of-range, fractional, and boolean coordinates, expecting `ValueError` for each.

**`test_every_tile_maps_deterministically_to_exactly_one_balanced_shard()`** iterates the full 40×40 tile grid. It verifies stable IDs are unique, mapping ignores the order of a shard-ID tuple, every owner is in the configured set, and assignment counts are 533/533/534.

**`test_shard_id_list_rejects_duplicates_and_empty_entries()`** verifies malformed active routing lists raise `ValueError` rather than creating ambiguous assignments.

### `tests/test_infrastructure.py`

The test functions return no value; they use temporary SQLite files or a TestClient. The two tests with a `monkeypatch` parameter use pytest's fixture to override environment/probe behavior and restore it after the test.

**`test_shard_health_reports_identity_database_and_version()`** builds an app against a temporary test DB, starts its lifespan with TestClient, and checks health and configured port. It removes its test DB afterward.

**`test_shard_records_are_transactional_and_databases_are_isolated()`** starts two factory-created shard apps with distinct DB paths, writes a key to shard one, reads it there, and expects 404 from shard two. It cleans both DB files.

**`test_shard_rejects_pixels_assigned_to_a_different_shard()`** checks that shard-1 rejects a wrong tile ID with 422 and a valid tile assigned to shard-2 with 409, leaving its pixel table empty.

**`test_shard_commits_and_clears_pixels_for_tiles_it_owns()`** writes tile-00-01 through shard-2, overwrites Alice's row with Bob's color/owner, confirms Alice's delete finds nothing, then confirms Bob's delete removes the coordinate. This uses local TestClient and SQLite, not a container.

**`test_coordinator_reports_live_shard_checks_and_unavailable_shards(monkeypatch)`** sets three service URLs and replaces `inspect_shard` with an async fake for this unit test. It verifies one unavailable result does not fail `/admin/shards`. It does **not** demonstrate Docker DNS.

Its nested **`fake_inspect(shard_id, url)`** returns a healthy result for shard one and three and a timeout-style unavailable result for shard two. It accepts the same identifiers as the production probe and has no network effects.

**`test_coordinator_shard_registry_can_include_a_configured_fourth_shard(monkeypatch)`** adds a fourth URL to the environment and confirms parsing returns the new service target. It does not create the container.

### `tests/integration/test_docker_services.py`

Module setup skips the test unless `RUN_DOCKER_INTEGRATION=1` and the Docker CLI is present. The test assumes the Compose project is already running.

**`request_json(url, method="GET", payload=None)`** issues a standard-library HTTP request and returns `(status_code, decoded_json)`. It catches HTTPError so the expected 404 isolation result can be asserted rather than hidden.

**`compose(*args, check=True)`** runs `docker compose` from the repository root and returns a captured `CompletedProcess`. It raises on failed commands by default.

**`call_container_shard(shard, port, path, method="GET", payload=None)`** builds a small Python urllib script and runs it with `docker compose exec -T` inside the requested shard container. It is used because shard ports are private and not published to the host.

**`browser_json(browser, url, method="GET", payload=None)`** sends an HTTP request through one cookie-preserving browser opener and returns its status and JSON body, including error responses.

**`post_container_shard_status(shard, port, path, payload)`** posts a pixel record from inside a shard container and returns HTTP status plus JSON. It lets the real-container test assert a 409 from a non-owner shard without treating the expected rejection as a subprocess failure.

**`open_browser_session()`** creates a fresh cookie jar, requests the coordinator page to receive a session cookie, and returns both the browser opener and cookie value. The integration test calls it twice to keep the two browser sessions independent.

**`websocket_url()`** converts the configured HTTP coordinator URL to its WebSocket equivalent for live browser connections.

**`volume_name(shard)`** gets the running container ID and uses `docker inspect` to return the Docker volume mounted at `/data`.

**`test_compose_shards_use_isolated_volumes_and_keep_records_after_restart()`** checks the live coordinator health overview, expected unique shard IDs/service URLs, and three different named volumes. It creates two independent cookie sessions and connects both to real coordinator WebSockets before drawing; each initial snapshot must match `/state`. Session A draws `(25,0)`, and the test checks `tile-00-01`/shard-2, the complete stored row's coordinate, tile ID, and color, the refreshed coordinator state, and a 409 plus no row when the same valid tile record is sent directly to shard-1. Session B then draws another pixel in the same tile; both sockets must receive each draw event, which verifies each browser observes the other's operation. Owner-scoped cleanup removes the new test sessions' rows, and any pre-existing rows at the two test coordinates are restored. The test also writes a random infrastructure record into shard-1, confirms shard-2 cannot read it, restarts shard-1, waits for health to return, and verifies the record remains. The test uses the `websockets` client supplied by `uvicorn[standard]` and is opt-in with `RUN_DOCKER_INTEGRATION=1`.

### `docker/`, `docker-compose.yml`, and configuration

`coordinator.Dockerfile` copies `frontend/` and `shared/`, installs `requirements.txt`, and starts `frontend.main:app` on 8000. `shard.Dockerfile` copies `shard/` and the same `shared/` code, then starts it on the environment port. The Compose health-check commands use Python's standard-library urllib; no curl package is required. Compose's only host port mapping is `8000:8000` for the coordinator.

`.env.example` supplies `SHARD_URLS` and `SHARD_IDS`; the latter must match across coordinator and shards and defines active ownership. `SHARD_ID`, `PORT`, and `SHARD_DB_PATH` differ per shard service. The dedicated `canvas-net` network supplies DNS names. The top-level volume names are distinct. `requirements.txt` serves both images and local tests, so test packages are also installed in the images in this version.

## F. Reproducible demonstrations

### Two users, overwrite, and Clear

1. Open <http://localhost:8000/> in a normal browser window and a private/incognito window. Separate windows in the same browser profile share cookies, so use distinct profiles for distinct sessions.
2. In the first window draw at `(12,12)` in red. In the second window draw at the same location in green. Both pages should show green after the WebSocket event.
3. Click **Clear my pixels** in the first window. Its response says it cleared zero owned pixels if that was Alice's only pixel; `(12,12)` stays green.
4. Click **Clear my pixels** in the second window. The green pixel disappears from both pages.

The red-to-green behavior is automated in `test_different_sessions_draw_and_clear_only_their_current_pixels`.

### Verify tile routing

The pure routing examples can be run without Docker:

```powershell
python -m pytest -q tests/test_tiles.py -p no:cacheprovider
```

With Compose running, draw through the browser-facing coordinator using one cookie session:

```powershell
$webSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-WebRequest http://localhost:8000/ -WebSession $webSession | Out-Null
$drawBody = @{ x = 25; y = 0; color = "#abcdef" } | ConvertTo-Json
Invoke-RestMethod http://localhost:8000/draw -Method Post -ContentType "application/json" -Body $drawBody -WebSession $webSession
```

Expected response includes `tile_id: tile-00-01` and `shard_id: shard-2`. Confirm from inside the shard (its port is private to Docker):

```powershell
docker compose exec shard-2 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8002/pixels').read().decode())"
```

The row should show `(25,0)`, `tile-00-01`, `#ABCDEF`, and the session owner. This live demonstration requires Docker; the opt-in integration test performs the same route assertion automatically.

### Shard discovery and health

With Compose running, use a browser or PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/admin/shards | ConvertTo-Json -Depth 5
docker compose ps
```

Expect one `healthy` entry per configured shard, with IDs `shard-1`, `shard-2`, `shard-3`. Stop one shard with `docker compose stop shard-2`; the coordinator should still answer and report shard two unavailable. Restart it with `docker compose start shard-2` and wait for health to return.

### Verify storage isolation and restart persistence

Use the PowerShell write/restart/read commands in the [README verification section](README.md#verify-health-and-persistence), then run the opt-in integration test in section B. Each record is written to the shard's own database. The integration test also asserts the Docker volume names differ.

## G. Troubleshooting

- **Docker engine not running:** open Docker Desktop, wait for the engine, then check `docker version` and `docker compose version`.
- **WSL problem:** use `wsl --status`, `wsl --update`, and `wsl --list --verbose`; restart if the install/update prompts for it. Confirm Docker Desktop has the WSL 2 engine enabled. The [Microsoft guide](https://learn.microsoft.com/en-us/windows/wsl/install) and [Docker WSL guide](https://docs.docker.com/desktop/features/wsl/) cover current setup steps.
- **Port 8000 conflict:** change the host side of Compose mapping from `8000:8000`, for example to `8001:8000`, and browse `http://localhost:8001/`.
- **Container unhealthy:** inspect `docker compose ps` and `docker compose logs <service>`. A shard DB permission/path error prevents shard health from succeeding.
- **Shard discovery fails:** inspect `.env` and confirm each entry is `id=http://compose-service-name:port`. Inter-container URLs use Compose DNS names, not host localhost. `/admin/shards` is a live probe, not a static inventory.
- **Python dependency errors:** activate `.venv` and rerun `python -m pip install -r requirements.txt`.
- **Docker integration test fails:** make sure Compose is running, the coordinator is on the configured `COORDINATOR_URL` (default `http://127.0.0.1:8000`), and each shard is healthy. The test restarts shard-1 but does not remove volumes.
- **Data missing after stopping:** `docker compose down` preserves volumes; `docker compose down -v` deletes them. Check the command used before assuming a recovery defect.

## H. Implemented versus future work

| Capability | Current state |
|---|---|
| Shared browser canvas and WebSocket draw/clear updates | Implemented through the coordinator; draws broadcast after a shard commit. Local API tests use fake shard stores. |
| Server-issued HTTP-only session cookie | Implemented; session map is in coordinator memory, not durable authentication. |
| Current pixel owner and owner-specific clear | Implemented in shard SQLite rows; overwrite ownership and concurrent draw/clear ordering are covered by local tests. Multi-shard Clear is not atomic if a shard fails mid-request. |
| Coordinator and three independently configured shard containers | Compose/Dockerfiles are written. Actual startup has not been verified in this environment when Docker is unavailable. |
| Shard health/info, infrastructure records, and routed pixel API | Implemented in source and covered by local app tests. Live Docker behavior is covered by an opt-in test that must be run against containers. |
| Separate named volumes and persistence across restart | Configured; persistence is not considered proven until the real-container test passes. |
| Tile mapping/routing and only-assigned-tile storage | Stage 3A implemented; deterministic modulo routing and shard-side ownership checks are locally tested. Live Docker routing remains unverified. |
| Mutable ownership records, epochs, stale-epoch rejection, per-tile positions, operation IDs/deduplication | Planned for later stages; not implemented. |
| Live tile migration, migration recovery, shard crash recovery | Planned; not implemented. |
| Undo, atomic multi-shard clear, snapshots, hashes, timelapse | Planned; not implemented. Current clear fan-out can partially complete if a shard fails. |
| Multiple coordinator replicas or cross-process WebSocket broadcast | Not implemented. The coordinator is a single point of failure. |

## Documentation maintenance rules for future phases

For every future feature change:

1. Update `FOLLOW_ALONG.md` with affected files and functions.
2. Update function explanations if a signature, behavior, parameter, return value, or side effect changes.
3. Update `ARCHITECTURE.md` when service boundaries, data flow, storage, or system behavior changes.
4. Append an ADR to `decision.md` for each significant architecture decision. Do not silently rewrite the decision history.
5. Update `README.md` if setup, commands, endpoints, or user-visible behavior changes.
6. Add or adjust tests and explain exactly what each test establishes.
7. Clearly label features as implemented, tested, partial, or planned.
8. Never describe behavior the code does not implement.
9. Preserve old decisions; mark them superseded or deprecated rather than deleting them.
10. Explain concepts in plain language suitable for a student learning distributed systems.
