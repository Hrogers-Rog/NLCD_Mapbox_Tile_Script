# NLCD Mapbox Tile Script

Generate Railroader terrain tile `.data` files from Mapbox Terrain-RGB, with optional NLCD 2021 land-cover-driven vegetation and water masking.

## Map profiles

Map-specific projection and height settings live in tracked JSON files under `profiles/`. Credentials are deliberately separate.

- `bushnell-whittier` is the default and preserves every setting that existed before profile support: origin, 8 m coordinate biases, zoom 15, the 500–1500 m encoding range, 513-pixel output, and the west-to-east 0–40 m height ramp.
- `prr-middle-division` uses the planned `40.425, -77.715` origin and the expanded-footprint uniform `+505 m` height offset. The route scope is Johnstown-Altoona-Harrisburg plus the East Broad Top main line, exported as a sparse corridor approximately ten 500 m tiles wide.
- `rutland-railroad-1948` uses `43.678728, -72.838300` for Burlington-Rutland, Rutland-Bellows Falls, Rutland-Bennington, and the period Clarendon & Pittsford network. Its six-tile (3 km) sparse corridor uses a measured uniform `+505 m` offset.

Select a profile explicitly on the command line:

```powershell
python get_tile_4.py 0 0 --profile prr-middle-division
python get_tile_area.py --profile prr-middle-division -1 1 -1 1 --workers 8
```

Omitting `--profile` always selects `bushnell-whittier`, preserving the original behavior. `run_editor.bat` starts on Bushnell and offers a session-only profile selector. Switching profiles never edits either profile file.

## Credentials

Create an ignored local `config.json` beside the scripts:

```json
{
  "mapbox_token": "YOUR_MAPBOX_TOKEN"
}
```

The token is resolved in this order:

1. `--token`
2. `MAPBOX_TOKEN` environment variable
3. `config.json`

The interactive launcher reads `config.json` internally and does not place the token in the displayed child-process command.

## What is included

- `get_tile_4.py`: generate a single tile
- `get_tile_area.py`: batch-generate a rectangular tile area in parallel
- `map_profiles.py`: load and validate map-specific settings
- `terrain_rgb.py`: validated, retried, concurrency-safe source-tile cache
- `scan_dem.py`: scan a profile footprint and calculate/persist its safe height offset
- `prepare_map_package.py`: create signed FUSE map manifests and restartable export plans
- `build_route_footprint.py`: cache OSM route relations and select the sparse corridor tiles
- `get_tile_list.py`: resumably generate only the tiles listed by a sparse Map.json
- `stitch_tile_edges.py`: make vegetation/water edge samples exactly continuous
- `validate_map_package.py`: verify every file, height range, and shared edge
- `profiles/*.json`: tracked, token-free map profiles
- `run_editor.bat`: interactive Windows launcher for setup and generation

## Requirements

- Python 3.10 or newer
- A Mapbox access token
- Python packages listed in `requirements.txt`

## Source-tile cache

Mapbox Terrain-RGB source tiles are stored under `cache/mapbox-terrain-rgb-v1/<zoom>/<x>/<y>.pngraw`. The cache is shared by the scanner, single-tile generator, and all batch workers. Downloads are validated as PNGs, retried for throttling and server failures, written atomically, and protected by cross-process lock files. `cache/` is ignored by Git.

Use `--cache-dir D:\somewhere\terrain-cache` to put the cache on another disk. Use `--refresh-cache` only when source tiles must be downloaded again.

## PRR DEM calibration gate

First inspect the source request count without using the token or downloading anything:

```powershell
python scan_dem.py --profile prr-middle-division --estimate-only
```

The expanded Johnstown-Harrisburg/EBT scan found a 0.0-960.1 m rectangular-envelope source range and selected a conservative +505 m offset, giving a safe 505.0-1465.1 m envelope. The generated sparse railway corridor itself validates at 518.84-1320.89 m with no clipped pixels.

```powershell
python scan_dem.py --profile prr-middle-division --confirm-large-scan --update-profile
```

