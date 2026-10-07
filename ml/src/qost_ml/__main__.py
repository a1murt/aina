"""Entry point: ``python -m qost_ml {dataset,train,cards}`` (SPEC §11.1).

* ``dataset --raw ml/data/raw --out ml/data/features`` — windowed features and labels from the raw
  parquet of ``python -m qost_sim ml-dataset`` (``make ml-dataset`` runs both).
* ``train --features ml/data/features --models ml/models`` — LightGBM per model type, calibration,
  model cards; exit code 1 if a SPEC §11.1 metric threshold is not reached.
* ``cards --models ml/models`` — summary of the latest model cards.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from twin_core.health import load_config_or_exit

SERVICE = "ml"


def _ml_root() -> Path:
    from qost_ml.predictor import default_models_dir

    return default_models_dir().parent


def _summary(card: dict[str, Any]) -> str:
    test = card["metrics"]["test"]
    passed = card["thresholds"]["passed"]
    lead = test.get("lead_time_median_h")
    return (
        f"{card['type']:<9} {card['version']}  test: PR-AUC {test['pr_auc']}, ROC-AUC "
        f"{test['roc_auc']}, lead median {lead if lead is not None else '—'} h "
        f"({test['failures_detected']}/{test['failures']} failures), base rate {test['base_rate']}"
        f"  thresholds: {'ok' if all(passed.values()) else 'NOT MET ' + json.dumps(passed)}"
    )


def cmd_dataset(args: argparse.Namespace) -> int:
    from qost_ml.dataset import build_dataset

    cfg = load_config_or_exit(SERVICE)
    meta = build_dataset(cfg, Path(args.raw), Path(args.out))
    for equipment_type, info in meta["tables"].items():
        print(
            f"{equipment_type:<9} rows {info['rows']}, eligible {info['eligible_rows']}, "
            f"positive {info['positive_rows']}, hash {info['hash'][:12]}"
        )
    print(f"features -> {args.out}")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    from qost_ml.train import train_all

    cfg = load_config_or_exit(SERVICE)
    results = train_all(
        cfg,
        Path(args.features),
        Path(args.models),
        types=tuple(args.types) if args.types else None,
    )
    ok = True
    for result in results.values():
        print(_summary(result.card))
        ok &= all(result.card["thresholds"]["passed"].values())
    return 0 if ok else 1


def cmd_cards(args: argparse.Namespace) -> int:
    from qost_ml.predictor import CARD_FILE, latest_version

    root = Path(args.models)
    found = False
    for type_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        path = latest_version(type_dir)
        if path is not None:
            found = True
            print(_summary(json.loads((path / CARD_FILE).read_text("utf-8"))))
    if not found:
        print(f"no model cards in {root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    root = _ml_root()
    parser = argparse.ArgumentParser(prog="python -m qost_ml", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ds = sub.add_parser("dataset", help="features and labels from the raw ml-dataset")
    ds.add_argument("--raw", default=str(root / "data" / "raw"))
    ds.add_argument("--out", default=str(root / "data" / "features"))
    tr = sub.add_parser("train", help="train, calibrate and evaluate the PdM models")
    tr.add_argument("--features", default=str(root / "data" / "features"))
    tr.add_argument("--models", default=str(root / "models"))
    tr.add_argument("--types", nargs="*", help="equipment types (default: robot conveyor)")
    cards = sub.add_parser("cards", help="print the latest model cards")
    cards.add_argument("--models", default=str(root / "models"))
    args = parser.parse_args(argv)
    handlers = {"dataset": cmd_dataset, "train": cmd_train, "cards": cmd_cards}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
