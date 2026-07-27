import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import numpy as np
from PIL import Image

from scan_dem import write_json_atomic


SIGNED_TILE_PATTERN = re.compile(r"tile_(-?\d+)_(-?\d+)\.data")


def validate_map(map_json: Path) -> dict:
    document = json.loads(map_json.read_text(encoding="utf-8"))
    listed = [(int(tile["x"]), int(tile["y"])) for tile in document.get("tiles", [])]
    expected = set(listed)
    duplicate_manifest_entries = len(listed) - len(expected)
    map_directory = map_json.parent
    actual = {}
    invalid_names = []
    for path in map_directory.glob("tile_*.data"):
        match = SIGNED_TILE_PATTERN.fullmatch(path.name)
        if match is None:
            invalid_names.append(path.name)
        else:
            actual[(int(match.group(1)), int(match.group(2)))] = path

    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)
    invalid_files = []
    clipped_pixels = 0
    minimum_u16 = 65535
    maximum_u16 = 0
    minimum_tile = None
    maximum_tile = None
    total_bytes = 0
    edges = {}
    for coordinate in sorted(expected & set(actual)):
        path = actual[coordinate]
        total_bytes += path.stat().st_size
        try:
            with Image.open(path) as image:
                image.load()
                if image.format != "PNG" or image.mode != "RGBA" or image.size != (513, 513):
                    invalid_files.append(path.name)
                    continue
                array = np.asarray(image).copy()
        except (OSError, ValueError):
            invalid_files.append(path.name)
            continue

        height = (array[:, :, 0].astype(np.uint16) << 8) | array[:, :, 1].astype(np.uint16)
        tile_minimum = int(height.min())
        tile_maximum = int(height.max())
        if tile_minimum < minimum_u16:
            minimum_u16 = tile_minimum
            minimum_tile = coordinate
        if tile_maximum > maximum_u16:
            maximum_u16 = tile_maximum
            maximum_tile = coordinate
        clipped_pixels += int(np.count_nonzero((height == 0) | (height == 65535)))
        alpha = array[:, :, 3]
        edges[coordinate] = {
            "westHeight": height[:, 0].copy(),
            "eastHeight": height[:, -1].copy(),
            "northHeight": height[0, :].copy(),
            "southHeight": height[-1, :].copy(),
            "westAlpha": alpha[:, 0].copy(),
            "eastAlpha": alpha[:, -1].copy(),
            "northAlpha": alpha[0, :].copy(),
            "southAlpha": alpha[-1, :].copy(),
        }

    shared_edges = 0
    maximum_height_seam_u16 = 0
    alpha_mismatch_samples = 0
    for (x, y), current in edges.items():
        east = edges.get((x + 1, y))
        if east is not None:
            shared_edges += 1
            maximum_height_seam_u16 = max(
                maximum_height_seam_u16,
                int(np.max(np.abs(current["eastHeight"].astype(np.int32) - east["westHeight"].astype(np.int32)))),
            )
            alpha_mismatch_samples += int(np.count_nonzero(current["eastAlpha"] != east["westAlpha"]))
        north = edges.get((x, y + 1))
        if north is not None:
            shared_edges += 1
            maximum_height_seam_u16 = max(
                maximum_height_seam_u16,
                int(np.max(np.abs(current["northHeight"].astype(np.int32) - north["southHeight"].astype(np.int32)))),
            )
            alpha_mismatch_samples += int(np.count_nonzero(current["northAlpha"] != north["southAlpha"]))

    valid = not any(
        (
            duplicate_manifest_entries,
            invalid_names,
            missing,
            extra,
            invalid_files,
            clipped_pixels,
            maximum_height_seam_u16,
            alpha_mismatch_samples,
        )
    )
    decode_height = lambda value: 500.0 + value / 65535.0 * 1000.0
    return {
        "schemaVersion": 1,
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "mapJson": str(map_json),
        "valid": valid,
        "manifestTileCount": len(listed),
        "uniqueManifestTileCount": len(expected),
        "duplicateManifestEntries": duplicate_manifest_entries,
        "terrainFileCount": len(actual),
        "missingTileCount": len(missing),
        "extraTileCount": len(extra),
        "invalidFilenameCount": len(invalid_names),
        "invalidFileCount": len(invalid_files),
        "totalBytes": total_bytes,
        "height": {
            "minimumMeters": decode_height(minimum_u16) if edges else None,
            "minimumTile": minimum_tile,
            "maximumMeters": decode_height(maximum_u16) if edges else None,
            "maximumTile": maximum_tile,
            "clippedPixelCount": clipped_pixels,
        },
        "seams": {
            "sharedEdgeCount": shared_edges,
            "maximumHeightDifferenceU16": maximum_height_seam_u16,
            "maximumHeightDifferenceMeters": maximum_height_seam_u16 / 65535.0 * 1000.0,
            "alphaMismatchSampleCount": alpha_mismatch_samples,
        },
        "samples": {
            "missingTiles": missing[:20],
            "extraTiles": extra[:20],
            "invalidFiles": invalid_files[:20],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Exhaustively validate a generated FUSE terrain package and every shared edge.")
    parser.add_argument("map_json", type=Path)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    map_json = args.map_json.expanduser().resolve()
    report = validate_map(map_json)
    report_path = (args.report or map_json.parent.parent / "validation-report.json").expanduser().resolve()
    write_json_atomic(report_path, report)
    print(f"Valid: {str(report['valid']).lower()}")
    print(f"Tiles: {report['terrainFileCount']:,}/{report['uniqueManifestTileCount']:,}")
    print(
        f"Height range: {report['height']['minimumMeters']:.2f} m to "
        f"{report['height']['maximumMeters']:.2f} m; clipped pixels: {report['height']['clippedPixelCount']}"
    )
    print(
        f"Shared edges: {report['seams']['sharedEdgeCount']:,}; "
        f"height error: {report['seams']['maximumHeightDifferenceMeters']:.6f} m; "
        f"alpha mismatches: {report['seams']['alphaMismatchSampleCount']}"
    )
    print(f"Report: {report_path}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
