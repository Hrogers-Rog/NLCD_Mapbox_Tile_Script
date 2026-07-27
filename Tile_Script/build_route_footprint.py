import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import requests

from map_profiles import MapProfile, MapProfileError, load_map_profile
from scan_dem import write_json_atomic


OSM_API_ROOT = "https://api.openstreetmap.org/api/0.6"
OSM_USER_AGENT = "FUSE-terrain-planner/1.0"
DEFAULT_SOURCE_DIRECTORY = Path(__file__).with_name("routes") / "source"


def load_route_config(profile: MapProfile) -> dict:
    document = json.loads(profile.source_path.read_text(encoding="utf-8"))
    config = document.get("routeFootprint")
    if not isinstance(config, dict):
        raise MapProfileError(f"Map profile {profile.source_path.name} has no routeFootprint object.")
    required = ("geoJson", "corridorWidthTiles", "mainLineRelationId", "eastBroadTopRelationId", "mainLineBounds")
    missing = [key for key in required if key not in config]
    if missing:
        raise MapProfileError(f"Map profile {profile.source_path.name} routeFootprint is missing: {', '.join(missing)}")
    return config


def download_relation(relation_id: int, source_directory: Path, refresh: bool = False) -> dict:
    path = source_directory / f"osm-relation-{relation_id}.json"
    if path.is_file() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    response = requests.get(
        f"{OSM_API_ROOT}/relation/{relation_id}/full.json",
        headers={"User-Agent": OSM_USER_AGENT},
        timeout=240,
    )
    response.raise_for_status()
    document = response.json()
    write_json_atomic(path, document)
    return document


def relation_segments(document: dict, relation_id: int, include_way, bounds: dict) -> list[dict]:
    elements = document.get("elements", [])
    nodes = {
        int(element["id"]): (float(element["lon"]), float(element["lat"]))
        for element in elements
        if element.get("type") == "node"
    }
    ways = {
        int(element["id"]): element
        for element in elements
        if element.get("type") == "way"
    }
    relation = next(
        (
            element
            for element in elements
            if element.get("type") == "relation" and int(element.get("id", -1)) == relation_id
        ),
        None,
    )
    if relation is None:
        raise RuntimeError(f"OSM response did not contain relation {relation_id}.")

    segments = []
    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        way = ways.get(int(member["ref"]))
        if way is None or not include_way(way.get("tags", {})):
            continue
        coordinates = [nodes[node_id] for node_id in way.get("nodes", []) if node_id in nodes]
        for start, end in zip(coordinates, coordinates[1:]):
            clipped = clip_segment(start, end, bounds)
            if clipped is not None and clipped[0] != clipped[1]:
                segments.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "osmRelation": relation_id,
                            "osmWay": int(way["id"]),
                            "name": way.get("tags", {}).get("name"),
                            "railway": way.get("tags", {}).get("railway"),
                            "usage": way.get("tags", {}).get("usage"),
                        },
                        "geometry": {"type": "LineString", "coordinates": [clipped[0], clipped[1]]},
                    }
                )
    return segments


def clip_segment(start, end, bounds: dict):
    west = float(bounds["westLongitude"])
    south = float(bounds["southLatitude"])
    east = float(bounds["eastLongitude"])
    north = float(bounds["northLatitude"])
    x0, y0 = start
    x1, y1 = end
    dx = x1 - x0
    dy = y1 - y0
    t0 = 0.0
    t1 = 1.0
    for p, q in ((-dx, x0 - west), (dx, east - x0), (-dy, y0 - south), (dy, north - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        ratio = q / p
        if p < 0:
            if ratio > t1:
                return None
            t0 = max(t0, ratio)
        else:
            if ratio < t0:
                return None
            t1 = min(t1, ratio)
    return (x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy)


def main_line_way(tags: dict) -> bool:
    return tags.get("railway") in {"rail", "disused", "abandoned"}


def east_broad_top_main_way(tags: dict) -> bool:
    if tags.get("railway") not in {"rail", "narrow_gauge", "disused", "abandoned"}:
        return False
    if tags.get("usage") == "branch" or "branch" in str(tags.get("name", "")).lower():
        return False
    return tags.get("usage") == "main" or tags.get("name") in {"East Broad Top", "East Broad Top Railroad"}


def route_point_to_world(profile: MapProfile, longitude: float, latitude: float) -> tuple[float, float]:
    north = (latitude - profile.origin_latitude) * 111111.0 - profile.origin_north_bias_meters
    east = (
        (longitude - profile.origin_longitude)
        * 111111.0
        * math.cos(math.radians(profile.origin_latitude))
        - profile.origin_east_bias_meters
    )
    return east, north


def point_to_segment_distance_squared(px, py, x0, y0, x1, y1) -> float:
    dx = x1 - x0
    dy = y1 - y0
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return (px - x0) ** 2 + (py - y0) ** 2
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length_squared))
    nearest_x = x0 + t * dx
    nearest_y = y0 + t * dy
    return (px - nearest_x) ** 2 + (py - nearest_y) ** 2


