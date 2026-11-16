# Project handoff report

Updated 16 November 2026. Current working branch: `tanisha/live-migration`.

## What the app does now

The browser sends a pixel draw to the coordinator. The coordinator finds which 25×25 tile contains that pixel and sends the draw to the tile's current shard. That shard saves the pixel and its place in the draw history in SQLite. The coordinator then tells connected browsers to paint the change. There are three shard containers, each with its own database volume. The coordinator also keeps session identities and tile ownership in its own SQLite volume.

## Work completed since the earlier progress review

| Problem | How we handled it |
| --- | --- |
| Neighbouring pixels visually covered each other | The browser now draws exactly one logical pixel per stored pixel. Zoom changes the display size and keeps click coordinates aligned. |
| A coordinator restart lost who owned pixels | Session tokens and user IDs now live in SQLite. Existing session tables are upgraded in place. |
| A failed Clear could leave browsers showing old pixels | Clear reports a partial result, refreshes from shard storage when possible, and makes browsers reconnect when a reliable refresh is unavailable. Clear is still not atomic across shards. |
| A dropped connection left the page stale | The browser reconnects with a delay, gets a fresh snapshot, and disables drawing and Clear until ready. Slow WebSocket sends have a time limit. |
| Tests and CI needed a repeatable setup | `./scripts/test-all.sh` installs test tools, runs unit tests, starts a separate temporary Compose project, runs Docker and Chromium tests, then removes that project's test containers and volumes. GitHub Actions runs the same script on pushes. |
| Retried draws might apply twice | Each draw has an operation ID. A shard stores the operation and pixel together and returns the original result on a matching retry. Each tile has an ordered sequence number and ownership epoch. |
| An interrupted tile transfer needed manual help | The coordinator records transfer progress in SQLite and retries it after restart or a shard outage. |
| Moving a tile stopped drawing for the whole copy | The coordinator now copies a tile snapshot and pages of draw history while the old shard keeps accepting draws. It catches up later writes, briefly pauses that tile, fences the old shard, copies final writes, and activates the new owner. |

Failure tests cover lost responses during snapshot, log copy, import, fencing, and activation; source and destination shard outages; and coordinator restart. They check that acknowledged writes survive and the old owner cannot keep writing after handoff.

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
