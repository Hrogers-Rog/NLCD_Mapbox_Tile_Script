import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from get_tile_4 import save_output_tile_atomic


def signed_component(value: int) -> str:
    return f"-{abs(value):03d}" if value < 0 else f"{value:03d}"


def tile_path(map_directory: Path, x: int, y: int) -> Path:
    return map_directory / f"tile_{signed_component(x)}_{signed_component(y)}.data"


def load_rgba(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image.load()
        if image.format != "PNG" or image.mode != "RGBA" or image.size != (513, 513):
            raise RuntimeError(f"Invalid terrain tile: {path}")
        return np.asarray(image).copy()


def stitch_map(map_json: Path) -> tuple[int, int]:
    document = json.loads(map_json.read_text(encoding="utf-8"))
    coordinates = {(int(tile["x"]), int(tile["y"])) for tile in document["tiles"]}
    map_directory = map_json.parent
    missing = [tile_path(map_directory, x, y) for x, y in coordinates if not tile_path(map_directory, x, y).is_file()]
    if missing:
        raise RuntimeError(f"Cannot stitch: {len(missing)} manifest tile(s) are missing, including {missing[0]}")

    changed_tiles = 0
    changed_samples = 0
    for x, y in sorted(coordinates, key=lambda coordinate: (coordinate[1], coordinate[0])):
        current_path = tile_path(map_directory, x, y)
        current = load_rgba(current_path)
        before = current[:, :, 3].copy()

        if (x - 1, y) in coordinates:
            west = load_rgba(tile_path(map_directory, x - 1, y))
            current[:, 0, 3] = west[:, -1, 3]
        if (x, y - 1) in coordinates:
            south = load_rgba(tile_path(map_directory, x, y - 1))
            current[-1, :, 3] = south[0, :, 3]

        difference_count = int(np.count_nonzero(before != current[:, :, 3]))
        if difference_count:
            save_output_tile_atomic(Image.fromarray(current, mode="RGBA"), current_path)
            changed_tiles += 1
            changed_samples += difference_count

    return changed_tiles, changed_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Make vegetation/water samples exactly continuous across terrain tile borders.")
    parser.add_argument("map_json", type=Path, help="FUSE Map/Map.json whose signed tiles should be stitched.")
    args = parser.parse_args()
    map_json = args.map_json.expanduser().resolve()
    changed_tiles, changed_samples = stitch_map(map_json)
    print(f"Stitched map: {map_json}")
    print(f"Changed tiles: {changed_tiles}; alpha edge samples: {changed_samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
