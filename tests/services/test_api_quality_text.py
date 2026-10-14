"""Wording of the defect-correlation card (WIN-15): no link / weak link / link."""

from __future__ import annotations

import qost_api.app  # noqa: F401  (route modules after the app: no import cycle)
from qost_api.routes.quality import correlation_text

FACTOR = "Перепад давления на фильтрах"


def test_not_significant_is_no_link() -> None:
    strength, text = correlation_text(
        "окраска", FACTOR, rho=0.05, p_value=0.41, n=160, direction=1, insight=False
    )
    assert strength == "none"
    assert text == "связи брака (окраска) с фактором «Перепад давления на фильтрах» нет " + (
        "(ρ = 0,05, p = 0,41, 160 ч)"
    )


def test_significant_below_the_threshold_is_a_weak_link() -> None:
    strength, text = correlation_text(
        "окраска", FACTOR, rho=0.29, p_value=0.0002, n=160, direction=1, insight=False
    )
    assert strength == "weak"
    assert text.startswith("слабая связь брака (окраска) с фактором «Перепад давления")
    assert "ρ = 0,29 — значима (p < 0,001), но ниже порога 0,3 для оповещения; 160 ч" in text
    assert "не обнаружена" not in text


def test_strong_link_keeps_the_insight_text() -> None:
    strength, text = correlation_text(
        "окраска", "Влажность", rho=-0.42, p_value=0.0001, n=160, direction=-1, insight=True
    )
    assert strength == "strong"
    assert text == (
        "брак (окраска) снижается вместе с фактором «Влажность»: ρ = -0,42, p < 0,001, "
        "160 ч наблюдений"
    )
