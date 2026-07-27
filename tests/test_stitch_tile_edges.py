import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from stitch_tile_edges import stitch_map


class StitchTileEdgesTests(unittest.TestCase):
    def test_stitch_copies_only_canonical_alpha_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_json = root / "Map.json"
            map_json.write_text(
                json.dumps({"tiles": [{"x": 0, "y": 0}, {"x": 1, "y": 0}]}),
                encoding="utf-8",
            )
            west = np.zeros((513, 513, 4), dtype=np.uint8)
            east = np.zeros((513, 513, 4), dtype=np.uint8)
            west[:, :, :3] = 11
            east[:, :, :3] = 22
            west[:, -1, 3] = 7
            east[:, 0, 3] = 9
            Image.fromarray(west, mode="RGBA").save(root / "tile_000_000.data", format="PNG")
            Image.fromarray(east, mode="RGBA").save(root / "tile_001_000.data", format="PNG")

            changed_tiles, changed_samples = stitch_map(map_json)

            with Image.open(root / "tile_001_000.data") as image:
                stitched = np.asarray(image).copy()
            self.assertEqual(1, changed_tiles)
            self.assertEqual(513, changed_samples)
            self.assertTrue(np.all(stitched[:, 0, 3] == 7))
            self.assertTrue(np.all(stitched[:, :, :3] == 22))


if __name__ == "__main__":
    unittest.main()
