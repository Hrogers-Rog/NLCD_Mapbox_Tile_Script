import argparse
import json
from pathlib import Path

import get_tile_4
from map_profiles import MapProfile, MapProfileError, load_map_profile
from scan_dem import write_json_atomic


DEFAULT_PACKAGE_DIRECTORY = Path(__file__).with_name("maps") / "PRRMiddleDivision"


def starter_game_graph_document() -> dict:
    """Return the empty legacy graph shape consumed by the terrain/track editor."""
    return {
        "tracks": {"nodes": {}, "segments": {}, "spans": {}},
        "areas": {},
        "texts": {},
        "scenery": {},
        "splineys": {},
        "simpleGraphs": {},
        "mandelas": {},
    }


def track_authoring_bridge_document(package_id: str, display_name: str) -> dict:
    """Create the FUSE wrapper that converts game-graph.json at runtime."""
    return {
        "schemaVersion": "1.0",
        "id": f"{package_id}.track-authoring",
        "name": f"{display_name} Track Graph",
        "author": "FUSE",
        "modVersion": "0.1.0",
        "description": "Runtime bridge for the editor-authored game-graph.json track layer.",
        "tags": ["legacy-converted", "authoring-source"],
        "mixinto": {
            "target": "game-graph",
            "sourceFile": "game-graph.json",
        },
        "extensions": {
            "legacyData": {
                "convertedAtRuntime": True,
                "sourcePackageId": package_id,
                "supportStatus": "temporary",
            }
        },
    }


def derive_game_tile_bounds(profile: MapProfile) -> tuple[int, int, int, int]:
    if profile.dem_scan is None:
        raise MapProfileError(f"Map profile {profile.source_path.name} has no demScan footprint.")

    get_tile_4.apply_map_profile(profile)
    scan = profile.dem_scan
    matching_x = []
    matching_y = []
    for coordinate in range(-999, 1000):
        (_, minimum_lon), (_, maximum_lon) = get_tile_4.tile_position_to_latlon_bounds(coordinate, 0)
        if maximum_lon >= scan.west_longitude and minimum_lon <= scan.east_longitude:
            matching_x.append(coordinate)

        (minimum_lat, _), (maximum_lat, _) = get_tile_4.tile_position_to_latlon_bounds(0, coordinate)
        if maximum_lat >= scan.south_latitude and minimum_lat <= scan.north_latitude:
            matching_y.append(coordinate)

    if not matching_x or not matching_y:
        raise RuntimeError("The DEM footprint does not intersect the supported -999..999 game-tile range.")
    return min(matching_x), max(matching_x), min(matching_y), max(matching_y)


