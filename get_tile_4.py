import argparse
import io
import json
import math
import os
from pathlib import Path
import struct
import tempfile

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates
import requests
from PIL import Image

from map_profiles import DEFAULT_PROFILE_NAME, MapProfile, MapProfileError, load_map_profile
from terrain_rgb import TerrainRgbCache, decode_terrain_rgb

# =========================
# Configuration (Defaults)
# =========================

MAPBOX_TOKEN = "YOUR_TOKEN_HERE"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_CACHE_PATH = Path(__file__).with_name("cache")
PLACEHOLDER_TOKENS = {"", "YOUR_TOKEN_HERE", "pk.your_mapbox_token_here"}

# Game map anchor
TILE_DIMENSION_M = 500.0
ORIGIN_LAT = 35.382614
ORIGIN_LON = -83.49541
ORIGIN_EAST_BIAS_M = 8.0
ORIGIN_NORTH_BIAS_M = -8.0

# Mapbox Terrain-RGB source
MAPBOX_ZOOM = 15
MAPBOX_TILE_SIZE = 256

# Game height normalization
HEIGHT_MIN_M = 500.0
HEIGHT_MAX_M = 1500.0
HEIGHT_RESOLUTION = 513


def apply_map_profile(profile: MapProfile) -> None:
    global TILE_DIMENSION_M
    global ORIGIN_LAT
    global ORIGIN_LON
    global ORIGIN_EAST_BIAS_M
    global ORIGIN_NORTH_BIAS_M
    global MAPBOX_ZOOM
    global MAPBOX_TILE_SIZE
    global HEIGHT_MIN_M
    global HEIGHT_MAX_M
    global HEIGHT_RESOLUTION

    TILE_DIMENSION_M = profile.tile_dimension_meters
    ORIGIN_LAT = profile.origin_latitude
    ORIGIN_LON = profile.origin_longitude
    ORIGIN_EAST_BIAS_M = profile.origin_east_bias_meters
    ORIGIN_NORTH_BIAS_M = profile.origin_north_bias_meters
    MAPBOX_ZOOM = profile.mapbox_zoom
    MAPBOX_TILE_SIZE = profile.mapbox_tile_size
    HEIGHT_MIN_M = profile.height_min_meters
    HEIGHT_MAX_M = profile.height_max_meters
    HEIGHT_RESOLUTION = profile.height_resolution


def resolve_height_offset(profile: MapProfile, args: argparse.Namespace) -> dict[str, float | str]:
    if args.no_offset:
        return {"mode": "none"}

    if args.height_offset is not None:
        return {"mode": "uniform", "meters": float(args.height_offset)}

    linear_override = any(
        value is not None
        for value in (args.offset_east_x, args.offset_west_x, args.offset_max)
    )
    if linear_override:
        profile_offset = profile.height_offset
        east = profile_offset.east_tile_x if profile_offset.mode == "linear_x" else -66.0
        west = profile_offset.west_tile_x if profile_offset.mode == "linear_x" else -98.0
        maximum = profile_offset.max_meters if profile_offset.mode == "linear_x" else 40.0
        return {
            "mode": "linear_x",
            "east": east if args.offset_east_x is None else float(args.offset_east_x),
            "west": west if args.offset_west_x is None else float(args.offset_west_x),
            "max": maximum if args.offset_max is None else float(args.offset_max),
        }

    profile_offset = profile.height_offset
    if profile_offset.mode == "uniform":
        return {"mode": "uniform", "meters": profile_offset.meters}
    if profile_offset.mode == "linear_x":
        return {
            "mode": "linear_x",
            "east": profile_offset.east_tile_x,
            "west": profile_offset.west_tile_x,
            "max": profile_offset.max_meters,
        }
    return {"mode": "none"}

# =========================
# NLCD land cover defaults
# =========================
NLCD_WMS_URL = "https://www.mrlc.gov/geoserver/mrlc_display/NLCD_2021_Land_Cover_L48/wms"
NLCD_GUTTER_PX = 48
NLCD_RESOLUTION = 513 + 2 * NLCD_GUTTER_PX
NLCD_BLUR_SIGMA = 16.0
ALL_VEG_PRESETS = [0, 1, 2, 3, 4, 5, 6, 7]

