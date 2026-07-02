# NLCD Mapbox Tile Script

Generate Railroader terrain tile `.data` files from Mapbox Terrain-RGB, with optional NLCD 2021 land-cover driven vegetation and water masking.

## What Is Included

- `get_tile_4.py`: generate a single tile
- `get_tile_area.py`: batch-generate a rectangular tile area in parallel
- `run_editor.bat`: interactive Windows launcher for setup and generation

## Requirements

- Python 3.10 or newer
- A Mapbox access token
- Python packages listed in `requirements.txt`

## Quick Start

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.json.example config.json
```

Edit `config.json` and replace the placeholder value with your real `mapbox_token`.

The token is resolved in this order:

1. `--token`
2. `MAPBOX_TOKEN` environment variable
3. `config.json`

## Usage

Generate a single tile:

```powershell
python get_tile_4.py -72 18 --base-x -72 --base-y 18
```

Generate a single tile without NLCD, forcing vegetation preset `3`:

```powershell
python get_tile_4.py -72 18 --base-x -72 --base-y 18 --veg 3 --no-gutter
```

Generate a rectangle of tiles in parallel:

```powershell
python get_tile_area.py -74 -72 18 20 --workers 8
```

On Windows, you can also run:

```powershell
.\run_editor.bat
```

## Output

Generated files are written to the current working directory using the game-safe `Tile_000_000.data` pattern.

Single-tile runs should pass `--base-x` and `--base-y` so the requested tile is remapped to a non-negative filename index.

`get_tile_area.py` defaults `--base-x` and `--base-y` to the start of the requested range, so a batch run begins at `Tile_000_000.data` automatically.

`Tile_*.data` outputs and local `config.json` are already ignored by git so the repository stays clean.

## Publishing Notes

- Add a `LICENSE` file before making the repository public so the usage terms are explicit.
- The included GitHub Actions workflow runs a lightweight dependency install and CLI/syntax check on pushes and pull requests.
