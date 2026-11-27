# Architecture Decision Records

**Project:** Collaborative Pixel Canvas
**Log status:** Stage 3A source and local tests implemented; Docker runtime proof is pending.
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
- **Consequences/trade-offs:** Current-owner Clear and ordered durable Clear history are implemented. ADR-022 records user-aware undo and final-state hash verification. Exact cross-tile timelapse remains unavailable without a durable global commit journal.
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
- **Consequences/trade-offs:** A slow socket can delay other requests because sends occur under the lock. This is not cross-process ordering or distributed linearizability. Browser gap detection and reconnect snapshot positions are defined by ADR-021.
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

### ADR-017 — Stable row-major tile identity and modulo routing

- **Date:** 2026-11-03
- **Status:** accepted
- **Context:** Stage 3A needs a pure, repeatable tile calculation and one deterministic owner among the configured shard set, without a network request or Python randomized hashing.
- **Decision:** Validate strict integer pixels in the 1000×1000 canvas; derive 25×25 tile coordinates; form a row-major ordinal `tile_y * 40 + tile_x`; name tiles `tile-{row:02d}-{column:02d}`; assign the ordinal modulo the canonical lexically sorted `SHARD_IDS` tuple.
- **Alternatives considered:** Python `hash()` (randomized per process), a coordinator network lookup for every coordinate, or an ownership registry. The first is not stable; the second adds latency; the registry/epochs are Stage 3B work.
- **Rationale:** Arithmetic is easy to test and shared identically by coordinator and shards. The 1,600 tiles divide as 533/533/534 over three IDs.
- **Consequences/trade-offs:** The mapping is deterministic only for a fixed shard set. Adding/removing a shard remaps many tiles, so do not change active `SHARD_IDS` while data matters. No migration or explicit ownership table exists yet.
- **References:** `shared/tiles.py`, `tests/test_tiles.py`, `frontend/main.py`, `shard/main.py`.

### ADR-018 — Persist routed pixels at the selected shard before broadcast

- **Date:** 2026-11-03
- **Status:** accepted
- **Context:** Stage 3A must make the tile owner authoritative for a pixel write while preserving server-managed user ownership and WebSocket behavior.
- **Decision:** The coordinator derives the owner, sends coordinates/tile/color/server session owner to exactly that shard, and only updates its cache/broadcasts after validating a successful commit receipt. A shard independently recomputes the tile, rejects mismatched tile IDs or tiles assigned elsewhere, and transactionally upserts one SQLite pixel row containing both color and owner. State reads collect records from all active shards.
- **Alternatives considered:** Keep the coordinator as the pixel source of truth and mirror writes to shards, broadcast before shard acknowledgment, or let any shard accept any coordinate. These can lose data on coordinator failure, show uncommitted changes, or create duplicate/conflicting pixel copies.
- **Rationale:** The selected shard becomes the authoritative storage location for the tile's current pixels, and the client sees a commit only after that shard confirms it.
- **Consequences/trade-offs:** This historic Stage 3A decision was superseded as draw operation IDs, epochs, durable sequence positions, and retry deduplication were implemented. Clear's original direct fan-out was also replaced by ADR-019; the protocol remains non-atomic across shard databases. The opt-in Compose integration test checks two browser sessions, owner-shard persistence, coordinator state refresh, non-owner rejection, and WebSocket snapshots/draw broadcasts. A passing integration run depends on access to Docker and live Compose services.
- **References:** `frontend/main.py` `request_shard`, `load_pixels_from_shards`, `draw`, `clear_my_pixels`; `shard/main.py` `write_pixel`, `list_pixels`, `clear_owned_pixels`; `tests/test_canvas.py`, `tests/test_infrastructure.py`, `tests/integration/test_docker_services.py`.

### ADR-019 — Recoverable cross-shard Clear with durable prepare and commit

