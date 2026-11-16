# Ordered operations foundation

This branch adds a fixed-owner protocol foundation. Each coordinator draw receives an operation ID (or uses the client's supplied ID), carries epoch 1, and is sent to the owner in the coordinator's durable SQLite registry. The registry seeds a tile from the deterministic Stage 3A mapping on first use and survives coordinator container recreation in `coordinator_data`. The shard commits pixels and a per-tile sequence in one SQLite transaction. Repeating an operation ID with the same payload returns the stored result without another write or sequence increment; reusing it for different content returns 409. New operations apply in shard arrival/commit order. A tile snapshot reads its pixels and last sequence in one SQLite transaction. `/tiles/{tile_id}/operations?after=N` reads the durable draw log after a sequence boundary.

The initial assignment remains the Stage 3A modulo mapping. Tile ownership can move between configured shards: the coordinator records a pending transfer, copies a snapshot and bounded log pages while the source continues accepting draws, then briefly pauses that tile, fences the source, copies the final writes, activates the destination at the next epoch, and updates its ownership record. Draws remain available during the copy phase. Requests for that tile return 503 during the final fence and cutover, or while recovery is pending after a failure. Repeating the same transfer request resumes it. The admin route requires `ADMIN_TOKEN` in the coordinator environment and the matching `X-Admin-Token` request header.

Example, after setting `ADMIN_TOKEN` for the coordinator and starting Compose:

```sh
curl -X POST -H 'X-Admin-Token: YOUR_TOKEN' 'http://localhost:8000/admin/tiles/0/0/transfer?destination=shard-2'
```

This is live copy with a **brief fenced cutover**, not a zero-pause transfer. A stopped coordinator leaves the durable transfer row in place. On restart, the coordinator retries pending transfers in the background; it also retries while a shard is unavailable and finishes when that shard returns. If failure occurs before fencing, source draws can continue; after fencing, the tile remains unavailable until recovery finishes. Do not change active `SHARD_IDS` while data matters. Clear is not represented in the operation log and remains non-atomic across shards. Snapshot responses are read checkpoints, not persisted historical snapshots. There is no cross-coordinator coordination or authentication on shard-internal endpoints beyond the private Compose network.

Next: add ordered Clear semantics, client event positions and resynchronization, and additional failure tests for larger histories and prolonged outages. An epoch change must continue to fence the old shard before the new one can commit.
