import pytest

from shared.tiles import (
    TOTAL_TILES,
    configured_shard_ids,
    shard_for_tile,
    tile_for_pixel,
)


@pytest.mark.parametrize(
    ("x", "y", "tile_x", "tile_y", "tile_index", "tile_id"),
    [
        (0, 0, 0, 0, 0, "tile-00-00"),
        (24, 24, 0, 0, 0, "tile-00-00"),
        (25, 25, 1, 1, 41, "tile-01-01"),
        (49, 49, 1, 1, 41, "tile-01-01"),
        (50, 50, 2, 2, 82, "tile-02-02"),
        (999, 999, 39, 39, 1599, "tile-39-39"),
    ],
)
def test_pixel_to_tile_examples(x, y, tile_x, tile_y, tile_index, tile_id):
    address = tile_for_pixel(x, y)
    assert (address.tile_x, address.tile_y, address.tile_index, address.tile_id) == (
        tile_x, tile_y, tile_index, tile_id
    )


@pytest.mark.parametrize(
    ("x", "y"),
    [(-1, 0), (0, -1), (1000, 0), (0, 1000), (1.5, 2), (2, 1.5), (True, 0)],
)
def test_pixel_to_tile_rejects_invalid_or_ambiguous_coordinates(x, y):
    with pytest.raises(ValueError):
        tile_for_pixel(x, y)


def test_every_tile_maps_deterministically_to_exactly_one_balanced_shard():
    shard_ids = configured_shard_ids("shard-3,shard-1,shard-2")
    ownership_counts = {shard_id: 0 for shard_id in shard_ids}
    observed_ids = set()

    for y in range(0, 1000, 25):
        for x in range(0, 1000, 25):
            tile = tile_for_pixel(x, y)
            owner = shard_for_tile(tile, shard_ids)
            assert owner == shard_for_tile(tile, tuple(reversed(shard_ids)))
            assert owner in shard_ids
            assert tile.tile_id not in observed_ids
            observed_ids.add(tile.tile_id)
            ownership_counts[owner] += 1

    assert len(observed_ids) == TOTAL_TILES == 1600
    assert sorted(ownership_counts.values()) == [533, 533, 534]


def test_shard_id_list_rejects_duplicates_and_empty_entries():
    with pytest.raises(ValueError):
        configured_shard_ids("shard-1,shard-1")
    with pytest.raises(ValueError):
        configured_shard_ids("shard-1,,shard-2")
