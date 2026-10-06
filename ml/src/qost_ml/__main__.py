"""Entry point: ``python -m qost_ml {dataset,train}`` — implemented in stage M7."""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qost_ml", description=__doc__)
    parser.add_argument("command", choices=["dataset", "train"])
    args = parser.parse_args(argv)
    print(f"qost_ml {args.command}: not implemented until M7")
    return 0


if __name__ == "__main__":
    sys.exit(main())
