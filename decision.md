# Collaborative Pixel Canvas — Decision Log

**Project:** Distributed Systems course project  
**Status:** Phase 1 foundation
**Last updated:** 2026-11-02

This document records decisions made so far. It distinguishes implemented prototype behavior from planned architecture so teammates do not assume unfinished distributed features already exist.

## 1. Current prototype decisions

### D-001 — Development environment
- **Decision:** Develop locally on a standard computer first; the university Remote Computing Environment (RCE) is optional.
- **Reason:** The proposal allows local execution and does not require separate physical machines for the initial development.
- **Status:** Agreed.

### D-002 — Backend and frontend technologies
- **Decision:** Use Python with FastAPI/Uvicorn for the backend, and HTML/CSS/JavaScript with the browser Canvas API for the frontend.
- **Reason:** These match the project proposal and are sufficient for the current prototype.
- **Status:** Implemented in prototype.

### D-003 — Initial API approach
- **Decision:** Use HTTP endpoints for drawing, state retrieval, and clear requests; use WebSockets for live updates to connected browser clients.
- **Current endpoints:** `GET /`, `GET /state`, `POST /draw`, `DELETE /clear`, and `WS /ws`.
- **Status:** Implemented in the current prototype; confirm route names against the current code before relying on them.

### D-004 — Live update delivery
- **Decision:** The backend broadcasts drawing updates and clear-related messages to connected WebSocket clients.
- **Reason:** All connected browsers should display a shared, live view.
- **Status:** Basic broadcast prototype works with two browser windows.

### D-005 — User identity
- **Decision:** The server issues a random HTTP-only cookie token and maps it to a server-side session ID. HTTP draw/clear requests and WebSockets use this session; request bodies cannot choose a user ID.
- **Limitation:** The registry is in memory, so restart invalidates sessions. This identifies a browser session, not a human account.
- **Status:** Implemented for the single-process prototype.

### D-006 — Pixel ownership and user-specific clear
- **Decision:** Track the current owner of each painted coordinate separately from its colour. A clear request should remove only pixels currently owned by the requesting user and broadcast the affected coordinates so all clients update.
- **Conflict rule:** If user B overwrites a pixel last drawn by user A, B becomes its current owner. Clearing A must not remove that pixel.
- **Status:** Implemented and covered by automated tests. Ownership metadata remains in process memory rather than durable history.
- **Scope clarification:** This is a current-owner model, not a complete operation-history model. Undo and durable history require additional records.

### D-007 — Initial state storage
- **Decision:** The first prototype stores painted pixels in process memory using coordinate keys such as `"x,y"`.
- **Reason:** This keeps the first implementation small while validating the request and rendering flow.
- **Limitation:** Data is lost when the server process restarts. This is not the final persistence design.
- **Status:** Implemented in prototype.

## 2. Planned architecture decisions

### D-008 — Canvas dimensions and tile size
- **Decision:** Logical canvas is 1000 × 1000 pixels; each tile is 25 × 25 pixels.
- **Derived result:** 40 × 40 = 1,600 tiles.
- **Status:** Required project design; tile-based sharding is not yet implemented in the basic prototype.

### D-009 — Shard processes
- **Decision:** Run shards as independent Python processes, each with its own port and persistent storage directory. Start locally, then consider RCE if useful.
- **Status:** Planned.

### D-010 — Communication
- **Decision:** Use WebSockets for live browser updates and HTTP/REST for control operations.
- **Status:** Basic browser WebSocket broadcast implemented; shard-to-shard communication and migration protocol remain planned.

### D-011 — Operation ordering and idempotency
- **Decision:** Each committed operation should have a unique operation ID, a per-tile update position, and an ownership epoch. Retries/replays should not apply the same operation more than once. Stale owners must reject writes from old epochs.
- **Status:** Planned; not provided by the initial in-memory prototype.

### D-012 — Persistence and recovery
- **Decision:** Use SQLite and durable operation logs, snapshots, and ordered replay to recover shard state after restart.
- **Status:** Planned.

### D-013 — Live migration and rebalancing
- **Decision:** Transfer tile state and relevant log position to a destination while accounting for writes that occur during transfer. Persist migration progress and make interrupted transfers recoverable. Change ownership only after the destination is ready; reject old-epoch writes at the source.
- **Status:** Planned. The protocol and its failure cases must be designed and tested before claiming safe migration.

### D-014 — Validation
- **Decision:** Test concurrent drawing, duplicate delivery, stale-owner writes, node addition/restart, migration, and shard crash during migration. Compare recovered canvas hashes and selected timelapse frames with a reference history.
- **Status:** Planned.

## 3. Current known limitations
1. The current prototype is single-process and in-memory.
2. The WebSocket broadcast manager is local to one backend process; it does not coordinate multiple server processes.
3. Connection IDs are not persistent authenticated identities.
4. Per-user clear depends on accurate server-side ownership metadata and correct broadcast handling.
5. No durable operation log, snapshot/replay, sharding, ownership epochs, or migration recovery is implemented yet.
6. A shared canvas needs a defined ordering policy for concurrent writes and clear operations before correctness can be guaranteed.

## 4. Immediate next steps
1. Verify user-specific clear across two browsers, including the overwrite case.
2. Keep API message formats documented and consistent between frontend and backend.
3. Add automated tests for tile coordinate mapping.
4. Introduce a tile ownership map and route operations to two local shard processes.
5. Add operation IDs, epochs, and per-tile positions.
6. Add SQLite logging and restart/replay tests.
7. Implement migration with persisted progress and failure-injection tests.

## 5. Decision log maintenance
When a design changes, update the relevant decision and its status rather than silently rewriting history. Record the date, rationale, trade-offs, and any migration steps needed for existing code.
