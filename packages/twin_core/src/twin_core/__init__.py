"""Qost Twin shared core library.

Sub-modules:
    twin_core.config    pydantic models + loader for config/*.yaml (FR-DOM-01/02)
    twin_core.aliases   external name -> code resolution, closest-code suggestions
    twin_core.clock     the only source of "now" (SPEC §5.2, NFR-11)
    twin_core.calendar  shift calendar (shifts, working days, holidays)
    twin_core.domain    shared domain enums (ISA-95 / ISO 22400 vocabulary)
"""

__version__ = "0.1.0"
