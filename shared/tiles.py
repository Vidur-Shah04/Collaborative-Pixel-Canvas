"""Deterministic pixel-to-tile and tile-to-shard routing for Stage 3A."""

from dataclasses import dataclass
import os

CANVAS_WIDTH = 1000
CANVAS_HEIGHT = 1000
TILE_WIDTH = 25
TILE_HEIGHT = 25
TILE_COLUMNS = CANVAS_WIDTH // TILE_WIDTH
TILE_ROWS = CANVAS_HEIGHT // TILE_HEIGHT
TOTAL_TILES = TILE_COLUMNS * TILE_ROWS
DEFAULT_SHARD_IDS = ("shard-1", "shard-2", "shard-3")


@dataclass(frozen=True)
class TileAddress:
    """Tile coordinates and stable row-major identity for one pixel."""

    tile_x: int
    tile_y: int
    tile_index: int
    tile_id: str


def tile_for_pixel(x: int, y: int) -> TileAddress:
    """Validate a pixel coordinate and return its row-major tile address."""
    if type(x) is not int or type(y) is not int:
        raise ValueError("pixel coordinates must be integers")
    if not 0 <= x < CANVAS_WIDTH or not 0 <= y < CANVAS_HEIGHT:
        raise ValueError("pixel coordinate is outside the canvas")

    tile_x = x // TILE_WIDTH
    tile_y = y // TILE_HEIGHT
    tile_index = tile_y * TILE_COLUMNS + tile_x
    return TileAddress(
        tile_x=tile_x,
        tile_y=tile_y,
        tile_index=tile_index,
        tile_id=f"tile-{tile_y:02d}-{tile_x:02d}",
    )


def configured_shard_ids(value: str | None = None) -> tuple[str, ...]:
    """Return unique shard IDs in canonical sorted order."""
    raw = value if value is not None else os.getenv("SHARD_IDS")
    if raw is None or not raw.strip():
        shard_ids = DEFAULT_SHARD_IDS
    else:
        shard_ids = tuple(part.strip() for part in raw.split(","))
    if not shard_ids or any(not shard_id for shard_id in shard_ids):
        raise ValueError("SHARD_IDS must contain comma-separated nonempty IDs")
    if len(set(shard_ids)) != len(shard_ids):
        raise ValueError("SHARD_IDS must not contain duplicates")
    return tuple(sorted(shard_ids))


def shard_for_tile(tile: TileAddress, shard_ids: tuple[str, ...] | None = None) -> str:
    """Assign a tile by row-major index modulo the canonical shard list."""
    if shard_ids is not None and not shard_ids:
        raise ValueError("at least one shard ID is required")
    owners = configured_shard_ids() if shard_ids is None else configured_shard_ids(",".join(shard_ids))
    return owners[tile.tile_index % len(owners)]
