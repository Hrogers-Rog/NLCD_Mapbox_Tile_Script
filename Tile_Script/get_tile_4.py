import argparse
import io
import json
import math
import os
from pathlib import Path
import struct

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates
import requests
from PIL import Image

# =========================
# Configuration (Defaults)
# =========================

MAPBOX_TOKEN = "YOUR_TOKEN_HERE"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
PLACEHOLDER_TOKENS = {"", "YOUR_TOKEN_HERE"}

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


def build_output_filename(gx: int, gy: int, base_x: int | None, base_y: int | None) -> str:
    output_x = gx if base_x is None else gx - base_x
    output_y = gy if base_y is None else gy - base_y

    if not (0 <= output_x <= 999 and 0 <= output_y <= 999):
        raise SystemExit(
            "Output filename indices must stay between 0 and 999 to match Tile_000_000.data. "
            "Use --base-x/--base-y to shift the output naming origin."
        )

    return f"Tile_{output_x:03d}_{output_y:03d}.data"


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

def fetch_terrain_rgb_256(tx, ty, token):
    url = f"https://api.mapbox.com/v4/mapbox.terrain-rgb/{MAPBOX_ZOOM}/{tx}/{ty}.pngraw"
    resp = requests.get(url, params={"access_token": token}, timeout=30)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def build_source_height_mosaic(left_px, top_px, right_px, bottom_px, token):
    tile_x0, tile_y0 = math.floor(left_px) // 256, math.floor(top_px) // 256
    tile_x1, tile_y1 = math.ceil(right_px) // 256, math.ceil(bottom_px) // 256
    mosaic = Image.new("RGB", ((tile_x1 - tile_x0 + 1) * 256, (tile_y1 - tile_y0 + 1) * 256))
    for ty in range(tile_y0, tile_y1 + 1):
        for tx in range(tile_x0, tile_x1 + 1):
            mosaic.paste(fetch_terrain_rgb_256(tx, ty, token), ((tx - tile_x0) * 256, (ty - tile_y0) * 256))
    arr = np.array(mosaic, dtype=np.float32)
    return -10000.0 + 0.1 * (arr[:, :, 0] * 65536 + arr[:, :, 1] * 256 + arr[:, :, 2]), tile_x0 * 256, tile_y0 * 256


def fetch_nlcd_landcover(min_lat, min_lon, max_lat, max_lon):
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": "mrlc_display:NLCD_2021_Land_Cover_L48",
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat}",
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

def sample_game_tile_heights(gx, gy, token, offset_args):
    (min_lat, min_lon), (max_lat, max_lon) = tile_position_to_latlon_bounds(gx, gy)
    left_px, right_px = lon_to_world_px(min_lon, MAPBOX_ZOOM), lon_to_world_px(max_lon, MAPBOX_ZOOM)
    top_px, bottom_px = lat_to_world_py(max_lat, MAPBOX_ZOOM), lat_to_world_py(min_lat, MAPBOX_ZOOM)
    source_heights, origin_x, origin_y = build_source_height_mosaic(left_px, top_px, right_px, bottom_px, token)

    res = HEIGHT_RESOLUTION
    ox = np.arange(res, dtype=np.float64)
    src_x = np.clip((left_px - origin_x) + ox * (right_px - left_px) / (res - 1), 0.0, source_heights.shape[1] - 1.0001)
    src_y = np.clip((top_px - origin_y) + ox * (bottom_px - top_px) / (res - 1), 0.0, source_heights.shape[0] - 1.0001)
    yy, xx = np.meshgrid(src_y, src_x, indexing="ij")
    sampled = map_coordinates(source_heights, [yy.ravel(), xx.ravel()], order=1, mode="nearest").reshape(res, res)

    if not offset_args["no_offset"]:
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
    parser.add_argument("--token", type=str, default=None, help="Mapbox API token. Overrides MAPBOX_TOKEN and config.json.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config.json containing mapbox_token.")
    parser.add_argument("--no-gutter", action="store_true", help="Crop output to 512x512 instead of 513x513.")
    parser.add_argument("--veg", type=int, choices=range(8), default=None, help="Vegetation preset 0-7 for the whole tile.")
    parser.add_argument("--no-nlcd", action="store_true", help="Skip NLCD land cover fetch.")
    parser.add_argument(
        "--nlcd-blur",
        type=float,
        default=NLCD_BLUR_SIGMA,
        help=f"Gaussian blur sigma for NLCD smoothing (default: {NLCD_BLUR_SIGMA}).",
    )
    parser.add_argument("--no-offset", action="store_true", help="Disable the west-to-east height offset ramp.")
    parser.add_argument("--offset-east-x", type=int, default=-66, help="East X boundary of the offset ramp (default: -66).")
    parser.add_argument("--offset-west-x", type=int, default=-98, help="West X boundary of the offset ramp (default: -98).")
    parser.add_argument(
        "--offset-max",
        type=float,
        default=40.0,
        help="Maximum height offset in metres at the west edge (default: 40.0).",
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

    args = parser.parse_args()
    token = resolve_mapbox_token(args.token, args.config)

    offset_params = {
        "no_offset": args.no_offset,
        "east": args.offset_east_x,
        "west": args.offset_west_x,
        "max": args.offset_max,
    }
    heights_m = sample_game_tile_heights(args.x, args.y, token, offset_params)

    veg_grid = water_grid = None
    if args.veg is None and not args.no_nlcd:
        (min_lat, min_lon), (max_lat, max_lon) = tile_position_to_latlon_bounds(args.x, args.y)
        nlcd_img = fetch_nlcd_landcover(min_lat, min_lon, max_lat, max_lon)
        veg_grid, water_grid = build_veg_water_grid(nlcd_img, HEIGHT_RESOLUTION, args.nlcd_blur, NLCD_GUTTER_PX)

    rgba = pack_to_rgba(heights_m, args.veg, veg_grid, water_grid)
    if args.no_gutter:
        rgba = rgba.crop((0, 0, 512, 512))
    output_name = build_output_filename(args.x, args.y, args.base_x, args.base_y)
    rgba.save(output_name, format="PNG")
    print(f"Saved {output_name}")


if __name__ == "__main__":
    main()
