import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile

import numpy as np

from get_tile_4 import DEFAULT_CONFIG_PATH, resolve_mapbox_token
from map_profiles import MapProfile, MapProfileError, load_map_profile
from terrain_rgb import (
    GeographicBounds,
    TERRAIN_RGB_CACHE_NAMESPACE,
    TERRAIN_RGB_TILESET,
    TerrainRgbCache,
    decode_terrain_rgb,
    pixel_center_to_lonlat,
    pixel_window_for_tile,
    tile_range_for_bounds,
)


DEFAULT_CACHE_PATH = Path(__file__).with_name("cache")
DEFAULT_REPORT_DIRECTORY = Path(__file__).with_name("reports")
DEFAULT_LARGE_SCAN_THRESHOLD = 1000


@dataclass(frozen=True)
class TileScanResult:
    minimum_meters: float
    minimum_longitude: float
    minimum_latitude: float
    maximum_meters: float
    maximum_longitude: float
    maximum_latitude: float
    sample_count: int
    cache_hit: bool


def calculate_uniform_offset(
    source_minimum: float,
    source_maximum: float,
    target_minimum: float,
    target_maximum: float,
) -> dict[str, float | bool]:
    required_minimum = target_minimum - source_minimum
    maximum_allowed = target_maximum - source_maximum
    recommended = float(math.ceil(required_minimum - 1e-9))
    shifted_minimum = source_minimum + recommended
    shifted_maximum = source_maximum + recommended
    feasible = recommended <= maximum_allowed + 1e-9
    return {
        "minimumRequiredMeters": required_minimum,
        "maximumAllowedMeters": maximum_allowed,
        "recommendedMeters": recommended,
        "shiftedMinimumMeters": shifted_minimum,
        "shiftedMaximumMeters": shifted_maximum,
        "lowerHeadroomMeters": shifted_minimum - target_minimum,
        "upperHeadroomMeters": target_maximum - shifted_maximum,
        "uniformOffsetFeasible": feasible,
    }


def profile_scan_bounds(profile: MapProfile) -> GeographicBounds:
    if profile.dem_scan is None:
        raise MapProfileError(
            f"Map profile {profile.source_path.name} has no demScan section. "
            "Add a production footprint before scanning."
        )
    return GeographicBounds(
        west=profile.dem_scan.west_longitude,
        south=profile.dem_scan.south_latitude,
        east=profile.dem_scan.east_longitude,
        north=profile.dem_scan.north_latitude,
    )


def scan_one_tile(
    cache: TerrainRgbCache,
    bounds: GeographicBounds,
    zoom: int,
    tile_x: int,
    tile_y: int,
    tile_size: int,
    refresh_cache: bool,
) -> TileScanResult | None:
    source = cache.get_tile(zoom, tile_x, tile_y, refresh=refresh_cache)
    x0, x1, y0, y1 = pixel_window_for_tile(bounds, zoom, tile_x, tile_y, tile_size)
    if x0 >= x1 or y0 >= y1:
        return None

    heights = decode_terrain_rgb(source.image)[y0:y1, x0:x1]
    finite = np.isfinite(heights)
    if not np.any(finite):
        return None

    safe_minimum = np.where(finite, heights, np.inf)
    safe_maximum = np.where(finite, heights, -np.inf)
    minimum_index = np.unravel_index(int(np.argmin(safe_minimum)), heights.shape)
    maximum_index = np.unravel_index(int(np.argmax(safe_maximum)), heights.shape)
    min_pixel_y, min_pixel_x = minimum_index
    max_pixel_y, max_pixel_x = maximum_index
    min_lon, min_lat = pixel_center_to_lonlat(
        zoom, tile_x, tile_y, x0 + min_pixel_x, y0 + min_pixel_y, tile_size
    )
    max_lon, max_lat = pixel_center_to_lonlat(
        zoom, tile_x, tile_y, x0 + max_pixel_x, y0 + max_pixel_y, tile_size
    )
    return TileScanResult(
        minimum_meters=float(heights[minimum_index]),
        minimum_longitude=min_lon,
        minimum_latitude=min_lat,
        maximum_meters=float(heights[maximum_index]),
        maximum_longitude=max_lon,
        maximum_latitude=max_lat,
        sample_count=int(np.count_nonzero(finite)),
        cache_hit=source.cache_hit,
    )