NLCD_COLOR_MAP = {
    (71, 107, 160): (0, True),    # 11 Open Water
    (186, 216, 234): (2, True),   # 95 Emergent Wetlands
    (112, 163, 186): (1, True),   # 90 Woody Wetlands
    (221, 201, 201): (5, False),  # 21 Developed Open Space
    (216, 147, 130): (2, False),  # 22 Developed Low Intensity
    (237, 0, 0): (7, False),      # 23 Developed Medium Intensity
    (170, 0, 0): (7, False),      # 24 Developed High Intensity
    (178, 173, 163): (6, False),  # 31 Barren Land
    (104, 170, 99): (0, False),   # 41 Deciduous Forest
    (28, 99, 48): (0, False),     # 42 Evergreen Forest
    (181, 201, 142): (0, False),  # 43 Mixed Forest
    (204, 186, 124): (3, False),  # 52 Shrub/Scrub
    (226, 226, 193): (5, False),  # 71 Grassland/Herbaceous
    (219, 216, 61): (2, False),   # 81 Pasture/Hay
    (170, 112, 40): (7, False),   # 82 Cultivated Crops
}
NLCD_DEFAULT = (0, False)


def load_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {config_path.name}: {exc}") from exc

    if not isinstance(data, dict):
        raise SystemExit(f"{config_path.name} must contain a JSON object.")

    return data


def resolve_mapbox_token(cli_token: str | None, config_path: Path) -> str:
    if cli_token:
        cli_token = cli_token.strip()
        if cli_token and cli_token not in PLACEHOLDER_TOKENS:
            return cli_token

    env_token = os.getenv("MAPBOX_TOKEN", "").strip()
    if env_token and env_token not in PLACEHOLDER_TOKENS:
        return env_token

    config_token = load_config(config_path).get("mapbox_token")
    if isinstance(config_token, str):
        config_token = config_token.strip()
        if config_token and config_token not in PLACEHOLDER_TOKENS:
            return config_token

    fallback_token = MAPBOX_TOKEN.strip()
    if fallback_token and fallback_token not in PLACEHOLDER_TOKENS:
        return fallback_token

    raise SystemExit(
        "Mapbox token not configured. Pass --token, set MAPBOX_TOKEN, or create config.json from config.json.example."
    )


def _signed_tile_component(value: int) -> str:
    if not -999 <= value <= 999:
        raise SystemExit("Signed tile coordinates must stay between -999 and 999.")
    return f"-{abs(value):03d}" if value < 0 else f"{value:03d}"


def build_output_filename(
    gx: int,
    gy: int,
    base_x: int | None,
    base_y: int | None,
    signed_filenames: bool = False,
) -> str:
    if signed_filenames:
        return f"tile_{_signed_tile_component(gx)}_{_signed_tile_component(gy)}.data"

    output_x = gx if base_x is None else gx - base_x
    output_y = gy if base_y is None else gy - base_y

    if not (0 <= output_x <= 999 and 0 <= output_y <= 999):
        raise SystemExit(
            "Output filename indices must stay between 0 and 999 to match Tile_000_000.data. "
            "Use --base-x/--base-y to shift the output naming origin."
        )

    return f"Tile_{output_x:03d}_{output_y:03d}.data"


def valid_output_tile(path: Path, expected_size: int) -> bool:
    if not path.is_file():
        return False
    try:
        with Image.open(path) as image:
            image.load()
            return image.format == "PNG" and image.mode == "RGBA" and image.size == (expected_size, expected_size)
    except (OSError, ValueError):
        return False


