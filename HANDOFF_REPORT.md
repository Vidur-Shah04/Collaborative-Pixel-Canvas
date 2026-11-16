# Project handoff report

Updated 16 November 2026. Current working branch: `tanisha/live-migration`.

## What the app does now

The browser sends a pixel draw to the coordinator. The coordinator finds which 25×25 tile contains that pixel and sends the draw to the tile's current shard. That shard saves the pixel and its place in the draw history in SQLite. The coordinator then tells connected browsers to paint the change. There are three shard containers, each with its own database volume. The coordinator also keeps session identities and tile ownership in its own SQLite volume.

## Work completed since the earlier progress review

1. **Made the canvas behave properly.** Two neighbouring colours used to cover each other on screen. We changed drawing to show one stored pixel as one canvas pixel and added zoom that keeps clicks aligned. If the connection drops, the page now reconnects and reloads the canvas before accepting another draw. The “Clear my pixels” button now reports when only some shards could be cleared and refreshes the page's view of the saved data.

2. **Made ownership survive a restart.** The app used to forget which pixels belonged to a browser when the main server restarted. It now saves the browser's session and tile ownership in SQLite. Existing session databases are upgraded without deleting their users.

3. **Made draw retries safe.** The coordinator gives every draw an ID, and a caller can reuse that ID when retrying a request whose reply was lost. The shard saves the ID with the pixel and returns the first result for a matching retry. It also numbers draws within each tile, so the transfer process knows their order.

4. **Made interrupted transfers resume.** If a server stopped halfway through moving a tile, the coordinator used to need another manual request. It now saves the transfer step and retries automatically when the coordinator or shard comes back.

5. **Kept drawing available during most of a transfer.** The old approach stopped drawing before copying a tile. Now the old shard keeps taking draws while the new shard receives the saved pixels and draw history. The coordinator copies any newer draws, pauses that tile briefly, blocks the old shard from writing, then switches ownership. Other tiles keep working throughout.

6. **Put testing on one command.** `./scripts/test-all.sh` runs the code tests, real Docker tests, and browser tests using a separate temporary canvas. GitHub Actions runs that same command after a push. The failure tests deliberately stop source and destination shards, restart the coordinator, and lose replies at several transfer steps. They check that saved draws arrive at the new owner and the old owner cannot accept new writes after the switch.

## Verified state

- Latest implementation commit before this report: `5079f4b` on `tanisha/live-migration`.
- Local full pipeline: **50 unit tests passed, 3 Docker integration tests passed, 3 Chromium browser tests passed**. Four opt-in tests appear as skipped during the first unit-only command; the script runs them in the later stages.
- [GitHub Actions run for live migration](https://github.com/Vidur-Shah04/Collaborative-Pixel-Canvas/actions/runs/43924890406): passed.
- The branch is pushed. No pull request has been opened or merged; `main` is not the current implementation branch.

## How to run and verify

From the `source` checkout, with Docker running:

```sh
test -f .env || cp .env.example .env
docker compose up --build
```

Open <http://localhost:8000/>. In the same folder, `docker compose down` stops the normal app and keeps its data. Do not use `down -v` for data you want to keep.

To run every check, free port 8000 first by stopping the normal app, then run:

```sh
./scripts/test-all.sh
```

This needs Python 3.12 and Docker. The first run downloads dependencies and Chromium. The script uses its own temporary Compose project and deletes only that project's test volumes.

## Next handoff steps

1. Start new work from `tanisha/live-migration`. Keep the active `SHARD_IDS` list fixed while data matters; adding or removing shards needs a separate rebalancing design.
2. Make Clear part of the ordered history, or define and test a durable cross-shard Clear contract. It currently uses best-effort deletions and can return partial success.
3. Add positions to browser events so a reconnecting browser can detect missed updates and replay or request a fresh snapshot.
4. Test larger tile histories, prolonged outages, and repeated crashes around the final cutover. The copy uses bounded log pages, but a long transfer can still take time.
5. Pin tested runtime dependency versions, review the remaining older design notes, and open a PR when the shared branch is ready.

Current limits: the final handoff briefly pauses draws on that tile; multiple coordinator replicas are unsupported; Clear is not in the draw log; and historical snapshots are read checkpoints rather than stored archives.
