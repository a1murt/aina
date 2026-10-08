"""Aina shared core library.

Sub-modules:
    twin_core.config    pydantic models + loader for config/*.yaml (FR-DOM-01/02)
    twin_core.aliases   external name -> code resolution, closest-code suggestions
    twin_core.clock     the only source of "now" (SPEC §5.2, NFR-11)
    twin_core.calendar  shift calendar (shifts, working days, holidays)
    twin_core.domain    shared domain enums (ISA-95 / ISO 22400 vocabulary)
    twin_core.kpi       ISO 22400-2 KPIs as pure functions, loss tree, plan and capacity (§5.4-5.8)
    twin_core.dq        data-quality reconciliation rules DQ-01…07 (§9.6)
    twin_core.rules     alert rules AL-* on aggregates, deduplication (§9.7)
    twin_core.bottleneck  bottleneck detection (aggregate path; active periods from M3) (§9.5)
    twin_core.importer  docx/xlsx/csv import: readers, header recognition, report (§7.4)
    twin_core.db        SQLAlchemy models of the storage schema (§8)
"""

__version__ = "0.1.0"
