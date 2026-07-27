import io
import math
import os
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
import tempfile
import time

import numpy as np
from PIL import Image, UnidentifiedImageError
import requests


TERRAIN_RGB_TILESET = "mapbox.terrain-rgb"
TERRAIN_RGB_CACHE_NAMESPACE = "mapbox-terrain-rgb-v1"
TERRAIN_RGB_URL = "https://api.mapbox.com/v4/mapbox.terrain-rgb/{zoom}/{x}/{y}.pngraw"


class TerrainRgbError(RuntimeError):
    """Raised when a Terrain-RGB source tile cannot be loaded safely."""


@dataclass(frozen=True)
class GeographicBounds:
    west: float
    south: float
    east: float
    north: float


@dataclass(frozen=True)
class TerrainTileResult:
    image: Image.Image
    cache_hit: bool
    path: Path


class TerrainRgbCache:
    def __init__(
        self,
        root: Path,
        token: str,
        tile_size: int = 256,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        lock_timeout_seconds: float = 120.0,
        stale_lock_seconds: float = 300.0,
        session=None,
        sleep=time.sleep,
    ):
        self.root = Path(root)
        self.token = token
        self.tile_size = tile_size
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.lock_timeout_seconds = lock_timeout_seconds
        self.stale_lock_seconds = stale_lock_seconds
        self.session = session or requests.Session()
        self.sleep = sleep

    def tile_path(self, zoom: int, x: int, y: int) -> Path:
        return self.root / TERRAIN_RGB_CACHE_NAMESPACE / str(zoom) / str(x) / f"{y}.pngraw"

    def get_tile(self, zoom: int, x: int, y: int, refresh: bool = False) -> TerrainTileResult:
        limit = 2 ** zoom
        if not (0 <= x < limit and 0 <= y < limit):
            raise TerrainRgbError(f"Terrain-RGB tile is outside zoom {zoom}: x={x}, y={y}")

        path = self.tile_path(zoom, x, y)
        if not refresh:
            cached = self._read_cached(path)
            if cached is not None:
                return TerrainTileResult(cached, True, path)

        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        deadline = time.monotonic() + self.lock_timeout_seconds
        acquired = False
        while not acquired:
            try:
                descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(f"pid={os.getpid()}\n")
                acquired = True
            except FileExistsError:
                if not refresh:
                    cached = self._read_cached(path)
                    if cached is not None:
                        return TerrainTileResult(cached, True, path)
                self._remove_stale_lock(lock_path)
                if time.monotonic() >= deadline:
                    raise TerrainRgbError(f"Timed out waiting for cache lock: {lock_path}")
                self.sleep(0.1)

        try:
            if not refresh:
                cached = self._read_cached(path)
                if cached is not None:
                    return TerrainTileResult(cached, True, path)

            content = self._download(zoom, x, y)
            image = self._decode_and_validate(content, path)
            self._write_atomic(path, content)
            return TerrainTileResult(image, False, path)
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _read_cached(self, path: Path) -> Image.Image | None:
        if not path.exists():
            return None
        try:
            content = path.read_bytes()
            return self._decode_and_validate(content, path)
        except (OSError, UnidentifiedImageError, TerrainRgbError):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return None

    def _decode_and_validate(self, content: bytes, path: Path) -> Image.Image:
        try:
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                if source.size != (self.tile_size, self.tile_size):
                    raise TerrainRgbError(
                        f"Unexpected Terrain-RGB dimensions in {path}: {source.size}; "
                        f"expected {(self.tile_size, self.tile_size)}"
                    )
                return source.convert("RGB").copy()
        except (OSError, UnidentifiedImageError) as exc:
            raise TerrainRgbError(f"Invalid Terrain-RGB PNG received for {path}") from exc

    def _download(self, zoom: int, x: int, y: int) -> bytes:
        url = TERRAIN_RGB_URL.format(zoom=zoom, x=x, y=y)
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            response = None
            retry_delay = min(2 ** (attempt - 1), 8)
            try:
                response = self.session.get(
                    url,
                    params={"access_token": self.token},
                    timeout=self.timeout_seconds,
                )
                if response.status_code == 200:
                    return response.content
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    retry_delay = _retry_after_seconds(response.headers.get("Retry-After"), retry_delay)
                    last_error = TerrainRgbError(
                        f"Mapbox returned HTTP {response.status_code} for z={zoom} x={x} y={y}"
                    )
                else:
                    try:
                        response.raise_for_status()
                    except requests.RequestException as exc:
                        raise TerrainRgbError(
                            f"Mapbox rejected Terrain-RGB tile z={zoom} x={x} y={y} "
                            f"with HTTP {response.status_code}"
                        ) from exc
            except requests.RequestException as exc:
                last_error = exc

            if attempt < self.max_attempts:
                self.sleep(retry_delay)

        raise TerrainRgbError(
            f"Unable to download Terrain-RGB tile z={zoom} x={x} y={y} "
            f"after {self.max_attempts} attempts: {last_error}"
        )

    def _write_atomic(self, path: Path, content: bytes) -> None:
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=path.name + ".",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass

    def _remove_stale_lock(self, lock_path: Path) -> None:
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > self.stale_lock_seconds:
                lock_path.unlink()
        except FileNotFoundError:
            pass


