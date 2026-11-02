# Architecture Decision Records

**Project:** Collaborative Pixel Canvas
**Log status:** Phase 2 source/configuration exists; Docker runtime proof is pending.
**Last reviewed:** 2026-11-03

Records with unknown dates are kept in the order they appeared in the former decision log. They precede the decisions recorded as implemented on 2026-11-02. The original notes did not preserve alternatives, so alternatives below are retrospective comparisons, not quotations from the original deliberation. Status is one of **accepted**, **proposed**, **superseded**, or **deprecated**.

## Historic decisions and proposals (date unknown)

### ADR-001 — Develop locally before relying on RCE

- **Date:** Unknown; present in the original decision log.
- **Status:** accepted
- **Context:** The course proposal permits a standard development machine and mentions the university Remote Computing Environment (RCE).
- **Decision:** Use local development as the baseline; RCE remains optional.
- **Alternatives considered:** Require RCE from the beginning. This would make the setup dependent on remote access.
- **Rationale:** Local development is accessible and matches the proposal's stated baseline.
- **Consequences/trade-offs:** Docker later simulates several service processes locally, but not several physical machines.
- **References:** `README.md`, `docker-compose.yml`.

### ADR-002 — Python/FastAPI backend and browser Canvas frontend

- **Date:** Unknown; present in the original decision log.
- **Status:** accepted
- **Context:** The project needs an HTTP/WebSocket backend and a small browser drawing interface.
- **Decision:** Use Python, FastAPI/Uvicorn, HTML, CSS, JavaScript, and the HTML Canvas API.
- **Alternatives considered:** A different web framework or a larger client-side framework. No formal historic comparison was recorded; the selected stack aligns with the course proposal and keeps the UI small.
- **Rationale:** FastAPI supports the required HTTP and WebSocket routes; Canvas provides direct pixel rendering.
- **Consequences/trade-offs:** The UI and coordinator are easy to run together, but the coordinator is still a single point of failure.
- **References:** `frontend/main.py`, `frontend/index.html`, `requirements.txt`.

### ADR-003 — HTTP actions and WebSocket live updates

- **Date:** Unknown; present in the original decision log.
- **Status:** accepted
- **Context:** Browsers need to send draw/clear actions and see other sessions' updates.
- **Decision:** Use HTTP for page/state/draw/clear and WebSockets for initial canvas state and subsequent draw/clear events.
- **Alternatives considered:** Polling with HTTP or sending all commands over the socket. No formal historic comparison was recorded; separate request routes are easy to inspect while the socket provides server push.
- **Rationale:** This makes the browser-to-coordinator flow simple to demonstrate.
- **Consequences/trade-offs:** WebSocket fan-out is only within this one coordinator process.
- **References:** Routes in `frontend/main.py`; callbacks in `frontend/index.html`.

### ADR-004 — Keep initial canvas and session state in coordinator memory

- **Date:** Unknown; present in the original decision log.
- **Status:** accepted
- **Context:** The first prototype needed to validate drawing and collaboration before persistence work.
- **Decision:** Store current pixels, sessions, and active sockets in process memory.
- **Alternatives considered:** Add durable canvas persistence immediately. The original sequence deferred that work until the browser flow was working.
- **Rationale:** It keeps the prototype understandable and quick to exercise.
- **Consequences/trade-offs:** A coordinator restart loses canvas pixels and sessions. Phase 2 shard SQLite stores only infrastructure test records and does not alter this behavior.
- **References:** Globals `pixels`, `sessions`, and `manager` in `frontend/main.py`; persistence proposal in ADR-009.

### ADR-005 — Target canvas and tile dimensions

- **Date:** Unknown; specified in proposal and former decision log.
- **Status:** proposed
- **Context:** Future tile ownership needs a fixed coordinate grid.
- **Decision:** Target a 1000×1000 canvas and 25×25 tiles, giving a 40×40 grid of 1,600 tiles.
- **Alternatives considered:** Other grid and tile sizes; the source proposal did not record a comparison.
- **Rationale:** Preserve the course proposal's dimensions and use an even tile division.
- **Consequences/trade-offs:** The browser currently uses a 1000×1000 canvas, but tile mapping and per-tile storage are not implemented.
- **References:** `frontend/index.html`; `document.pdf`; `collab pixel doc.pdf`.

### ADR-006 — Use separate shard processes

- **Date:** Unknown; planned in the former decision log and proposal.
- **Status:** proposed
- **Context:** Logical shards inside one process would not demonstrate independent processes or storage.
- **Decision:** Run the same shard application as separate services with distinct identities, ports, and storage.
- **Alternatives considered:** Keep all shards as Python objects in one process, or require separate physical computers. The first does not test process isolation; the second adds setup demands.
- **Rationale:** Separate processes provide a local path to testing service boundaries.
- **Consequences/trade-offs:** The Compose implementation is now recorded in ADR-013; containers still share one physical host. Actual Docker startup remains unverified here.
- **References:** `shard/main.py`, `docker-compose.yml`, `docker/shard.Dockerfile`.