def save_output_tile_atomic(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            image.save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


# =========================
# Projection helpers
# =========================

def lon_to_world_px(lon_deg: float, zoom: int) -> float:
    world_size = MAPBOX_TILE_SIZE * (2 ** zoom)
    return (lon_deg + 180.0) / 360.0 * world_size


def lat_to_world_py(lat_deg: float, zoom: int) -> float:
    world_size = MAPBOX_TILE_SIZE * (2 ** zoom)
    lat_rad = math.radians(lat_deg)
    merc = math.log(math.tan(math.pi / 4.0 + lat_rad / 2.0))
    return (1.0 - merc / math.pi) / 2.0 * world_size


def f32(x: float) -> float:
    return struct.unpack("!f", struct.pack("!f", float(x)))[0]


def add_meters(lat_deg, lon_deg, north_m, east_m):
    lat, lon = f32(lat_deg), f32(lon_deg)
    north, east = f32(north_m), f32(east_m)
    lat_out = f32(lat + f32(north / f32(111111.0)))
    cos_arg = f32(f32(0.017453292) * lat)
    denom = f32(f32(111111.0) * f32(math.cos(cos_arg)))
    lon_out = f32(lon + f32(east / denom))
    return lat_out, lon_out


def tile_position_to_latlon_bounds(gx, gy):
    min_lat, min_lon = add_meters(
        ORIGIN_LAT,
        ORIGIN_LON,
        TILE_DIMENSION_M * gy + ORIGIN_NORTH_BIAS_M,
        TILE_DIMENSION_M * gx + ORIGIN_EAST_BIAS_M,
    )
    max_lat, max_lon = add_meters(
        ORIGIN_LAT,
        ORIGIN_LON,
        TILE_DIMENSION_M * (gy + 1) + ORIGIN_NORTH_BIAS_M,
        TILE_DIMENSION_M * (gx + 1) + ORIGIN_EAST_BIAS_M,
    )
    return (min_lat, min_lon), (max_lat, max_lon)


# =========================
# Fetch logic
# =========================

def fetch_terrain_rgb_256(tx, ty, cache, refresh_cache=False):
    return cache.get_tile(MAPBOX_ZOOM, tx, ty, refresh=refresh_cache).image


def build_source_height_mosaic(left_px, top_px, right_px, bottom_px, cache, refresh_cache=False):
    tile_size = MAPBOX_TILE_SIZE
    tile_x0, tile_y0 = math.floor(left_px) // tile_size, math.floor(top_px) // tile_size
    tile_x1, tile_y1 = math.ceil(right_px) // tile_size, math.ceil(bottom_px) // tile_size
    mosaic = Image.new(
        "RGB",
        ((tile_x1 - tile_x0 + 1) * tile_size, (tile_y1 - tile_y0 + 1) * tile_size),
    )
    for ty in range(tile_y0, tile_y1 + 1):
        for tx in range(tile_x0, tile_x1 + 1):
            mosaic.paste(
                fetch_terrain_rgb_256(tx, ty, cache, refresh_cache),
                ((tx - tile_x0) * tile_size, (ty - tile_y0) * tile_size),
            )
    return decode_terrain_rgb(mosaic), tile_x0 * tile_size, tile_y0 * tile_size


def expand_nlcd_bounds(min_lat, min_lon, max_lat, max_lon, gutter_px=NLCD_GUTTER_PX):
    intervals = HEIGHT_RESOLUTION - 1
    latitude_step = (max_lat - min_lat) / intervals
    longitude_step = (max_lon - min_lon) / intervals
    # WMS BBOX coordinates describe the outside edges of the first and last
    # pixels. The extra half interval makes cropped pixel centers land exactly
    # on the 513 game samples, including the shared samples at tile borders.
    expansion_intervals = gutter_px + 0.5
    return (
        min_lat - expansion_intervals * latitude_step,
        min_lon - expansion_intervals * longitude_step,
        max_lat + expansion_intervals * latitude_step,
        max_lon + expansion_intervals * longitude_step,
    )


def fetch_nlcd_landcover(min_lat, min_lon, max_lat, max_lon):
    request_min_lat, request_min_lon, request_max_lat, request_max_lon = expand_nlcd_bounds(
        min_lat, min_lon, max_lat, max_lon
    )
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": "mrlc_display:NLCD_2021_Land_Cover_L48",
        "BBOX": f"{request_min_lon},{request_min_lat},{request_max_lon},{request_max_lat}",
        "WIDTH": str(NLCD_RESOLUTION),
        "HEIGHT": str(NLCD_RESOLUTION),
        "SRS": "EPSG:4326",
        "FORMAT": "image/png",
        "STYLES": "",
    }
    resp = requests.get(NLCD_WMS_URL, params=params, timeout=30)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def build_veg_water_grid(nlcd_img, out_res, blur_sigma, gutter_px):
    arr = np.array(nlcd_img.resize((out_res + 2 * gutter_px, out_res + 2 * gutter_px), Image.NEAREST))
    veg_raw = np.zeros(arr.shape[:2], dtype=np.uint8)
    water_raw = np.zeros(arr.shape[:2], dtype=bool)

    colors = set(map(tuple, arr.reshape(-1, 3).tolist()))
    for color in colors:
        best_dist, best_val = float("inf"), NLCD_DEFAULT
        if color in NLCD_COLOR_MAP:
            best_val = NLCD_COLOR_MAP[color]
        else:
            for ref, val in NLCD_COLOR_MAP.items():
                distance = sum((a - b) ** 2 for a, b in zip(color, ref))
                if distance < best_dist:
                    best_dist, best_val = distance, val
        preset, is_water = best_val
        mask = np.all(arr == color, axis=2)
        veg_raw[mask] = preset
        water_raw[mask] = is_water

    if blur_sigma > 0:
        stack = np.stack([(veg_raw == cls).astype(np.float32) for cls in ALL_VEG_PRESETS])
        stack = np.stack([gaussian_filter(mask, sigma=blur_sigma) for mask in stack])
        veg_raw = np.array(ALL_VEG_PRESETS, dtype=np.uint8)[np.argmax(stack, axis=0)]
        water_raw = gaussian_filter(water_raw.astype(np.float32), sigma=blur_sigma) >= 0.5

    gutter = gutter_px
    return veg_raw[gutter:gutter + out_res, gutter:gutter + out_res], water_raw[gutter:gutter + out_res, gutter:gutter + out_res]


