import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import get_tile_4
import get_tile_area
from map_profiles import DEFAULT_PROFILE_NAME, load_map_profile


class MapProfileTests(unittest.TestCase):
    def test_default_profile_preserves_existing_bushnell_settings(self):
        profile = load_map_profile(None, PROJECT_ROOT)

        self.assertEqual(DEFAULT_PROFILE_NAME, profile.profile_id)
        self.assertEqual(35.382614, profile.origin_latitude)
        self.assertEqual(-83.49541, profile.origin_longitude)
        self.assertEqual(8.0, profile.origin_east_bias_meters)
        self.assertEqual(-8.0, profile.origin_north_bias_meters)
        self.assertEqual(500.0, profile.tile_dimension_meters)
        self.assertEqual(15, profile.mapbox_zoom)
        self.assertEqual(256, profile.mapbox_tile_size)
        self.assertEqual(500.0, profile.height_min_meters)
        self.assertEqual(1500.0, profile.height_max_meters)
        self.assertEqual(513, profile.height_resolution)
        self.assertEqual("linear_x", profile.height_offset.mode)
        self.assertEqual(-66.0, profile.height_offset.east_tile_x)
        self.assertEqual(-98.0, profile.height_offset.west_tile_x)
        self.assertEqual(40.0, profile.height_offset.max_meters)

    def test_prr_profile_is_independent_from_bushnell(self):
        profile = load_map_profile("prr-middle-division", PROJECT_ROOT)

        self.assertEqual("prr-middle-division", profile.profile_id)
        self.assertEqual(40.425, profile.origin_latitude)
        self.assertEqual(-77.715, profile.origin_longitude)
        self.assertEqual("uniform", profile.height_offset.mode)
        self.assertEqual(505.0, profile.height_offset.meters)

    def test_rutland_1948_profile_keeps_the_new_center_and_scanned_offset_separate(self):
        profile = load_map_profile("rutland-railroad-1948", PROJECT_ROOT)

        self.assertEqual("rutland-railroad-1948", profile.profile_id)
        self.assertEqual(43.678728, profile.origin_latitude)
        self.assertEqual(-72.8383, profile.origin_longitude)
        self.assertEqual("uniform", profile.height_offset.mode)
        self.assertEqual(505.0, profile.height_offset.meters)
        self.assertEqual(505.0, profile.dem_scan.target_min_meters)
        self.assertEqual(1495.0, profile.dem_scan.target_max_meters)

    def test_existing_rutland_alburgh_profile_is_unchanged(self):
        profile = load_map_profile("rutland-railroad", PROJECT_ROOT)

        self.assertEqual(44.057, profile.origin_latitude)
        self.assertEqual(-72.872, profile.origin_longitude)
        self.assertEqual(477.0, profile.height_offset.meters)

    def test_profile_offset_is_used_when_cli_does_not_override_it(self):
        profile = load_map_profile("bushnell-whittier", PROJECT_ROOT)
        args = self._offset_args()

        offset = get_tile_4.resolve_height_offset(profile, args)

        self.assertEqual(
            {"mode": "linear_x", "east": -66.0, "west": -98.0, "max": 40.0},
            offset,
        )

    def test_uniform_cli_offset_can_temporarily_override_profile(self):
        profile = load_map_profile("bushnell-whittier", PROJECT_ROOT)
        args = self._offset_args(height_offset=425.0)

        offset = get_tile_4.resolve_height_offset(profile, args)

        self.assertEqual({"mode": "uniform", "meters": 425.0}, offset)

    def test_signed_filename_matches_fuse_map_convention(self):
        self.assertEqual(
            "tile_-142_054.data",
            get_tile_4.build_output_filename(-142, 54, None, None, signed_filenames=True),
        )

    def test_nlcd_gutter_expands_geographic_request_without_changing_tile_center(self):
        expanded = get_tile_4.expand_nlcd_bounds(40.0, -78.0, 40.5, -77.5, gutter_px=48)

        self.assertLess(expanded[0], 40.0)
        self.assertLess(expanded[1], -78.0)
        self.assertGreater(expanded[2], 40.5)
        self.assertGreater(expanded[3], -77.5)
        self.assertAlmostEqual(40.25, (expanded[0] + expanded[2]) / 2.0)
        self.assertAlmostEqual(-77.75, (expanded[1] + expanded[3]) / 2.0)

    def test_batch_child_command_passes_profile_without_token_when_using_config(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(get_tile_area.subprocess, "run", return_value=completed) as run:
            get_tile_area.run_one(
                PROJECT_ROOT / "get_tile_4.py",
                0,
                0,
                0,
                0,
                False,
                None,
                True,
                None,
                str(PROJECT_ROOT / "profiles" / "prr-middle-division.json"),
                False,
                None,
                None,
                None,
                None,
                None,
                PROJECT_ROOT / "cache",
                False,
                PROJECT_ROOT / "Tile_Script" / "config.json",
                PROJECT_ROOT / "maps" / "PRRMiddleDivision" / "Map",
                True,
                True,
            )

        command = run.call_args.args[0]
        self.assertIn("--profile", command)
        self.assertNotIn("--token", command)
        self.assertIn("--config", command)
        self.assertIn("--output-dir", command)
        self.assertIn("--signed-filenames", command)
        self.assertIn("--skip-existing", command)

    @staticmethod
    def _offset_args(**overrides):
        values = {
            "no_offset": False,
            "height_offset": None,
            "offset_east_x": None,
            "offset_west_x": None,
            "offset_max": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)


if __name__ == "__main__":
    unittest.main()