### ADR-007 — Plan operation IDs, per-tile positions, and ownership epochs

- **Date:** Unknown; specified in proposal and former decision log.
- **Status:** proposed
- **Context:** Concurrent operations, retries, and ownership changes need deterministic replay and stale-writer rejection.
- **Decision:** Future drawing operations should use unique operation IDs, monotonically increasing per-tile positions, and epochs that advance when tile ownership changes.
- **Alternatives considered:** Unordered cross-node writes or claiming exactly-once network delivery. The proposal instead calls for retry-safe idempotent processing and explicit epochs.
- **Rationale:** The fields make ordering and duplicate handling explicit.
- **Consequences/trade-offs:** These fields and guarantees do not exist in current code; do not infer them from Docker service separation.
- **References:** Proposal PDFs; future work only.

### ADR-008 — Plan live migration and interrupted-migration recovery

- **Date:** Unknown; specified in proposal and former decision log.
- **Status:** proposed
- **Context:** Rebalancing must not lose writes while users draw or while a source/destination fails.
- **Decision:** The future migration protocol should persist progress, transfer state and log position, account for writes during transfer, and hand off ownership only when the destination is ready.
- **Alternatives considered:** Stop drawing during migration or switch ownership before destination readiness. Those choices either interrupt use or risk losing updates.
- **Rationale:** Live migration and crash recovery are central project goals.
- **Consequences/trade-offs:** No migration manager, protocol, or recovery mechanism exists yet.
- **References:** Proposal PDFs; future work only.

### ADR-009 — Plan durable operation logs, snapshots, and replay

- **Date:** Unknown; specified in proposal and former decision log.
- **Status:** proposed
- **Context:** In-memory canvas state is lost on restart, and recovery needs a repeatable source of truth.
- **Decision:** The proposed canvas persistence design uses SQLite operation history and snapshots, then replays later operations.
- **Alternatives considered:** Save only a flattened image or keep only in-memory state. An image loses operation history; memory does not survive restart.
- **Rationale:** Logs and snapshots can restore current state and retain a history for verification.
- **Consequences/trade-offs:** The Phase 2 shard sample-record table is not an operation log and does not persist the canvas.
- **References:** Proposal PDFs; `shard/main.py` is infrastructure-only.

### ADR-010 — Plan undo, history outputs, and distributed correctness tests

- **Date:** Unknown; specified in proposal.
- **Status:** proposed
- **Context:** The course asks for safe user operations, reproducible failure cases, and ways to check recovered results.
- **Decision:** Plan eligible user-aware undo; deterministic tests for concurrency, duplicate IDs, stale epochs, restart, and source/destination failures; and later hash/timelapse comparisons after replay.
- **Alternatives considered:** Unconditional undo or whole-canvas clear can erase another user's later work. Testing only successful requests would miss crash and retry behavior. Detailed original alternatives were not recorded.
- **Rationale:** Operations should respect ownership and later changes, and recovery claims need comparison to expected history.
- **Consequences/trade-offs:** Current-owner Clear exists, but undo, durable clear history, the distributed failure tests, hashes, and timelapse do not.
- **References:** Proposal PDFs; current Clear routes in `frontend/main.py`.

## Implemented decisions recorded 2026-11-02

### ADR-011 — Server-issued session and current pixel ownership

- **Date:** 2026-11-02 (recorded in the Phase 1 decision log).
- **Status:** accepted
- **Context:** Clear must distinguish each browser session, and request bodies must not choose a pixel owner.
- **Decision:** Issue an opaque HTTP-only `canvas_session` cookie mapped to a server-generated UUID. Store each occupied pixel as one `{color, owner}` record; overwrite replaces both, and clear deletes only records currently owned by that session.
- **Alternatives considered:** Trust a client-supplied `user_id`, identify users only by WebSocket connection, or keep separate color and owner dictionaries. These either trust the client, fail to bind HTTP actions to the same session, or risk mismatched maps.
- **Rationale:** HTTP actions and WebSocket admission share the same server-validated session. One value keeps current color and owner together.
- **Consequences/trade-offs:** Session IDs are not human authentication and the map is lost at coordinator restart. Cookie `Secure` is unset for local plain HTTP; a TLS deployment should enable it.
- **References:** `frontend/main.py` `session_for`, `DrawRequest`, `draw`, `clear_my_pixels`, `websocket_endpoint`; ownership tests in `tests/test_canvas.py`.

### ADR-012 — Serialize state, broadcasts, and initial snapshots

