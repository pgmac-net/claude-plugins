#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["cairosvg>=2.7"]
# ///
"""Render icon.svg to the PNG sizes the harness uses.

  uv run render.py            write icon-<size>.png beside this file
  uv run render.py --out DIR  write them somewhere else

The SVG is the source; the PNGs are committed so nobody needs a renderer to
use them. Slack takes icon-1024.png (512 to 2000 px square).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cairosvg

HERE = Path(__file__).resolve().parent
SIZES = (1024, 512, 256, 128, 64, 32)


def render(source: Path, out_dir: Path) -> list[Path]:
    """Write one PNG per size and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for size in SIZES:
        target = out_dir / f"icon-{size}.png"
        cairosvg.svg2png(url=str(source), write_to=str(target), output_width=size, output_height=size)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=HERE / "icon.svg", help="the SVG to render")
    parser.add_argument("--out", type=Path, default=HERE, help="directory for the PNGs")
    args = parser.parse_args(argv)
    if not args.source.is_file():
        print(f"ERROR no such file: {args.source}", file=sys.stderr)
        return 2
    for path in render(args.source, args.out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
