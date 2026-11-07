# Ordered operations foundation

This branch adds a fixed-owner protocol foundation. Each coordinator draw receives an operation ID (or uses the client's supplied ID), carries epoch 1, and is sent to the tile's deterministic owner. The shard commits pixels and a per-tile sequence in one SQLite transaction. Repeating an operation ID with the same payload returns the stored result without another write or sequence increment; reusing it for different content returns 409. New operations apply in shard arrival/commit order. A tile snapshot reads its pixels and last sequence in one SQLite transaction.

The current assignment remains the Stage 3A modulo mapping, and epoch 1 is fixed. There is no ownership-transfer endpoint or coordinator ownership registry yet. Do not change active `SHARD_IDS` while data matters. Clear is not represented in the operation log and remains non-atomic across shards. Snapshot responses are read checkpoints, not persisted historical snapshots or a replay API.

Next: define a durable ownership registry and a fenced transfer transaction before increasing an epoch or routing to a new owner. Then add ordered Clear semantics, a log read/replay API, client event positions and resynchronization, and migration failure tests. An epoch change must fence the old shard before the new one can commit.
