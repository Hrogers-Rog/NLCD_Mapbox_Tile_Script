import io
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image
import requests

from terrain_rgb import TerrainRgbCache, decode_terrain_rgb


class FakeResponse:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


def terrain_png(values):
    values = np.asarray(values, dtype=np.float64)
    encoded = np.rint((values + 10000.0) * 10.0).astype(np.uint32)
    rgb = np.stack(
        [
            ((encoded >> 16) & 0xFF).astype(np.uint8),
            ((encoded >> 8) & 0xFF).astype(np.uint8),
            (encoded & 0xFF).astype(np.uint8),
        ],
        axis=2,
    )
    output = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(output, format="PNG")
    return output.getvalue()


class TerrainRgbCacheTests(unittest.TestCase):
    def test_download_is_cached_and_reused(self):
        content = terrain_png([[100.0, 101.0], [102.0, 103.0]])
        session = FakeSession([FakeResponse(200, content)])
        with tempfile.TemporaryDirectory() as directory:
            cache = TerrainRgbCache(Path(directory), "test-token", tile_size=2, session=session)

            first = cache.get_tile(3, 1, 2)
            second = cache.get_tile(3, 1, 2)

            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(1, len(session.calls))
            np.testing.assert_allclose(decode_terrain_rgb(second.image), [[100, 101], [102, 103]], atol=0.01)

    def test_corrupt_cache_entry_is_replaced(self):
        content = terrain_png([[10.0, 20.0], [30.0, 40.0]])
        session = FakeSession([FakeResponse(200, content)])
        with tempfile.TemporaryDirectory() as directory:
            cache = TerrainRgbCache(Path(directory), "test-token", tile_size=2, session=session)
            path = cache.tile_path(3, 1, 2)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not a png")

            result = cache.get_tile(3, 1, 2)

            self.assertFalse(result.cache_hit)
            self.assertEqual(1, len(session.calls))
            self.assertGreater(path.stat().st_size, len(b"not a png"))

    def test_throttled_request_uses_retry_after_then_succeeds(self):
        content = terrain_png([[1.0, 2.0], [3.0, 4.0]])
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "0"}),
                FakeResponse(200, content),
            ]
        )
        delays = []
        with tempfile.TemporaryDirectory() as directory:
            cache = TerrainRgbCache(
                Path(directory),
                "test-token",
                tile_size=2,
                session=session,
                sleep=delays.append,
            )

            cache.get_tile(3, 1, 2)

            self.assertEqual(2, len(session.calls))
            self.assertEqual([0.0], delays)


if __name__ == "__main__":
    unittest.main()