# =========================
# Height sampling
# =========================

def sample_game_tile_heights(gx, gy, cache, offset_args, refresh_cache=False):
    (min_lat, min_lon), (max_lat, max_lon) = tile_position_to_latlon_bounds(gx, gy)
    left_px, right_px = lon_to_world_px(min_lon, MAPBOX_ZOOM), lon_to_world_px(max_lon, MAPBOX_ZOOM)
    top_px, bottom_px = lat_to_world_py(max_lat, MAPBOX_ZOOM), lat_to_world_py(min_lat, MAPBOX_ZOOM)
    source_heights, origin_x, origin_y = build_source_height_mosaic(
        left_px,
        top_px,
        right_px,
        bottom_px,
        cache,
        refresh_cache,
    )

    res = HEIGHT_RESOLUTION
    ox = np.arange(res, dtype=np.float64)
    src_x = np.clip((left_px - origin_x) + ox * (right_px - left_px) / (res - 1), 0.0, source_heights.shape[1] - 1.0001)
    src_y = np.clip((top_px - origin_y) + ox * (bottom_px - top_px) / (res - 1), 0.0, source_heights.shape[0] - 1.0001)
    yy, xx = np.meshgrid(src_y, src_x, indexing="ij")
    sampled = map_coordinates(source_heights, [yy.ravel(), xx.ravel()], order=1, mode="nearest").reshape(res, res)

    if offset_args["mode"] == "uniform":
        sampled += float(offset_args["meters"])
    elif offset_args["mode"] == "linear_x":
        east_x, west_x, max_m = offset_args["east"], offset_args["west"], offset_args["max"]
        t = np.clip((float(gx) + ox / (res - 1) - east_x) / (west_x - east_x), 0.0, 1.0)
        sampled += (t * max_m).astype(np.float32)[np.newaxis, :]

    return sampled