The rectangular scan caches every source tile, considers only pixels inside `demScan.bounds`, writes `reports/prr-middle-division-dem-scan.json`, verifies that one uniform shift fits the safe 505-1495 m range, and replaces the provisional profile offset only when the result is feasible. Interrupted runs can be repeated; valid cached tiles are reused.

Once `build_route_footprint.py` has produced the signed Railroader tile list, use `--tile-list` to calibrate against the sparse production corridor instead of all terrain inside its rectangular envelope:

```powershell
python scan_dem.py --profile prr-middle-division --tile-list routes\prr-main-ebt-tiles.json --estimate-only
python scan_dem.py --profile prr-middle-division --tile-list routes\prr-main-ebt-tiles.json --confirm-large-scan --update-profile
```

Sparse scanning converts every selected 500 m Railroader tile back to geographic bounds, downloads each overlapping Terrain-RGB source tile only once, and masks out pixels belonging to gaps in the route footprint. This is the production calibration mode for narrow railway maps.

If the production footprint changes, edit `demScan.bounds` in the PRR profile and rerun the estimate and scan. Existing overlapping source tiles remain reusable.

## Rutland 1945-1948 corridor and DEM gate

The production route footprint contains 3,541 signed 500 m terrain tiles in a six-tile-wide corridor. The exact sparse scan measured `0.0-824.9 m`; the locked `+505 m` shift produces `505.0-1329.9 m`, safely inside the `500-1500 m` game range. An eight-tile trial was rejected because its `1039.0 m` maximum would have shifted to `1544.0 m`.

The C&P supplemental lines are deliberately labeled as terrain-footprint planning geometry. They ensure the quarry, Proctor, Center Rutland, West Rutland, and Rutland connection areas receive terrain, but they are not the later track-laying centerline.

```powershell
python build_route_footprint.py --profile rutland-railroad-1948
python scan_dem.py --profile rutland-railroad-1948 --tile-list routes\rutland-railroad-1948-tiles.json --estimate-only
python scan_dem.py --profile rutland-railroad-1948 --tile-list routes\rutland-railroad-1948-tiles.json --update-profile
```

## Sparse production corridor

The cached OSM Main Line and East Broad Top relations produce one connected 5 km-wide footprint containing 6,565 terrain tiles. EBT branches are excluded. The completed package is under `maps/PRRMiddleDivision`.

```powershell
python build_route_footprint.py --profile prr-middle-division
python get_tile_list.py maps\PRRMiddleDivision\Map\Map.json --profile prr-middle-division --config Tile_Script\config.json --cache-dir cache --output-dir maps\PRRMiddleDivision\Map --skip-existing --workers 8
python stitch_tile_edges.py maps\PRRMiddleDivision\Map\Map.json
python validate_map_package.py maps\PRRMiddleDivision\Map\Map.json
```

## Altoona terrain test

The first in-game gate is an isolated 10 by 10 tile block centered on Altoona (`x=-120..-111`, `y=15..24`). It uses signed FUSE filenames, NLCD vegetation/water, atomic tile writes, a restart-safe export, and an exact vegetation seam pass. The prepared package lives under `maps/PRRMiddleDivisionAltoonaTest` and is installed separately from `FuseTestMap`.

```powershell
python get_tile_area.py -120 -111 15 24 --profile prr-middle-division --config Tile_Script\config.json --cache-dir cache --output-dir maps\PRRMiddleDivisionAltoonaTest\Map --signed-filenames --skip-existing --workers 8
python stitch_tile_edges.py maps\PRRMiddleDivisionAltoonaTest\Map\Map.json
```

## Height-offset overrides

The active profile supplies the normal offset. Temporary CLI overrides do not alter the profile:

```powershell
# Disable the profile offset for one run
python get_tile_4.py 0 0 --profile prr-middle-division --no-offset

# Replace it with a uniform offset for one run
python get_tile_4.py 0 0 --profile prr-middle-division --height-offset 435

# Use the legacy linear-X controls
python get_tile_4.py -72 18 --offset-east-x -66 --offset-west-x -98 --offset-max 40
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

The tests lock down the original Bushnell values and verify that the PRR profile can change independently.
