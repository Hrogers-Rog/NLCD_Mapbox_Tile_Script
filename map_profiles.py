import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PROFILE_NAME = "bushnell-whittier"
PROFILE_DIRECTORY_NAME = "profiles"


class MapProfileError(ValueError):
    """Raised when a map profile is missing or malformed."""


@dataclass(frozen=True)
class HeightOffsetProfile:
    mode: str
    meters: float = 0.0
    east_tile_x: float = 0.0
    west_tile_x: float = 0.0
    max_meters: float = 0.0


@dataclass(frozen=True)
class DemScanProfile:
    west_longitude: float
    south_latitude: float
    east_longitude: float
    north_latitude: float
    scan_zoom: int
    target_min_meters: float
    target_max_meters: float


@dataclass(frozen=True)
class MapProfile:
    profile_id: str
    display_name: str
    description: str
    origin_latitude: float
    origin_longitude: float
    origin_east_bias_meters: float
    origin_north_bias_meters: float
    tile_dimension_meters: float
    mapbox_zoom: int
    mapbox_tile_size: int
    height_min_meters: float
    height_max_meters: float
    height_resolution: int
    height_offset: HeightOffsetProfile
    dem_scan: DemScanProfile | None
    source_path: Path


def resolve_profile_path(profile: str | None, script_directory: Path) -> Path:
    value = (profile or DEFAULT_PROFILE_NAME).strip()
    if not value:
        value = DEFAULT_PROFILE_NAME

    candidate = Path(value).expanduser()
    looks_like_path = (
        candidate.is_absolute()
        or candidate.suffix.lower() == ".json"
        or "/" in value
        or "\\" in value
    )
    if looks_like_path:
        return candidate if candidate.is_absolute() else script_directory / candidate

    return script_directory / PROFILE_DIRECTORY_NAME / f"{value}.json"


def load_map_profile(profile: str | None, script_directory: Path) -> MapProfile:
    path = resolve_profile_path(profile, script_directory).resolve()
    if not path.exists():
        raise MapProfileError(f"Map profile was not found: {path}")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MapProfileError(f"Invalid JSON in map profile {path.name}: {exc}") from exc

    if not isinstance(document, dict):
        raise MapProfileError(f"Map profile {path.name} must contain a JSON object.")
    if document.get("schemaVersion") != 1:
        raise MapProfileError(f"Map profile {path.name} must set schemaVersion to 1.")

    origin = _required_object(document, "origin", path)
    terrain = _required_object(document, "terrain", path)
    offset = _required_object(document, "heightOffset", path)
    offset_mode = str(offset.get("mode", "")).strip().lower().replace("-", "_")
    if offset_mode == "linearx":
        offset_mode = "linear_x"
    if offset_mode not in {"none", "uniform", "linear_x"}:
        raise MapProfileError(
            f"Map profile {path.name} heightOffset.mode must be none, uniform, or linearX."
        )

    height_offset = HeightOffsetProfile(
        mode=offset_mode,
        meters=_number(offset, "meters", path, default=0.0),
        east_tile_x=_number(offset, "eastTileX", path, default=0.0),
        west_tile_x=_number(offset, "westTileX", path, default=0.0),
        max_meters=_number(offset, "maxMeters", path, default=0.0),
    )
    if height_offset.mode == "linear_x" and height_offset.east_tile_x == height_offset.west_tile_x:
        raise MapProfileError(
            f"Map profile {path.name} linearX offset requires different eastTileX and westTileX values."
        )

    height_min = _number(terrain, "heightMinMeters", path)
    height_max = _number(terrain, "heightMaxMeters", path)
    if height_max <= height_min:
        raise MapProfileError(
            f"Map profile {path.name} heightMaxMeters must be greater than heightMinMeters."
        )

    dem_scan = _load_dem_scan(document, terrain, path)

    return MapProfile(
        profile_id=_required_string(document, "id", path),
        display_name=_required_string(document, "displayName", path),
        description=str(document.get("description", "")).strip(),
        origin_latitude=_number(origin, "latitude", path),
        origin_longitude=_number(origin, "longitude", path),
        origin_east_bias_meters=_number(origin, "eastBiasMeters", path, default=0.0),
        origin_north_bias_meters=_number(origin, "northBiasMeters", path, default=0.0),
        tile_dimension_meters=_number(terrain, "tileDimensionMeters", path),
        mapbox_zoom=_integer(terrain, "mapboxZoom", path),
        mapbox_tile_size=_integer(terrain, "mapboxTileSize", path),
        height_min_meters=height_min,
        height_max_meters=height_max,
        height_resolution=_integer(terrain, "heightResolution", path),
        height_offset=height_offset,
        dem_scan=dem_scan,
        source_path=path,
    )


def _load_dem_scan(document: dict, terrain: dict, path: Path) -> DemScanProfile | None:
    value = document.get("demScan")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MapProfileError(f"Map profile {path.name} demScan must contain an object.")

    bounds = _required_object(value, "bounds", path)
    west = _number(bounds, "westLongitude", path)
    south = _number(bounds, "southLatitude", path)
    east = _number(bounds, "eastLongitude", path)
    north = _number(bounds, "northLatitude", path)
    if not (-180.0 <= west < east <= 180.0):
        raise MapProfileError(
            f"Map profile {path.name} demScan longitude bounds must satisfy -180 <= west < east <= 180."
        )
    if not (-85.05112878 <= south < north <= 85.05112878):
        raise MapProfileError(
            f"Map profile {path.name} demScan latitude bounds must be inside Web Mercator and south < north."
        )

    scan_zoom = value.get("zoom", terrain.get("mapboxZoom"))
    if isinstance(scan_zoom, bool) or not isinstance(scan_zoom, int) or not 0 <= scan_zoom <= 22:
        raise MapProfileError(f"Map profile {path.name} demScan.zoom must be an integer from 0 through 22.")

    target_min = _number(value, "targetMinMeters", path)
    target_max = _number(value, "targetMaxMeters", path)
    terrain_min = _number(terrain, "heightMinMeters", path)
    terrain_max = _number(terrain, "heightMaxMeters", path)
    if not (terrain_min <= target_min < target_max <= terrain_max):
        raise MapProfileError(
            f"Map profile {path.name} demScan targets must stay inside the terrain encoding range."
        )

    return DemScanProfile(
        west_longitude=west,
        south_latitude=south,
        east_longitude=east,
        north_latitude=north,
        scan_zoom=scan_zoom,
        target_min_meters=target_min,
        target_max_meters=target_max,
    )


def _required_object(document: dict, key: str, path: Path) -> dict:
    value = document.get(key)
    if not isinstance(value, dict):
        raise MapProfileError(f"Map profile {path.name} must contain an object at {key}.")
    return value


def _required_string(document: dict, key: str, path: Path) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MapProfileError(f"Map profile {path.name} must contain a nonblank string at {key}.")
    return value.strip()


def _number(document: dict, key: str, path: Path, default: float | None = None) -> float:
    value = document.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MapProfileError(f"Map profile {path.name} must contain a number at {key}.")
    return float(value)


def _integer(document: dict, key: str, path: Path) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MapProfileError(f"Map profile {path.name} must contain an integer at {key}.")
    return value
