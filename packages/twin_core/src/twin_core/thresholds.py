"""Effective thresholds: ``rules.yaml`` overlaid by the ``settings`` table (SPEC §8, §12.2).

``PATCH /api/v1/config/thresholds`` (admin) stores *partial* overrides in ``settings`` under the
keys of :data:`OVERRIDE_KEYS` (``thresholds`` -> :class:`~twin_core.config.Thresholds`,
``data_quality`` -> data-quality tolerances); every change is audited by the API. Readers apply
them with :func:`apply_rule_overrides` / :func:`effective_config`, so the YAML stays the default
and a removed override falls back to it. The engine (or any asyncpg user) loads the stored
overrides with :func:`fetch_rule_overrides`.

Overrides are validated by the same pydantic models as the YAML (types, ranges and the ordering
checks such as ``defect_rate_limit < defect_rate_critical``), so an override can never produce a
configuration the YAML itself would reject.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from twin_core.config import RulesConfig, TwinConfig

OVERRIDE_KEYS: tuple[str, ...] = ("thresholds", "data_quality")
"""``settings.key`` values holding rule overrides (each value: ``{field: value}``)."""

FETCH_SQL = "SELECT key, value::text AS value FROM settings WHERE key = ANY($1::text[])"
"""asyncpg query of the stored overrides (used by :func:`fetch_rule_overrides`)."""


class OverrideError(ValueError):
    """Overrides that do not validate; ``errors`` lists ``{loc, msg, type}`` like FastAPI."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors))
        self.errors = errors


def _section(rules: RulesConfig, key: str) -> BaseModel:
    section = getattr(rules, key)
    if not isinstance(section, BaseModel):
        raise TypeError(f"rules.{key} is not a model")
    return section


def override_fields(rules: RulesConfig) -> dict[str, list[str]]:
    """Field names that may be overridden, per settings key."""
    return {key: list(type(_section(rules, key)).model_fields) for key in OVERRIDE_KEYS}


def apply_rule_overrides(
    rules: RulesConfig, overrides: Mapping[str, Mapping[str, Any]] | None
) -> RulesConfig:
    """``rules`` with ``overrides`` (``{"thresholds": {...}, "data_quality": {...}}``) applied.

    Unknown sections or fields, wrong types and broken ordering raise :class:`OverrideError`.
    ``None`` values are ignored (= use the YAML value).
    """
    if not overrides:
        return rules
    errors: list[dict[str, Any]] = []
    update: dict[str, BaseModel] = {}
    for key, values in overrides.items():
        if key not in OVERRIDE_KEYS:
            errors.append({"loc": [key], "msg": "unknown settings section", "type": "extra"})
            continue
        section = _section(rules, key)
        fields = type(section).model_fields
        clean = {k: v for k, v in (values or {}).items() if v is not None}
        for name in clean:
            if name not in fields:
                errors.append(
                    {"loc": [key, name], "msg": "unknown threshold", "type": "extra_forbidden"}
                )
        if any(e["loc"][0] == key for e in errors):
            continue
        merged = {**section.model_dump(), **clean}
        try:
            update[key] = type(section).model_validate(merged)
        except ValidationError as exc:
            errors.extend(
                {"loc": [key, *e["loc"]], "msg": e["msg"], "type": e["type"]}
                for e in exc.errors(include_url=False, include_context=False)
            )
    if errors:
        raise OverrideError(errors)
    return rules.model_copy(update=update)


def effective_config(
    cfg: TwinConfig, overrides: Mapping[str, Mapping[str, Any]] | None
) -> TwinConfig:
    """A :class:`TwinConfig` whose ``rules`` carry the overrides (the same object without any)."""
    if not overrides:
        return cfg
    rules = apply_rule_overrides(cfg.rules, overrides)
    if rules is cfg.rules:
        return cfg
    return TwinConfig(
        cfg.config_dir,
        plant=cfg.plant,
        rules=rules,
        reasons=cfg.reason_codes,
        defects=cfg.defect_codes,
        simulation=cfg.simulation,
        business=cfg.business,
        tag_map=cfg.tag_map,
        tag_map_path=cfg.tag_map_path,
    )


def overrides_from_rows(rows: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{settings.key: value}`` (value as dict or JSON text) -> overrides of known sections."""
    out: dict[str, dict[str, Any]] = {}
    for key in OVERRIDE_KEYS:
        raw = rows.get(key)
        if raw is None:
            continue
        value = json.loads(raw) if isinstance(raw, str | bytes) else raw
        if isinstance(value, dict) and value:
            out[key] = dict(value)
    return out


async def fetch_rule_overrides(conn: Any) -> dict[str, dict[str, Any]]:
    """Stored overrides through an asyncpg connection (engine and tools)."""
    rows = await conn.fetch(FETCH_SQL, list(OVERRIDE_KEYS))
    return overrides_from_rows({r["key"]: r["value"] for r in rows})


__all__ = [
    "FETCH_SQL",
    "OVERRIDE_KEYS",
    "OverrideError",
    "apply_rule_overrides",
    "effective_config",
    "fetch_rule_overrides",
    "override_fields",
    "overrides_from_rows",
]
