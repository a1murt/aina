"""MQTT Unified Namespace topics (SPEC §6.8), derived from the plant hierarchy.

``{topic_root}/{AREA}/{LINE}/{EQUIPMENT}/{signal}`` for equipment, ``…/{AREA}/{LINE}/{signal}``
for lines, ``…/BUFFERS/{CODE}/{signal}``, ``…/{CKD area}/{PRODUCT}/{signal}`` and
``…/Plant/{signal}``; unit and defect events of a line go to ``…/{AREA}/{LINE}/units``.
``topic_root`` already contains the site (``qost/v1/KST``). The simulator publishes exactly these
topics and the collector maps them back (parity is tested).
"""

from __future__ import annotations

from twin_core.config import TwinConfig

BUFFERS_FOLDER = "BUFFERS"
PLANT_FOLDER = "Plant"
UNITS_TOPIC = "units"


def _ckd_area(cfg: TwinConfig) -> str:
    return next((a.code for a in cfg.plant.areas if a.kind == "storage"), "CKD")


def target_path(cfg: TwinConfig, kind: str, code: str) -> tuple[str, ...]:
    """Path segments below the topic root for a tag-map target (``equipment``, ``line``, …)."""
    if kind == "equipment":
        line = cfg.line_of_equipment(code)
        return (cfg.area_of_line(line.code).code, line.code, code)
    if kind == "line":
        return (cfg.area_of_line(code).code, code)
    if kind == "buffer":
        return (BUFFERS_FOLDER, code)
    if kind == "product":
        return (_ckd_area(cfg), code)
    if kind == "site":
        return (PLANT_FOLDER,)
    raise ValueError(f"unknown tag target kind {kind!r}")


def signal_topic(cfg: TwinConfig, topic_root: str, kind: str, code: str, signal: str) -> str:
    return "/".join((topic_root.rstrip("/"), *target_path(cfg, kind, code), signal))


def units_topic(cfg: TwinConfig, topic_root: str, line: str) -> str:
    return "/".join((topic_root.rstrip("/"), *target_path(cfg, "line", line), UNITS_TOPIC))


__all__ = ["signal_topic", "target_path", "units_topic"]
