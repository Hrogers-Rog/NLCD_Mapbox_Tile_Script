import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def run_one(
    script: Path,
    x: int,
    y: int,
    base_x: int | None,
    base_y: int | None,
    no_gutter: bool,
    veg: int | None,
    no_nlcd: bool,
    nlcd_blur: float | None,
    no_offset: bool,
    offset_east_x: int | None,
    offset_west_x: int | None,
    offset_max: float | None,
    token: str | None = None,
):
    cmd = [sys.executable, str(script), str(x), str(y)]
    if base_x is not None:
        cmd += ["--base-x", str(base_x)]
    if base_y is not None:
        cmd += ["--base-y", str(base_y)]
    if no_gutter:
        cmd += ["--no-gutter"]
    if veg is not None:
        cmd += ["--veg", str(veg)]
    if no_nlcd:
        cmd += ["--no-nlcd"]
    if nlcd_blur is not None:
        cmd += ["--nlcd-blur", str(nlcd_blur)]
    if no_offset:
        cmd += ["--no-offset"]
    if offset_east_x is not None:
        cmd += ["--offset-east-x", str(offset_east_x)]
    if offset_west_x is not None:
        cmd += ["--offset-west-x", str(offset_west_x)]
    if offset_max is not None:
        cmd += ["--offset-max", str(offset_max)]
    if token:
        cmd += ["--token", token]

    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return x, y, (result.returncode if result.returncode is not None else 1), result.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-run the tile generator over a game-tile rectangle in parallel.")
    parser.add_argument("--script", default="get_tile_4.py", help="Path to the tile generator script.")
    parser.add_argument("x0", type=int, help="Start x (inclusive)")
    parser.add_argument("x1", type=int, help="End x (inclusive)")
    parser.add_argument("y0", type=int, help="Start y (inclusive)")
    parser.add_argument("y1", type=int, help="End y (inclusive)")
    parser.add_argument(
        "--base-x",
        type=int,
        default=None,
        help="Output filename origin for X. Defaults to the start of the requested range.",
    )
    parser.add_argument(
        "--base-y",
        type=int,
        default=None,
        help="Output filename origin for Y. Defaults to the start of the requested range.",
    )
    parser.add_argument("--no-gutter", action="store_true", help="Pass through --no-gutter")
    parser.add_argument("--veg", type=int, choices=range(0, 8), default=None, help="Vegetation preset 0-7 for the whole tile")
    parser.add_argument("--no-nlcd", action="store_true", help="Skip NLCD land cover fetch")
    parser.add_argument(
        "--nlcd-blur",
        type=float,
        default=None,
        help="Per-class blur sigma passed through to the generator (omit to use the generator default).",
    )
    parser.add_argument("--no-offset", action="store_true", help="Disable linear height offset ramp")
    parser.add_argument("--offset-east-x", type=int, default=None, help="East X boundary of offset ramp")
    parser.add_argument("--offset-west-x", type=int, default=None, help="West X boundary of offset ramp")
    parser.add_argument("--offset-max", type=float, default=None, help="Max height offset in metres at west end")
    parser.add_argument("--token", type=str, default=None, help="Mapbox API token (overrides env/config)")
    parser.add_argument("--workers", type=int, default=max(4, (os.cpu_count() or 4)), help="Parallel workers")
    args = parser.parse_args()

    script_path = Path(args.script).expanduser()
    if not script_path.exists():
        print(f"Error: script not found: {script_path}", file=sys.stderr)
        return 2

    x_min, x_max = sorted((args.x0, args.x1))
    y_min, y_max = sorted((args.y0, args.y1))
    base_x = args.base_x if args.base_x is not None else x_min
    base_y = args.base_y if args.base_y is not None else y_min

    jobs: list[tuple[int, int]] = [(x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)]
    total = len(jobs)

    print(f"Running {total} tiles with {args.workers} workers...")

    failed: list[tuple[int, int, int]] = []
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_one,
                script_path,
                x,
                y,
                base_x,
                base_y,
                args.no_gutter,
                args.veg,
                args.no_nlcd,
                args.nlcd_blur,
                args.no_offset,
                args.offset_east_x,
                args.offset_west_x,
                args.offset_max,
                args.token,
            ): (x, y)
            for (x, y) in jobs
        }
        for future in as_completed(futures):
            x, y = futures[future]
            try:
                _, _, code, err = future.result()
            except Exception as exc:
                code = 1
                err = str(exc)

            done += 1
            if code != 0:
                failed.append((x, y, code))
                print(f"[{done}/{total}] FAIL x={x} y={y} (code {code})")
                if err.strip():
                    print(err.rstrip())
            else:
                print(f"[{done}/{total}] ok   x={x} y={y}")

    if failed:
        print("\nFailures:", file=sys.stderr)
        for x, y, code in failed:
            print(f"  x={x} y={y} code={code}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