def source_tiles(bounds: GeographicBounds, zoom: int, tile_size: int) -> list[tuple[int, int]]:
    x0, x1, y0, y1 = tile_range_for_bounds(bounds, zoom, tile_size)
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def cached_tile_count(cache_root: Path, zoom: int, tiles: list[tuple[int, int]]) -> int:
    base = cache_root / TERRAIN_RGB_CACHE_NAMESPACE / str(zoom)
    return sum(1 for x, y in tiles if (base / str(x) / f"{y}.pngraw").is_file())


def build_report(
    profile: MapProfile,
    bounds: GeographicBounds,
    zoom: int,
    tile_size: int,
    tiles: list[tuple[int, int]],
    results: list[TileScanResult],
    cache_root: Path,
) -> dict:
    if not results:
        raise RuntimeError("The scan footprint did not contain any valid Terrain-RGB samples.")

    minimum = min(results, key=lambda result: result.minimum_meters)
    maximum = max(results, key=lambda result: result.maximum_meters)
    source_minimum = minimum.minimum_meters
    source_maximum = maximum.maximum_meters
    target_minimum = profile.dem_scan.target_min_meters
    target_maximum = profile.dem_scan.target_max_meters
    offset = calculate_uniform_offset(source_minimum, source_maximum, target_minimum, target_maximum)
    x0, x1, y0, y1 = tile_range_for_bounds(bounds, zoom, tile_size)
    cache_hits = sum(1 for result in results if result.cache_hit)

    return {
        "schemaVersion": 1,
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "profile": {
            "id": profile.profile_id,
            "displayName": profile.display_name,
            "path": str(profile.source_path),
        },
        "footprint": {
            "kind": "geographicRectangle",
            "bounds": {
                "westLongitude": bounds.west,
                "southLatitude": bounds.south,
                "eastLongitude": bounds.east,
                "northLatitude": bounds.north,
            },
        },
        "source": {
            "tileset": TERRAIN_RGB_TILESET,
            "zoom": zoom,
            "tileSize": tile_size,
            "tileRange": {"xMin": x0, "xMax": x1, "yMin": y0, "yMax": y1},
            "tileCount": len(tiles),
            "cacheHits": cache_hits,
            "downloads": len(tiles) - cache_hits,
            "cacheDirectory": str(cache_root.resolve()),
            "sampleCount": sum(result.sample_count for result in results),
        },
        "elevation": {
            "sourceMinimumMeters": source_minimum,
            "sourceMinimumLocation": {
                "longitude": minimum.minimum_longitude,
                "latitude": minimum.minimum_latitude,
            },
            "sourceMaximumMeters": source_maximum,
            "sourceMaximumLocation": {
                "longitude": maximum.maximum_longitude,
                "latitude": maximum.maximum_latitude,
            },
        },
        "targetEncoding": {
            "absoluteMinimumMeters": profile.height_min_meters,
            "absoluteMaximumMeters": profile.height_max_meters,
            "safeMinimumMeters": target_minimum,
            "safeMaximumMeters": target_maximum,
        },
        "heightOffset": offset,
        "productionReady": bool(offset["uniformOffsetFeasible"]),
    }