- **Date:** 2026-11-27
- **Status:** accepted
- **Context:** A direct delete fan-out could remove pixels on some shards and then return an ambiguous partial failure. A retry also had no stable request identity.
- **Decision:** The browser creates a UUID and sends it in `X-Operation-ID`. Before shard calls, the coordinator durably records the user, participant shard IDs, and `preparing` phase in its SQLite ownership database. Each shard persists a prepared coordinate list keyed by that ID, validating that every affected tile is active locally. Once all shards prepare, the coordinator stores the combined coordinates and moves to `committing`; it then asks every shard to commit. Shard commit is transactional and idempotent, deleting only rows still owned by the initiating user. The coordinator marks the operation `committed` only after all shard receipts. Background recovery retries every unfinished Clear after coordinator startup. Draw and migration requests are held while a Clear is unfinished. Reusing an ID for another user is rejected.
- **Alternatives considered:** Keep best-effort delete fan-out (leaves permanently ambiguous partial outcomes); attempt a distributed transaction across independent SQLite databases (not supported); use a durable coordinator intent plus idempotent participant commits (selected because it allows retries and restart recovery without claiming distributed atomicity).
- **Guarantees and limits:** Once all participants and their persistent databases are available, an interrupted operation can resume to completion. Shard pixel deletion and its participant `committed` marker share one SQLite transaction; coordinator completion is recorded only after every participant returns the matching operation ID and commit status. The protocol is not atomic: a crash during commit can expose a temporarily partial canvas until recovery completes. With the single coordinator architecture, its lock orders client draws, Clear requests, and transfer initiation; recovery also takes this lock and broadcasts a refreshed state. Multiple coordinator processes are unsupported. Per-shard delete uses user and coordinate conditions, so an overwrite by another user is retained. Ordered/replayable Clear events are added by ADR-020. A lost database volume cannot be reconstructed from this protocol.
- **Assumptions:** Shard SQLite volumes survive container restarts and the coordinator ownership volume survives coordinator restarts. During recovery, the configured participant shard identities remain available and stable. The legacy shard-local `DELETE /pixels` route is not used by the coordinator and bypasses this multi-shard protocol if called directly.
- **References:** `frontend/main.py` `clear_operations`, `finish_clear`, `recover_pending_work`, `clear_my_pixels`; `shard/main.py` `clear_operations`, `prepare_clear`, `commit_clear`; `frontend/index.html`; `tests/test_ordered_operations.py`.

### ADR-020 — Finalize Clear history in each tile's ordered log

- **Date:** 2026-11-28
- **Status:** accepted
- **Context:** The recoverable Clear protocol persisted participant state but its history did not replay. Appending Clear to a separate table would leave it unordered against draw operations. A single global sequence is unnecessary for canvas reconstruction because each operation affects one tile at a time and pixels in different tiles are independent.
- **Decision:** Keep `pixel_operations` as the per-tile ordered history. A participant commit transaction deletes prepared user-owned pixels and marks that participant committed, but does not append a replay event. Only after every participant has committed does the coordinator move to `finalizing` and call each shard's finalize endpoint. Each shard appends one typed Clear event per affected tile using that tile's next sequence; the event records the parent Clear ID, initiating session, and coordinates. Finalization and the participant `history_finalized` marker share one SQLite transaction. After all shards finalize, the coordinator marks the Clear `committed` and `history_finalized`. Retries do not increment sequences or apply an event twice. A no-op Clear remains in coordinator history but needs no tile event.
- **Replay and snapshots:** The existing migration checkpoint is a tile pixel snapshot plus `last_sequence`. Restore installs that state as the destination baseline; operation import applies contiguous later draw/Clear events. A Clear removes a replayed coordinate only if it is still owned by the recorded session, so a later draw at a greater tile sequence takes effect normally. Migration is refused while the coordinator Clear is not fully committed, including partial history finalization.
- **Alternatives considered:** Add a global total-order log, which would duplicate established per-tile ordering without changing final canvas reconstruction; put Clear only in a side table, which has no relation to draw sequence; append events during each pixel commit, which would expose a partial Clear in history before all shards commit. The selected per-tile typed event plus delayed finalization preserves existing ordering and recovery behavior.
- **Guarantees and limits:** Every newly completed Clear has durable coordinator metadata and per-tile history events for affected tiles. Pixel commits and history finalization remain recoverable, not atomic, across shards. During finalization, some tile logs can contain the event while the coordinator still marks the overall Clear pending; coordinator-mediated migration/replay is blocked until all participants finalize. There is no public full-canvas replay/hash endpoint or total order across tiles. Clear operations completed before this change cannot be assigned an exact historical sequence retroactively; schema migration marks those records as legacy-complete without inventing order.

### ADR-021 — Detect WebSocket gaps with per-tile cursors and resnapshot

