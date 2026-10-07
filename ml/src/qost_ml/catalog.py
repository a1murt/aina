"""Feature texts for explanations: ``ml/feature_catalog.yaml`` (SPEC §11.1, ru + draft kk).

A feature's text comes from ``features.<name>`` if present, otherwise from the ``stats`` template of
``<signal>_<stat>_<window>h`` filled with the signal's words, unit and window. Variants: ``_neg``
for a negative value, ``_flat`` for a value that rounds to zero, ``_missing`` for NaN.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from qost_ml.spec import PdmSpec

LANGS: tuple[str, ...] = ("ru", "kk")
_NAME = re.compile(r"^(?P<signal>.+)_(?P<stat>mean|std|max|slope|count)_(?P<window>\d+)h$")
_MARKER = Path("ml") / "feature_catalog.yaml"


def search_upwards(marker: Path) -> Path | None:
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for directory in (start, *start.parents):
            if (directory / marker).exists():
                return directory / marker
    return None


def default_catalog_path() -> Path:
    """``ML_FEATURE_CATALOG`` or the repository's ``ml/feature_catalog.yaml``."""
    env = os.environ.get("ML_FEATURE_CATALOG")
    if env:
        return Path(env)
    found = search_upwards(_MARKER)
    return found if found is not None else _MARKER


class CatalogError(ValueError):
    """The catalog is missing a text a feature can need."""


@dataclass(frozen=True)
class FeatureCatalog:
    data: dict[str, Any]
    spec: PdmSpec

    @classmethod
    def load(cls, spec: PdmSpec, path: Path | None = None) -> FeatureCatalog:
        source = path or default_catalog_path()
        data = yaml.safe_load(source.read_text("utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise CatalogError(f"{source}: expected a mapping with version: 1")
        catalog = cls(data, spec)
        problems = catalog.problems()
        if problems:
            raise CatalogError(f"{source}:\n  " + "\n  ".join(problems))
        return catalog

    # ------------------------------------------------------------------ lookup

    def _entry(self, feature: str) -> tuple[dict[str, Any], dict[str, str]]:
        """(template entry, placeholders) for a feature name."""
        features: dict[str, Any] = self.data.get("features") or {}
        if feature in features:
            return features[feature], {}
        match = _NAME.match(feature)
        if match is None:
            raise CatalogError(f"no text for feature {feature!r}")
        signal = (self.data.get("signals") or {}).get(match["signal"])
        stat = (self.data.get("stats") or {}).get(match["stat"])
        window = (self.data.get("windows") or {}).get(int(match["window"]))
        if signal is None or stat is None or window is None:
            raise CatalogError(f"no signal/stat/window text for feature {feature!r}")
        return stat, {"signal": match["signal"], "window": match["window"]}

    def text(self, feature: str, value: float, lang: str = "ru") -> str:
        """Human text of ``feature = value`` in ``lang`` (``ru`` or ``kk``)."""
        if lang not in LANGS:
            raise ValueError(f"unknown language {lang!r}")
        entry, parts = self._entry(feature)
        missing = math.isnan(value)
        if missing:
            template = entry.get(f"{lang}_missing")
            if template is None:
                label = self.data["labels"]["missing"][lang]
                return str(label).format(feature=feature)
        elif round(float(value), 2) == 0 and f"{lang}_flat" in entry:
            template = entry[f"{lang}_flat"]
        elif float(value) < 0 and f"{lang}_neg" in entry:
            template = entry[f"{lang}_neg"]
        else:
            template = entry[lang]
        fields: dict[str, Any] = {"value": 0.0 if missing else float(value)}
        fields["horizon"] = self.spec.horizon_h
        if "signal" in parts:
            sig = self.data["signals"][parts["signal"]]
            fields["signal"] = sig[lang]
            fields["signal_gen"] = sig.get(f"{lang}_gen", sig[lang])
            fields["unit"] = sig[f"unit_{lang}"]
            fields["window"] = self.data["windows"][int(parts["window"])][lang]
        if feature == "shift" and not missing:
            fields["shift"] = self._shift_name(int(value), lang)
        return str(template).format(**fields)

    def _shift_name(self, index: int, lang: str) -> str:
        names = self.spec.shift_names_ru if lang == "ru" else self.spec.shift_names_kk
        if 0 <= index < len(names):
            return names[index]
        return str(self.data["labels"]["off_shift"][lang])

    def texts(self, feature: str, value: float) -> dict[str, str]:
        return {lang: self.text(feature, value, lang) for lang in LANGS}

    # ------------------------------------------------------------------ validation

    def problems(self) -> list[str]:
        """Every feature of every PdM type must render in every language for any value."""
        out: list[str] = []
        signals = self.data.get("signals") or {}
        for ts in self.spec.types.values():
            for code in ts.signal_codes:
                if code not in signals:
                    out.append(f"signals.{code}: missing")
        for name, sig in signals.items():
            for key in (*LANGS, *(f"unit_{lang}" for lang in LANGS)):
                if not sig.get(key):
                    out.append(f"signals.{name}.{key}: missing")
        for equipment_type in self.spec.types:
            for feature in self.spec.feature_names(equipment_type):
                for value in (1.25, -1.25, 0.0, math.nan, 99.0):
                    for lang in LANGS:
                        try:
                            text = self.text(feature, value, lang)
                        except (CatalogError, KeyError, ValueError, IndexError) as exc:
                            out.append(f"{feature} ({lang}, {value}): {exc!r}")
                            continue
                        if not text.strip() or "{" in text or "None" in text:
                            out.append(f"{feature} ({lang}, {value}): bad text {text!r}")
        return sorted(set(out))