- **Date:** 2026-11-02 (Phase 1 correctness audit).
- **Status:** accepted
- **Context:** A WebSocket could otherwise receive a mutation before an older initial snapshot; concurrent clear/draw could also display out of order.
- **Decision:** Use one `asyncio.Lock` for state reads, mutations plus broadcasts, and WebSocket registration plus initial snapshot.
- **Alternatives considered:** Unsynchronized snapshots, client-side sequence reconciliation, or a durable event stream. The latter two require protocol/storage work not in the prototype.
- **Rationale:** One in-process critical section gives a simple linear order and consistent color/owner changes.
- **Consequences/trade-offs:** A slow socket can delay other requests because sends occur under the lock. This is not cross-process ordering or distributed linearizability.
- **References:** `frontend/main.py` `canvas_lock`, `get_state`, `draw`, `clear_my_pixels`, `websocket_endpoint`; ordering tests in `tests/test_canvas.py`.

### ADR-013 — Docker Compose services and bridge-network discovery

- **Date:** 2026-11-02 (Phase 2 configuration recorded in project notes).
- **Status:** accepted
- **Context:** The project needs separate local service processes and a browser-facing coordinator without requiring multiple physical computers.
- **Decision:** Define one coordinator and three copies of the shard application as independent Compose services on a dedicated bridge. Publish only coordinator port 8000. Use service DNS and configurable `SHARD_URLS` for health probes.
- **Alternatives considered:** Logical shards inside the coordinator or manually managed multiple host processes. A logical shard doesn't test isolation; manual process startup is less repeatable.
- **Rationale:** Compose gives named services, internal DNS, health checks, and one reproducible start command.
- **Consequences/trade-offs:** Containers share a host failure domain. The config and images exist, but Docker CLI/engine is absent in the current environment, so live startup and DNS are unverified.
- **References:** `docker-compose.yml`, `docker/coordinator.Dockerfile`, `docker/shard.Dockerfile`, `.env.example`, `frontend/main.py`.

### ADR-014 — One SQLite file and named volume per shard

- **Date:** 2026-11-02 (Phase 2 configuration recorded in project notes).
- **Status:** accepted
- **Context:** Infrastructure tests need persistent records and must show that shard databases are isolated.
- **Decision:** Mount distinct named volumes (`shard1_data`, `shard2_data`, `shard3_data`) at `/data`; use `/data/shard.sqlite3` within each service. Use parameterized SQLite sample-record endpoints for verification.
- **Alternatives considered:** Shared SQLite storage or ephemeral container files. Shared SQLite hides isolation and is not appropriate as a multi-container shared file; ephemeral data vanishes on container recreation.
- **Rationale:** Separate Docker volumes and DB files give each shard independent persistent state.
- **Consequences/trade-offs:** Normal `docker compose down` keeps data; `down -v` erases it. Actual volume isolation and restart durability still need the live integration test.
- **References:** `docker-compose.yml`, `shard/main.py`, `tests/integration/test_docker_services.py`.

### ADR-015 — Live shard health probes and graceful partial status

- **Date:** 2026-11-02 (Phase 2 configuration recorded in project notes).
- **Status:** accepted
- **Context:** A dashboard must report actual shard reachability and remain useful if one shard is down.
- **Decision:** Probe each configured `/health` endpoint concurrently with bounded HTTPX timeouts; return per-service healthy, unhealthy, or unavailable status from `/admin/shards`.
- **Alternatives considered:** Return hardcoded healthy data, use coordinator localhost for inter-container calls, or fail the entire route when one shard is down. These do not provide live service discovery or partial health.
- **Rationale:** Docker service names resolve within the bridge, while per-shard results make partial failure visible.
- **Consequences/trade-offs:** Bad `SHARD_URLS` syntax remains an operator configuration error. Real Docker DNS behavior hasn't been checked in this environment.
- **References:** `frontend/main.py` `configured_shards`, `inspect_shard`, `shard_health_overview`; `shard/main.py` `health`.

### ADR-016 — Keep Phase 2 shard API infrastructure-only

- **Date:** 2026-11-02 (Phase 2 scope).
- **Status:** accepted
- **Context:** Validate containers, network, and storage before implementing canvas consistency across shards.
- **Decision:** Shard endpoints expose identity, health, and sample records only. Canvas draw and clear remain coordinator-local.
- **Alternatives considered:** Add tile mapping/routing in the same phase. Combining it with unverified container foundations would make failures harder to isolate.
- **Rationale:** A narrow infrastructure API verifies process and storage boundaries without implying tile ownership exists.
- **Consequences/trade-offs:** Shards currently do not receive canvas operations. Tile routing, epochs, migration, recovery, and undo remain future work.
- **References:** `shard/main.py`, `frontend/main.py`, `ARCHITECTURE.md`, `FOLLOW_ALONG.md`.

## Maintaining this log

Append a new ADR for each significant future architecture choice. Do not delete a historic record when code changes; mark it superseded or deprecated and append the replacement. Use an exact date when known and **Unknown** otherwise. Do not mark a proposal accepted until code and tests support its claims.