- **Context:** A WebSocket can disconnect or fail to deliver an event while other clients continue to mutate the durable canvas. The previous browser reconnected but had no sequence cursor to detect a gap, and its pre-connect `/state` fetch was discarded. The existing shard operation log already assigns a contiguous sequence within each tile and an ownership epoch.
- **Decision:** WebSocket `state` and `GET /state` include both pixels and a map of each initialized tile's `{epoch, sequence}` position. Shards expose durable positions from the same `tile_ownership` rows used by tile snapshots, including a fenced source until the coordinator records the destination owner. The coordinator filters by its ownership registry. Draw events carry `{tile_id, epoch, sequence}`; Clear events carry one position for each affected tile. Coordinator mutations, snapshot reads, and socket registration remain serialized by `canvas_lock`, and transfer activation plus ownership cutover now occur under that lock. The browser installs a snapshot cursor before enabling input. For each event it ignores a sequence already applied, applies only the next sequence for that tile, and closes/reconnects for a gap, malformed position, or backwards epoch. Reconnect obtains a new authoritative state over the WebSocket before enabling canvas actions. A failed server send closes the socket to trigger this path.
- **Snapshot consistency and ordering:** This is a single-coordinator handoff. While the coordinator lock is held, it reads current pixels and shard cursor positions, registers the socket, sends the snapshot, and then releases mutations to broadcast later events. An event committed during snapshot acquisition is therefore either reflected in both pixels and cursor or delivered after the state message. A cursor is per tile; no global cross-tile event order is created. Clear contributes a cursor advance only for tiles it changed; all its tile events are broadcast together after the recoverable Clear protocol is fully finalized. Coordinator restart loses WebSocket connections but durable shard pixels and cursors let the browser reconnect to current state.
- **Alternatives considered:** A coordinator-wide sequence would add ordering not needed for canvas state and would require its own durable transaction boundary. Fetching `/state` before opening `/ws` has a race in which an update can fall between the fetch and socket registration. Client-side guesses without durable cursors cannot reliably identify missing history.
- **Consequences/trade-offs:** Snapshot reads make one request to each shard for pixels and one for durable tile positions; no 1,600 empty-tile scan is needed because untouched tiles start at epoch 1, sequence 0. The lock can delay draws while shard reads or a slow socket send completes. The design depends on one coordinator serializing every mutation; multiple coordinator replicas and cross-process WebSocket fan-out are unsupported. Per-tile positions detect missing delivered events but do not provide a total order across tiles or replay the event stream into the browser; reconnect uses a current-state snapshot. If a shard is unavailable, the snapshot is rejected and the client stays disabled while retrying rather than accepting partial state. Clear remains recoverable, not atomic.
- **References:** `frontend/main.py` `finish_clear`, `recover_pending_work`; `shard/main.py` `finalize_clear`, `import_operations`, `tile_operations`; `tests/test_canvas.py`, `tests/test_ordered_operations.py`.

### ADR-022 — Durable tile checkpoints, replay verification, and safe user undo

- **Date:** 2026-11-28
- **Status:** accepted
- **Context:** Migration already transfers a consistent tile pixel snapshot and sequence boundary, but there was no persistent checkpoint API, explicit restore-and-replay verification, canvas hash, or undo implementation. The proposal requires user-aware undo, a ten-attempt limit, restoration verification, and Clear-aware history.
- **Decision — snapshots and replay:** An active shard can save an immutable tile snapshot in `tile_snapshots`, recording its ID, ownership epoch, last tile sequence, and pixels. Restoring a saved snapshot resets that tile's pixels to the saved baseline and replays every later per-tile log entry in sequence in the same SQLite transaction. A missing sequence or malformed operation rejects the restore without committing partial state. Draw, Clear, and Undo records are replayed; snapshot restore does not delete or rewrite operation IDs, sequence positions, or the current ownership epoch. Restoring is limited to the active owner.
- **Decision — hash:** `GET /hash` returns SHA-256 over a versioned ASCII serialization. Pixels sort by row-major tile index, then local y and x. Each record is `tileIndex(4 digits),localY(2 digits),localX(2 digits),COLOR\n`; colors are uppercase `#RRGGBB`. Empty pixels are omitted, a fixed canvas/version header is included, and ownership metadata is excluded. This makes equal visible canvases hash equally across owners and shard placement.
- **Decision — undo:** The coordinator stores each session's draw actions and monotonically increasing per-session action order in its persistent SQLite database. Each draw records the pixel state it replaced. `POST /undo` counts durable attempts per session and allows at most ten; a retry uses `X-Operation-ID` and does not count twice. It targets the session's latest not-yet-attempted draw. The owner shard appends a sequenced Undo event only after checking that the current pixel is still the expected color owned by that session. If a later user's overwrite is present, it records a no-effect `skipped` Undo event and preserves that pixel. A successful Undo restores the saved prior pixel or removes the pixel if none existed. Both outcomes replay safely.
- **Alternatives considered:** Reconstructing hashes from shard-local ownership/owner metadata would make hashes placement-dependent. Undo by unconditional coordinate deletion could erase another user's overwrite. Rewriting old history during restore would break operation ID deduplication. A global sequence for all tiles would enable an exact chronological timelapse but is unnecessary for final-state recovery and would require durable cross-shard commit-intent reconciliation.
- **Consequences/trade-offs:** Checkpoints are durable on the shard database that created them; loss of that volume also loses its local checkpoint and log. Hash verifies visible final state, not owner attribution or chronological history. Undo order is per session only and creates no total order across unrelated users or tiles. The project still cannot produce a correct whole-canvas chronological timelapse from independent tile positions. An exact timelapse requires a future durable coordinator commit journal and recovery protocol; timestamps or tile ID order must not be substituted.
- **References:** `frontend/main.py` `canonical_canvas_hash`, `/hash`, `user_draw_actions`, `undo_attempts`, `undo_last_draw`; `shard/main.py` `/tiles/{tile_id}/snapshots`, `restore_tile_snapshot`, `undo_pixel`, `import_operations`; `tests/test_canvas.py`, `tests/test_ordered_operations.py`.

## Maintaining this log

Append a new ADR for each significant future architecture choice. Do not delete a historic record when code changes; mark it superseded or deprecated and append the replacement. Use an exact date when known and **Unknown** otherwise. Do not mark a proposal accepted until code and tests support its claims.
