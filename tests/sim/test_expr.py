"""Telemetry signal models: safe expression compiler (simulation.yaml ``telemetry``)."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from qost_sim.model.expr import ExpressionError, compile_expr
from qost_sim.model.plant import compile_signal_models
from twin_core.config import TwinConfig


def test_every_configured_signal_model_compiles(cfg: TwinConfig) -> None:
    models = compile_signal_models(cfg)
    for type_code, signals in cfg.simulation.telemetry.per_type.items():
        assert [m.code for m in models[type_code]] == list(signals)
    oven = models["oven"][0].expr
    assert oven.uses_precursor
    assert not any(m.expr.uses_precursor for t, ms in models.items() if t != "oven" for m in ms)
    booth = {m.code: m.expr for m in models["booth"]}
    assert booth["filter_dp_pa"].names == {"dp_start", "rate", "hours_since_change"}
    assert "filter_dp_pa" in booth["airflow_mps"].names


def test_evaluation_and_noise() -> None:
    expr = compile_expr("12 + 7*d + N(0, 0.4)", ["d"]).bind(random.Random(1))
    values = [expr.evaluate({"d": 0.5}) for _ in range(4000)]
    assert statistics.fmean(values) == pytest.approx(15.5, abs=0.03)
    assert statistics.pstdev(values) == pytest.approx(0.4, rel=0.05)
    caret = compile_expr("2.0 + 5.5*d^2", ["d"]).bind(random.Random(1))
    assert caret.evaluate({"d": 0.5}) == pytest.approx(2.0 + 5.5 * 0.25)


def test_functions() -> None:
    rng = random.Random(3)
    expr = compile_expr("Poisson(0.2 + 4*d^2)", ["d"]).bind(rng)
    counts = [expr.evaluate({"d": 1.0}) for _ in range(5000)]
    assert statistics.fmean(counts) == pytest.approx(4.2, rel=0.05)
    assert all(c == int(c) and c >= 0 for c in counts)
    big = compile_expr("Poisson(50)", []).bind(rng)
    assert statistics.fmean(big.evaluate({}) for _ in range(500)) == pytest.approx(50, rel=0.05)
    assert compile_expr("Poisson(0)", []).bind(rng).evaluate({}) == 0
    trig = compile_expr("55 + 10*sin(2*pi*t/24 + phase)", ["t", "phase"]).bind(rng)
    assert trig.evaluate({"t": 6.0, "phase": 0.0}) == pytest.approx(65.0)
    clip = compile_expr("clip(x, 0, 1) + max(1, 2, 3) + min(4, 5) + abs(-1) + sqrt(4)", ["x"])
    assert clip.bind(rng).evaluate({"x": 7}) == pytest.approx(1 + 3 + 4 + 1 + 2)
    assert compile_expr("exp(0) + log(e) + cos(0)", []).bind(rng).evaluate({}) == pytest.approx(3)


def test_precursor_is_bound_per_unit() -> None:
    expr = compile_expr("140 + 8*precursor(2)", [])
    assert expr.bind(random.Random(0)).evaluate({}) == 140  # unbound: no precursor
    calls: list[float] = []

    def precursor(hours: float) -> float:
        calls.append(hours)
        return 0.5

    assert expr.bind(random.Random(0), precursor).evaluate({}) == 144
    assert calls == [2]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("__import__('os').system('true')", "unknown function"),
        ("d.real", "Attribute is not allowed"),
        ("[d][0]", "not allowed"),
        ("unknown + 1", "unknown variable 'unknown'"),
        ("N(0)", "takes 2..2 arguments"),
        ("N(mu=0, sd=1)", "only positional"),
        ("min(*d)", "only positional"),
        ("'text'", "only numbers"),
        ("True + 1", "only numbers"),
        ("d if d else 0", "IfExp is not allowed"),
        ("lambda: 1", "Lambda is not allowed"),
        ("12 + (", "syntax error"),
        ("140 + N(0, 1.2) (+ drift)", "unknown function"),
        ("d // 2", "BinOp is not allowed"),
    ],
)
def test_unsafe_or_invalid_expressions_are_rejected(text: str, message: str) -> None:
    with pytest.raises(ExpressionError, match=message):
        compile_expr(text, ["d"])


def test_constants_are_not_variables() -> None:
    expr = compile_expr("2*pi*d", ["d"])
    assert expr.names == {"d"}
    assert expr.bind(random.Random(0)).evaluate({"d": 1}) == pytest.approx(2 * math.pi)