def pack_to_rgba(heights_m, veg_preset, veg_grid, water_grid):
    u16 = np.clip(
        np.floor((heights_m - HEIGHT_MIN_M) / (HEIGHT_MAX_M - HEIGHT_MIN_M) * 65535.0),
        0,
        65535,
    ).astype(np.uint16)
    if veg_preset is not None:
        veg = np.full(u16.shape, veg_preset & 0x7, dtype=np.uint8)
        water = np.zeros(u16.shape, dtype=bool)
    elif veg_grid is not None:
        veg = veg_grid[:u16.shape[0], :u16.shape[1]]
        water = water_grid[:u16.shape[0], :u16.shape[1]]
    else:
        veg = np.zeros(u16.shape, dtype=np.uint8)
        water = np.zeros(u16.shape, dtype=bool)
    a_ch = (water.astype(np.uint8) << 7) | (veg << 4)
    return Image.fromarray(
        np.stack(
            [
                (u16 >> 8).astype(np.uint8),
                (u16 & 0xFF).astype(np.uint8),
                np.zeros_like(u16, dtype=np.uint8),
                a_ch,
            ],
            axis=2,
        ),
        mode="RGBA",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Generate one Railroader terrain tile from Mapbox Terrain-RGB and optional NLCD land cover."
    )
    parser.add_argument("x", type=int, help="Game tile X coordinate")
    parser.add_argument("y", type=int, help="Game tile Y coordinate")
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_NAME,
        help=(
            "Map profile name from the profiles folder, or a profile JSON path "
            f"(default: {DEFAULT_PROFILE_NAME})."
        ),
    )
    parser.add_argument("--token", type=str, default=None, help="Mapbox API token. Overrides MAPBOX_TOKEN and config.json.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config.json containing mapbox_token.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"Reusable Terrain-RGB source-tile cache (default: {DEFAULT_CACHE_PATH}).",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Redownload Terrain-RGB source tiles even when they are already cached.",
    )
    parser.add_argument("--no-gutter", action="store_true", help="Crop output to 512x512 instead of 513x513.")
    parser.add_argument("--veg", type=int, choices=range(8), default=None, help="Vegetation preset 0-7 for the whole tile.")
    parser.add_argument("--no-nlcd", action="store_true", help="Skip NLCD land cover fetch.")
    parser.add_argument(
        "--nlcd-blur",
        type=float,
        default=NLCD_BLUR_SIGMA,
        help=f"Gaussian blur sigma for NLCD smoothing (default: {NLCD_BLUR_SIGMA}).",
    )
    parser.add_argument("--no-offset", action="store_true", help="Disable the active profile's height offset.")
    parser.add_argument(
        "--height-offset",
        type=float,
        default=None,
        help="Override the profile with a uniform height offset in metres.",
    )
    parser.add_argument("--offset-east-x", type=int, default=None, help="Override the east X boundary and use a linear X offset ramp.")
    parser.add_argument("--offset-west-x", type=int, default=None, help="Override the west X boundary and use a linear X offset ramp.")
    parser.add_argument(
        "--offset-max",
        type=float,
        default=None,
        help="Override the maximum height offset at the west edge and use a linear X offset ramp.",
    )
    parser.add_argument(
        "--base-x",
        type=int,
        default=None,
        help="Output filename origin for X. Generated file index is x - base_x.",
    )
    parser.add_argument(
        "--base-y",
        type=int,
        default=None,
        help="Output filename origin for Y. Generated file index is y - base_y.",
    )
    parser.add_argument(
        "--signed-filenames",
        action="store_true",
        help="Write FUSE/Railroader signed names such as tile_-002_004.data instead of remapped legacy names.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory that receives the generated tile (default: current directory).",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip generation when a complete PNG tile already exists at the output path.",
    )

    args = parser.parse_args()
    try:
        profile = load_map_profile(args.profile, Path(__file__).resolve().parent)
    except MapProfileError as exc:
        parser.error(str(exc))
    apply_map_profile(profile)
    output_name = build_output_filename(
        args.x,
        args.y,
        args.base_x,
        args.base_y,
        signed_filenames=args.signed_filenames,
    )
    output_path = args.output_dir.expanduser().resolve() / output_name
    expected_size = 512 if args.no_gutter else HEIGHT_RESOLUTION
    if args.skip_existing and valid_output_tile(output_path, expected_size):
        print(f"Profile: {profile.display_name} ({profile.profile_id})")
        print(f"Skipped existing {output_path}")
        return

    token = resolve_mapbox_token(args.token, args.config)
    cache = TerrainRgbCache(args.cache_dir, token, tile_size=MAPBOX_TILE_SIZE)

    offset_params = resolve_height_offset(profile, args)
    heights_m = sample_game_tile_heights(
        args.x,
        args.y,
        cache,
        offset_params,
        refresh_cache=args.refresh_cache,
    )

    veg_grid = water_grid = None
    if args.veg is None and not args.no_nlcd:
        (min_lat, min_lon), (max_lat, max_lon) = tile_position_to_latlon_bounds(args.x, args.y)
        nlcd_img = fetch_nlcd_landcover(min_lat, min_lon, max_lat, max_lon)
        veg_grid, water_grid = build_veg_water_grid(nlcd_img, HEIGHT_RESOLUTION, args.nlcd_blur, NLCD_GUTTER_PX)

    rgba = pack_to_rgba(heights_m, args.veg, veg_grid, water_grid)
    if args.no_gutter:
        rgba = rgba.crop((0, 0, 512, 512))
    save_output_tile_atomic(rgba, output_path)
    print(f"Profile: {profile.display_name} ({profile.profile_id})")
    print(f"Saved {output_path}")


if __name__ == "__main__":
    main()