def write_json_atomic(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def update_profile_offset(profile: MapProfile, report: dict, report_path: Path) -> None:
    offset = report["heightOffset"]
    if not offset["uniformOffsetFeasible"]:
        raise RuntimeError("The profile cannot be updated because no safe uniform offset exists.")

    document = json.loads(profile.source_path.read_text(encoding="utf-8"))
    document["heightOffset"] = {
        "mode": "uniform",
        "meters": offset["recommendedMeters"],
        "calibration": {
            "report": os.path.relpath(report_path.resolve(), profile.source_path.parent.resolve()),
            "sourceMinimumMeters": report["elevation"]["sourceMinimumMeters"],
            "sourceMaximumMeters": report["elevation"]["sourceMaximumMeters"],
            "generatedAtUtc": report["generatedAtUtc"],
        },
    }
    write_json_atomic(profile.source_path, document)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan a profile's cached/remote Mapbox Terrain-RGB footprint and calculate a safe uniform height offset."
    )
    parser.add_argument(
        "--profile",
        default="prr-middle-division",
        help=(
            "Map profile name from the profiles folder, or a profile JSON path "
            "(default: prr-middle-division)."
        ),
    )
    parser.add_argument("--token", default=None, help="Mapbox token; overrides MAPBOX_TOKEN and config.json.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config.json.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_PATH, help="Reusable source-tile cache.")
    parser.add_argument("--report", type=Path, default=None, help="Output report JSON path.")
    parser.add_argument("--workers", type=int, default=min(8, max(2, os.cpu_count() or 2)))
    parser.add_argument("--refresh-cache", action="store_true", help="Force every source tile to be downloaded again.")
    parser.add_argument("--estimate-only", action="store_true", help="Show the request/cache estimate without scanning.")
    parser.add_argument(
        "--confirm-large-scan",
        action="store_true",
        help=f"Allow more than {DEFAULT_LARGE_SCAN_THRESHOLD} uncached source-tile requests.",
    )
    parser.add_argument(
        "--update-profile",
        action="store_true",
        help="After a successful scan, replace the profile's provisional offset with the recommendation.",
    )
    args = parser.parse_args()

    try:
        profile = load_map_profile(args.profile, Path(__file__).resolve().parent)
        bounds = profile_scan_bounds(profile)
    except MapProfileError as exc:
        parser.error(str(exc))

    zoom = profile.dem_scan.scan_zoom
    tile_size = profile.mapbox_tile_size
    tiles = source_tiles(bounds, zoom, tile_size)
    cache_root = args.cache_dir.expanduser().resolve()
    cached = 0 if args.refresh_cache else cached_tile_count(cache_root, zoom, tiles)
    missing = len(tiles) - cached
    x0, x1, y0, y1 = tile_range_for_bounds(bounds, zoom, tile_size)

    print(f"Profile: {profile.display_name} ({profile.profile_id})")
    print(
        f"Footprint: {bounds.west:.6f},{bounds.south:.6f} to "
        f"{bounds.east:.6f},{bounds.north:.6f}"
    )
    print(f"Terrain-RGB z{zoom} tile range: x {x0}..{x1}, y {y0}..{y1}")
    print(f"Source tiles: {len(tiles):,} total; {cached:,} cached; {missing:,} requests needed")
    if args.estimate_only:
        return 0

    if missing > DEFAULT_LARGE_SCAN_THRESHOLD and not args.confirm_large_scan:
        print(
            f"Refusing {missing:,} uncached requests without --confirm-large-scan. "
            "Run --estimate-only first, then repeat with that flag when ready.",
            file=sys.stderr,
        )
        return 2

    token = ""
    if missing or args.refresh_cache:
        token = resolve_mapbox_token(args.token, args.config)
    cache = TerrainRgbCache(cache_root, token, tile_size=tile_size)
    results: list[TileScanResult] = []
    failures: list[str] = []
    progress_step = max(1, len(tiles) // 100)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                scan_one_tile,
                cache,
                bounds,
                zoom,
                tile_x,
                tile_y,
                tile_size,
                args.refresh_cache,
            ): (tile_x, tile_y)
            for tile_x, tile_y in tiles
        }
        for completed, future in enumerate(as_completed(futures), 1):
            tile_x, tile_y = futures[future]
            try:
                result = future.result()
                if result is not None:
                    results.append(result)
            except Exception as exc:
                failures.append(f"z={zoom} x={tile_x} y={tile_y}: {exc}")
            if completed % progress_step == 0 or completed == len(tiles):
                print(f"Scanned {completed:,}/{len(tiles):,} source tiles")

    if failures:
        print(f"DEM scan failed for {len(failures)} source tile(s):", file=sys.stderr)
        for failure in failures[:20]:
            print(f"  {failure}", file=sys.stderr)
        if len(failures) > 20:
            print(f"  ...and {len(failures) - 20} more", file=sys.stderr)
        return 1

    report = build_report(profile, bounds, zoom, tile_size, tiles, results, cache_root)
    report_path = args.report or (DEFAULT_REPORT_DIRECTORY / f"{profile.profile_id}-dem-scan.json")
    report_path = report_path.expanduser().resolve()
    write_json_atomic(report_path, report)
    offset = report["heightOffset"]
    print(
        f"Source elevation range: {report['elevation']['sourceMinimumMeters']:.1f} m to "
        f"{report['elevation']['sourceMaximumMeters']:.1f} m"
    )
    print(
        f"Recommended uniform offset: {offset['recommendedMeters']:.0f} m; "
        f"shifted range {offset['shiftedMinimumMeters']:.1f} m to {offset['shiftedMaximumMeters']:.1f} m"
    )
    print(f"Production ready: {str(report['productionReady']).lower()}")
    print(f"Report: {report_path}")

    if args.update_profile:
        update_profile_offset(profile, report, report_path)
        print(f"Updated profile offset: {profile.source_path}")
    return 0 if report["productionReady"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