def package_documents(
    profile: MapProfile,
    x_min: int,
    x_max: int,
    y_min: int,
    y_max: int,
    package_id: str | None = None,
    display_name: str | None = None,
    spawn_position: tuple[float, float, float] = (250.0, 902.0, 250.0),
    tiles: list[dict] | None = None,
) -> tuple[dict, dict, dict]:
    package_id = package_id or profile.profile_id
    display_name = display_name or profile.display_name
    if tiles is None:
        tiles = [
            {"x": x, "y": y}
            for x in range(x_min, x_max + 1)
            for y in range(y_min, y_max + 1)
        ]
    map_document = {
        "origin": {
            "latitude": profile.origin_latitude,
            "longitude": profile.origin_longitude,
        },
        "tileDimension": profile.tile_dimension_meters,
        "tiles": tiles,
    }
    info_document = {
        "Id": package_id,
        "DisplayName": display_name,
        "Author": "FUSE",
        "Version": "0.1.0",
        "ManagerVersion": "0.27.10",
        "GameVersion": "2025.1",
        "Requirements": ["FUSE"],
        "LoadAfter": ["FUSE"],
        "FuseDataFiles": [
            f"{package_id}.fuse.json",
            f"{package_id}-tracks.fuse.json",
        ],
    }
    fuse_document = {
        "schemaVersion": "1.0",
        "id": package_id,
        "name": display_name,
        "author": "FUSE",
        "description": f"{display_name} terrain package generated from the active map profile.",
        "map": {
            "displayName": display_name,
            "description": "DEM-calibrated Pennsylvania terrain with base Bushnell content suppressed.",
            "mapFolder": "Map",
            "suppressBaseWorld": True,
        },
        "world": {
            "spawnPoints": [
                {
                    "name": f"{display_name} Spawn",
                    "position": {
                        "x": spawn_position[0],
                        "y": spawn_position[1],
                        "z": spawn_position[2],
                    },
                    "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "radius": 3.0,
                    "priority": 1000,
                }
            ]
        },
    }
    return map_document, info_document, fuse_document


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a FUSE map package and production export plan from a map profile.")
    parser.add_argument("--profile", default="prr-middle-division")
    parser.add_argument("--output", type=Path, default=DEFAULT_PACKAGE_DIRECTORY)
    parser.add_argument("--package-id", default=None)
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--x-min", type=int, default=None)
    parser.add_argument("--x-max", type=int, default=None)
    parser.add_argument("--y-min", type=int, default=None)
    parser.add_argument("--y-max", type=int, default=None)
    parser.add_argument("--spawn-x", type=float, default=250.0)
    parser.add_argument("--spawn-y", type=float, default=902.0)
    parser.add_argument("--spawn-z", type=float, default=250.0)
    parser.add_argument(
        "--tile-list",
        type=Path,
        default=None,
        help="Sparse tile-list JSON from build_route_footprint.py; overrides rectangle derivation.",
    )
    args = parser.parse_args()

    try:
        profile = load_map_profile(args.profile, Path(__file__).resolve().parent)
        explicit_bounds = (args.x_min, args.x_max, args.y_min, args.y_max)
        sparse_tiles = None
        if args.tile_list is not None:
            if any(value is not None for value in explicit_bounds):
                parser.error("--tile-list cannot be combined with explicit rectangle bounds.")
            tile_list_path = args.tile_list.expanduser().resolve()
            tile_list_document = json.loads(tile_list_path.read_text(encoding="utf-8"))
            sparse_tiles = [
                {"x": int(tile["x"]), "y": int(tile["y"])}
                for tile in tile_list_document["tiles"]
            ]
            if not sparse_tiles:
                parser.error("--tile-list did not contain any tiles.")
            x_min = min(tile["x"] for tile in sparse_tiles)
            x_max = max(tile["x"] for tile in sparse_tiles)
            y_min = min(tile["y"] for tile in sparse_tiles)
            y_max = max(tile["y"] for tile in sparse_tiles)
        elif any(value is not None for value in explicit_bounds):
            if any(value is None for value in explicit_bounds):
                parser.error("--x-min, --x-max, --y-min, and --y-max must be supplied together.")
            x_min, x_max = sorted((args.x_min, args.x_max))
            y_min, y_max = sorted((args.y_min, args.y_max))
        else:
            x_min, x_max, y_min, y_max = derive_game_tile_bounds(profile)
    except MapProfileError as exc:
        parser.error(str(exc))

    package_root = args.output.expanduser().resolve()
    map_directory = package_root / "Map"
    map_directory.mkdir(parents=True, exist_ok=True)
    package_id = args.package_id or profile.profile_id
    display_name = args.display_name or profile.display_name
    map_document, info_document, fuse_document = package_documents(
        profile,
        x_min,
        x_max,
        y_min,
        y_max,
        package_id=package_id,
        display_name=display_name,
        spawn_position=(args.spawn_x, args.spawn_y, args.spawn_z),
        tiles=sparse_tiles,
    )
    write_json_atomic(map_directory / "Map.json", map_document)
    write_json_atomic(package_root / "Info.json", info_document)
    write_json_atomic(package_root / f"{package_id}.fuse.json", fuse_document)
    write_json_atomic(
        package_root / f"{package_id}-tracks.fuse.json",
        track_authoring_bridge_document(package_id, display_name),
    )
    graph_path = package_root / "game-graph.json"
    if not graph_path.exists():
        write_json_atomic(graph_path, starter_game_graph_document())

    tile_count = len(map_document["tiles"])
    if sparse_tiles is None:
        resume_command = (
            f"python get_tile_area.py {x_min} {x_max} {y_min} {y_max} "
            f"--profile {profile.profile_id} --config Tile_Script\\config.json "
            f"--cache-dir cache --output-dir \"{map_directory}\" "
            "--signed-filenames --skip-existing --workers 8"
        )
    else:
        resume_command = (
            f"python get_tile_list.py \"{map_directory / 'Map.json'}\" "
            f"--profile {profile.profile_id} --config Tile_Script\\config.json "
            f"--cache-dir cache --output-dir \"{map_directory}\" "
            "--skip-existing --workers 8"
        )
    plan = {
        "schemaVersion": 1,
        "profile": str(profile.source_path),
        "packageId": package_id,
        "packageDirectory": str(package_root),
        "mapDirectory": str(map_directory),
        "gameTileBounds": {"xMin": x_min, "xMax": x_max, "yMin": y_min, "yMax": y_max},
        "tileCount": tile_count,
        "filenameMode": "signed",
        "heightOffsetMeters": profile.height_offset.meters,
        "resumeCommand": resume_command,
    }
    write_json_atomic(package_root / "export-plan.json", plan)

    print(f"Package: {package_root}")
    print(f"Game tile range: x={x_min}..{x_max}, y={y_min}..{y_max}")
    print(f"Production tiles: {tile_count:,}")
    print(f"Map manifest: {map_directory / 'Map.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
