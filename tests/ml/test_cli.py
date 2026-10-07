"""``python -m qost_ml`` (``make ml-dataset`` / ``make ml-train``) entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from qost_ml.__main__ import main as ml_main


def test_cards_summarise_the_committed_models(capsys: pytest.CaptureFixture[str]) -> None:
    assert ml_main(["cards"]) == 0
    out = capsys.readouterr().out
    assert "conveyor" in out
    assert "robot" in out
    assert out.count("thresholds: ok") == 2


def test_cards_without_models(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ml_main(["cards", "--models", str(tmp_path)]) == 0
    assert "no model cards" in capsys.readouterr().out


def test_command_is_required() -> None:
    with pytest.raises(SystemExit):
        ml_main([])
