import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

from get_tile_area import _output_path, _valid_existing_output, run_one
from map_profiles import DEFAULT_PROFILE_NAME, MapProfileError, load_map_profile


DEFAULT_CACHE_PATH = Path(__file__).with_name("cache")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate only the signed terrain tiles listed in a FUSE Map.json.")
    parser.add_argument("map_json", type=Path)
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("get_tile_4.py"))
    parser.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=min(8, max(2, os.cpu_count() or 2)))
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--no-gutter", action="store_true")
    parser.add_argument("--veg", type=int, choices=range(8), default=None)
    parser.add_argument("--no-nlcd", action="store_true")
    parser.add_argument("--nlcd-blur", type=float, default=None)
    parser.add_argument("--no-offset", action="store_true")
    parser.add_argument("--height-offset", type=float, default=None)
    args = parser.parse_args()

    map_json = args.map_json.expanduser().resolve()
    script = args.script.expanduser().resolve()
    if not map_json.is_file():
        parser.error(f"Map.json was not found: {map_json}")
    if not script.is_file():
        parser.error(f"Tile generator was not found: {script}")
    try:
        profile = load_map_profile(args.profile, Path(__file__).resolve().parent)
    except MapProfileError as exc:
        parser.error(str(exc))

    document = json.loads(map_json.read_text(encoding="utf-8"))
    requested = sorted({(int(tile["x"]), int(tile["y"])) for tile in document.get("tiles", [])})
    if not requested:
        parser.error(f"Map manifest contains no tiles: {map_json}")
    output_directory = (args.output_dir or map_json.parent).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    config = args.config.expanduser().resolve() if args.config is not None else None
    cache = args.cache_dir.expanduser().resolve()
    expected_size = 512 if args.no_gutter else profile.height_resolution
    jobs = requested
    if args.skip_existing:
        jobs = [
            (x, y)
            for x, y in requested
            if not _valid_existing_output(_output_path(output_directory, x, y, 0, 0, True), expected_size)
        ]
    skipped = len(requested) - len(jobs)
    print(f"Profile: {profile.display_name} ({profile.profile_id})")
    print(f"Manifest: {map_json}")
    print(f"Output: {output_directory}")
    print(f"Requested {len(requested):,}; skipped {skipped:,}; generating {len(jobs):,} with {args.workers} workers")
    if not jobs:
        return 0

    failed = []
    progress_step = max(1, len(jobs) // 100)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                run_one,
                script,
                x,
                y,
                None,
                None,
                args.no_gutter,
                args.veg,
                args.no_nlcd,
                args.nlcd_blur,
                str(profile.source_path),
                args.no_offset,
                args.height_offset,
                None,
                None,
                None,
                None,
                cache,
                args.refresh_cache,
                config,
                output_directory,
                True,
                args.skip_existing,
            ): (x, y)
            for x, y in jobs
        }
        for completed, future in enumerate(as_completed(futures), 1):
            x, y = futures[future]
            try:
                _, _, code, error = future.result()
            except Exception as exc:
                code, error = 1, str(exc)
            if code != 0:
                failed.append((x, y, error.strip()))
                print(f"FAIL x={x} y={y}: {error.strip()}", file=sys.stderr)
            if completed % progress_step == 0 or completed == len(jobs):
                print(f"Completed {completed:,}/{len(jobs):,}; failures {len(failed)}")

    if failed:
        print(f"Generation failed for {len(failed)} tile(s). Rerun with --skip-existing to resume.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