def select_corridor_tiles(profile: MapProfile, features: list[dict], corridor_width_tiles: float) -> list[dict]:
    tile_size = profile.tile_dimension_meters
    radius = corridor_width_tiles * tile_size / 2.0
    radius_squared = radius * radius
    selected: set[tuple[int, int]] = set()
    for feature in features:
        coordinates = feature["geometry"]["coordinates"]
        (x0, y0) = route_point_to_world(profile, coordinates[0][0], coordinates[0][1])
        (x1, y1) = route_point_to_world(profile, coordinates[1][0], coordinates[1][1])
        candidate_x0 = max(-999, math.floor((min(x0, x1) - radius) / tile_size - 0.5))
        candidate_x1 = min(999, math.ceil((max(x0, x1) + radius) / tile_size - 0.5))
        candidate_y0 = max(-999, math.floor((min(y0, y1) - radius) / tile_size - 0.5))
        candidate_y1 = min(999, math.ceil((max(y0, y1) + radius) / tile_size - 0.5))
        for tile_x in range(candidate_x0, candidate_x1 + 1):
            center_x = (tile_x + 0.5) * tile_size
            for tile_y in range(candidate_y0, candidate_y1 + 1):
                center_y = (tile_y + 0.5) * tile_size
                if point_to_segment_distance_squared(center_x, center_y, x0, y0, x1, y1) <= radius_squared:
                    selected.add((tile_x, tile_y))
    return [{"x": x, "y": y} for x, y in sorted(selected)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a cached OSM railway GeoJSON and sparse Railroader tile footprint.")
    parser.add_argument("--profile", default="prr-middle-division")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIRECTORY)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    try:
        profile = load_map_profile(args.profile, Path(__file__).resolve().parent)
        config = load_route_config(profile)
    except MapProfileError as exc:
        parser.error(str(exc))

    source_directory = args.source_dir.expanduser().resolve()
    main_relation_id = int(config["mainLineRelationId"])
    ebt_relation_id = int(config["eastBroadTopRelationId"])
    main_document = download_relation(main_relation_id, source_directory, refresh=args.refresh)
    ebt_document = download_relation(ebt_relation_id, source_directory, refresh=args.refresh)
    main_bounds = config["mainLineBounds"]
    all_bounds = {
        "westLongitude": profile.dem_scan.west_longitude,
        "southLatitude": profile.dem_scan.south_latitude,
        "eastLongitude": profile.dem_scan.east_longitude,
        "northLatitude": profile.dem_scan.north_latitude,
    }
    features = relation_segments(main_document, main_relation_id, main_line_way, main_bounds)
    main_segment_count = len(features)
    features.extend(relation_segments(ebt_document, ebt_relation_id, east_broad_top_main_way, all_bounds))
    ebt_segment_count = len(features) - main_segment_count
    corridor_width = float(config["corridorWidthTiles"])
    tiles = select_corridor_tiles(profile, features, corridor_width)

    geojson_path = profile.source_path.parent.parent / config["geoJson"]
    geojson = {
        "type": "FeatureCollection",
        "name": "PRR Main Line Johnstown-Harrisburg and East Broad Top main line",
        "attribution": "Railway geometry © OpenStreetMap contributors, ODbL 1.0",
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "sourceRelations": [main_relation_id, ebt_relation_id],
        "features": features,
    }
    write_json_atomic(geojson_path, geojson)
    tile_list_path = geojson_path.with_name(geojson_path.stem + "-tiles.json")
    tile_document = {
        "schemaVersion": 1,
        "profile": profile.profile_id,
        "sourceGeoJson": str(geojson_path),
        "corridorWidthTiles": corridor_width,
        "corridorWidthMeters": corridor_width * profile.tile_dimension_meters,
        "tileCount": len(tiles),
        "tiles": tiles,
    }
    write_json_atomic(tile_list_path, tile_document)

    print(f"Main-line segments: {main_segment_count:,}")
    print(f"East Broad Top main-line segments: {ebt_segment_count:,}")
    print(f"Corridor width: {corridor_width:g} tiles ({corridor_width * profile.tile_dimension_meters:,.0f} m)")
    print(f"Sparse terrain tiles: {len(tiles):,}")
    print(f"GeoJSON: {geojson_path}")
    print(f"Tile list: {tile_list_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
