import unittest
import json
import math
import tempfile

from map_profiles import load_map_profile
from scan_dem import (
    calculate_uniform_offset,
    load_world_tile_list,
    profile_scan_bounds,
    source_tiles,
    source_windows_for_world_tiles,
    world_tile_bounds,
)
from prepare_map_package import (
    package_documents,
    starter_game_graph_document,
    track_authoring_bridge_document,
)
from build_route_footprint import build_way_query, east_broad_top_main_way, manual_line_segments

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DemScanTests(unittest.TestCase):
    def test_rutland_1948_production_footprint_and_scan_are_locked(self):
        tile_list = json.loads(
            (PROJECT_ROOT / "routes" / "rutland-railroad-1948-tiles.json").read_text(encoding="utf-8")
        )
        report = json.loads(
            (PROJECT_ROOT / "reports" / "rutland-railroad-1948-dem-scan.json").read_text(encoding="utf-8")
        )

        self.assertEqual("rutland-railroad-1948", tile_list["profile"])
        self.assertEqual(6.0, tile_list["corridorWidthTiles"])
        self.assertEqual(3541, tile_list["tileCount"])
        self.assertEqual(3541, len(tile_list["tiles"]))
        self.assertTrue(report["productionReady"])
        self.assertEqual(0.0, report["elevation"]["sourceMinimumMeters"])
        self.assertAlmostEqual(824.900390625, report["elevation"]["sourceMaximumMeters"])
        self.assertEqual(505.0, report["heightOffset"]["recommendedMeters"])
        self.assertAlmostEqual(1329.900390625, report["heightOffset"]["shiftedMaximumMeters"])

    def test_way_query_uses_exact_tag_values_and_declared_bounds(self):
        query = build_way_query(
            {
                "bounds": {
                    "westLongitude": -73.27,
                    "southLatitude": 42.86,
                    "eastLongitude": -72.9,
                    "northLatitude": 43.62,
                },
                "tags": {
                    "railway": ["rail", "abandoned"],
                    "name": ["B&R Main Line", "Bennington Branch"],
                },
            }
        )

        self.assertIn('["railway"~"^(rail|abandoned)$"]', query)
        self.assertIn('["name"~"^(B&R Main Line|Bennington Branch)$"]', query)
        self.assertIn("(42.86,-73.27,43.62,-72.9)", query)

    def test_manual_historical_line_is_split_into_corridor_segments(self):
        features = manual_line_segments(
            {
                "manualLines": [
                    {
                        "name": "period branch",
                        "source": "historical map",
                        "accuracy": "terrain only",
                        "coordinates": [[-73.0, 43.0], [-73.1, 43.1], [-73.2, 43.2]],
                    }
                ]
            }
        )

        self.assertEqual(2, len(features))
        self.assertEqual("period branch", features[0]["properties"]["name"])
        self.assertEqual([(-73.0, 43.0), (-73.1, 43.1)], features[0]["geometry"]["coordinates"])

    def test_prr_profile_defines_production_scan_footprint(self):
        profile = load_map_profile("prr-middle-division", PROJECT_ROOT)
        bounds = profile_scan_bounds(profile)

        self.assertEqual(-78.96, bounds.west)
        self.assertEqual(40.15, bounds.south)
        self.assertEqual(-76.84, bounds.east)
        self.assertEqual(40.70, bounds.north)
        self.assertEqual(15, profile.dem_scan.scan_zoom)
        self.assertEqual(505.0, profile.dem_scan.target_min_meters)
        self.assertEqual(1495.0, profile.dem_scan.target_max_meters)
        self.assertGreater(len(source_tiles(bounds, 15, 256)), 1000)

    def test_recommended_offset_rounds_up_to_a_whole_meter(self):
        result = calculate_uniform_offset(91.4, 667.2, 505.0, 1495.0)

        self.assertEqual(414.0, result["recommendedMeters"])
        self.assertAlmostEqual(505.4, result["shiftedMinimumMeters"])
        self.assertAlmostEqual(1081.2, result["shiftedMaximumMeters"])
        self.assertTrue(result["uniformOffsetFeasible"])

    def test_offset_reports_when_uniform_shift_cannot_fit(self):
        result = calculate_uniform_offset(0.0, 1200.0, 505.0, 1495.0)

        self.assertFalse(result["uniformOffsetFeasible"])
        self.assertLess(result["maximumAllowedMeters"], result["recommendedMeters"])

    def test_world_tile_bounds_invert_the_profile_local_projection(self):
        profile = load_map_profile("prr-middle-division", PROJECT_ROOT)

        bounds = world_tile_bounds(profile, 0, 0)

        expected_west = profile.origin_longitude + profile.origin_east_bias_meters / (
            111111.0 * math.cos(math.radians(profile.origin_latitude))
        )
        expected_south = (
            profile.origin_latitude + profile.origin_north_bias_meters / 111111.0
        )
        self.assertAlmostEqual(expected_west, bounds.west)
        self.assertAlmostEqual(expected_south, bounds.south)
        self.assertGreater(bounds.east, bounds.west)
        self.assertGreater(bounds.north, bounds.south)

    def test_sparse_world_tiles_share_source_tiles_without_scanning_the_rectangle(self):
        profile = load_map_profile("prr-middle-division", PROJECT_ROOT)
        world_tiles = [(0, 0), (1, 0), (2, 0)]

        overall, windows = source_windows_for_world_tiles(profile, world_tiles, 15, 256)

        individual_count = sum(
            len(source_tiles(world_tile_bounds(profile, x, y), 15, 256))
            for x, y in world_tiles
        )
        self.assertLess(len(windows), individual_count)
        self.assertAlmostEqual(world_tile_bounds(profile, 0, 0).west, overall.west)
        self.assertAlmostEqual(world_tile_bounds(profile, 2, 0).east, overall.east)
        self.assertTrue(all(bounds_list for bounds_list in windows.values()))

    def test_route_tile_list_is_deduplicated_and_profile_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "route-tiles.json"
            path.write_text(
                json.dumps(
                    {
                        "profile": "prr-middle-division",
                        "tiles": [{"x": -2, "y": 3}, {"x": -2, "y": 3}, {"x": -1, "y": 3}],
                    }
                ),
                encoding="utf-8",
            )

            tiles = load_world_tile_list(path, "prr-middle-division")

            self.assertEqual([(-2, 3), (-1, 3)], tiles)
            with self.assertRaisesRegex(ValueError, "not 'rutland-railroad'"):
                load_world_tile_list(path, "rutland-railroad")

    def test_prr_package_uses_signed_production_tile_range_and_spawn(self):
        profile = load_map_profile("prr-middle-division", PROJECT_ROOT)
        map_document, info_document, fuse_document = package_documents(
            profile,
            -120,
            -111,
            15,
            24,
            package_id="prr-middle-division-altoona-test",
            display_name="PRR Middle Division - Altoona Terrain Test",
            spawn_position=(-57250.0, 825.0, 10250.0),
        )
        self.assertEqual(100, len(map_document["tiles"]))
        self.assertTrue(fuse_document["map"]["suppressBaseWorld"])
        self.assertEqual(825.0, fuse_document["world"]["spawnPoints"][0]["position"]["y"])
        self.assertEqual(
            [
                "prr-middle-division-altoona-test.fuse.json",
                "prr-middle-division-altoona-test-tracks.fuse.json",
            ],
            info_document["FuseDataFiles"],
        )

    def test_track_authoring_documents_start_empty_and_reference_the_graph(self):
        graph = starter_game_graph_document()
        bridge = track_authoring_bridge_document("prr-middle-division", "PRR Middle Division")

        self.assertEqual({}, graph["tracks"]["nodes"])
        self.assertEqual({}, graph["tracks"]["segments"])
        self.assertEqual("game-graph", bridge["mixinto"]["target"])
        self.assertEqual("game-graph.json", bridge["mixinto"]["sourceFile"])
        self.assertIn("legacy-converted", bridge["tags"])

    def test_east_broad_top_branch_ways_are_excluded_from_production_corridor(self):
        self.assertTrue(
            east_broad_top_main_way(
                {"railway": "abandoned", "name": "East Broad Top Railroad", "usage": "main"}
            )
        )
        self.assertFalse(
            east_broad_top_main_way(
                {"railway": "abandoned", "name": "Rocky Ridge Branch", "usage": "branch"}
            )
        )


if __name__ == "__main__":
    unittest.main()
