"""Validate a config directory from the command line.

    python -m twin_core.config [CONFIG_DIR] [--tag-map FILE]

Exit code 0 and a short summary when valid; 1 and the full list of problems otherwise.
"""

from __future__ import annotations

import argparse
import sys

from twin_core.config.errors import ConfigError
from twin_core.config.loader import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m twin_core.config", description=__doc__)
    parser.add_argument("config_dir", nargs="?", default=None)
    parser.add_argument("--tag-map", default=None, help="tag map file to validate as well")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config_dir, tag_map=args.tag_map)
    except ConfigError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    print(f"OK: {cfg!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