def decode_terrain_rgb(image: Image.Image) -> np.ndarray:
    array = np.asarray(image.convert("RGB"), dtype=np.float32)
    return -10000.0 + 0.1 * (
        array[:, :, 0] * 65536.0 + array[:, :, 1] * 256.0 + array[:, :, 2]
    )


def lon_to_world_px(longitude: float, zoom: int, tile_size: int = 256) -> float:
    return (longitude + 180.0) / 360.0 * tile_size * (2 ** zoom)


def lat_to_world_py(latitude: float, zoom: int, tile_size: int = 256) -> float:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    latitude_radians = math.radians(latitude)
    mercator = math.log(math.tan(math.pi / 4.0 + latitude_radians / 2.0))
    return (1.0 - mercator / math.pi) / 2.0 * tile_size * (2 ** zoom)


def world_px_to_lon(world_x: float, zoom: int, tile_size: int = 256) -> float:
    return world_x / (tile_size * (2 ** zoom)) * 360.0 - 180.0


def world_py_to_lat(world_y: float, zoom: int, tile_size: int = 256) -> float:
    normalized = math.pi * (1.0 - 2.0 * world_y / (tile_size * (2 ** zoom)))
    return math.degrees(math.atan(math.sinh(normalized)))


def tile_range_for_bounds(bounds: GeographicBounds, zoom: int, tile_size: int = 256) -> tuple[int, int, int, int]:
    left = lon_to_world_px(bounds.west, zoom, tile_size)
    right = lon_to_world_px(bounds.east, zoom, tile_size)
    top = lat_to_world_py(bounds.north, zoom, tile_size)
    bottom = lat_to_world_py(bounds.south, zoom, tile_size)
    x0 = math.floor(left / tile_size)
    x1 = math.floor(math.nextafter(right, -math.inf) / tile_size)
    y0 = math.floor(top / tile_size)
    y1 = math.floor(math.nextafter(bottom, -math.inf) / tile_size)
    return x0, x1, y0, y1


def pixel_window_for_tile(
    bounds: GeographicBounds,
    zoom: int,
    tile_x: int,
    tile_y: int,
    tile_size: int = 256,
) -> tuple[int, int, int, int]:
    left = lon_to_world_px(bounds.west, zoom, tile_size) - tile_x * tile_size
    right = lon_to_world_px(bounds.east, zoom, tile_size) - tile_x * tile_size
    top = lat_to_world_py(bounds.north, zoom, tile_size) - tile_y * tile_size
    bottom = lat_to_world_py(bounds.south, zoom, tile_size) - tile_y * tile_size

    x0 = max(0, math.ceil(left - 0.5))
    x1 = min(tile_size, math.floor(right - 0.5) + 1)
    y0 = max(0, math.ceil(top - 0.5))
    y1 = min(tile_size, math.floor(bottom - 0.5) + 1)
    return x0, x1, y0, y1


def pixel_center_to_lonlat(
    zoom: int,
    tile_x: int,
    tile_y: int,
    pixel_x: int,
    pixel_y: int,
    tile_size: int = 256,
) -> tuple[float, float]:
    world_x = tile_x * tile_size + pixel_x + 0.5
    world_y = tile_y * tile_size + pixel_y + 0.5
    return world_px_to_lon(world_x, zoom, tile_size), world_py_to_lat(world_y, zoom, tile_size)


def _retry_after_seconds(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default
