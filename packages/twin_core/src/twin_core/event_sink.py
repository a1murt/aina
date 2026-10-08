"""Destinations for batches of :mod:`twin_core.events` (SPEC §6.1 backfill, §7.2 FR-ING-02).

A sink is opened from a URL-like spec, so producers (``qost_sim backfill``, the collector) do not
depend on where events go::

    sink = open_sink("jsonl:var/backfill.jsonl")   # one JSON event per line
    sink = open_sink("memory:")                    # tests
    sink = open_sink("null:")                      # benchmarks

More schemes are registered with :func:`register_sink`. ``db:`` (``db:`` = ``DATABASE_URL``, or
``db:postgresql+asyncpg://…``) is provided by :mod:`twin_core.db.sink` and imported lazily, so
``qost_sim backfill --sink db:`` and the collector share one writer (idempotent by ``event_id``).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Protocol, runtime_checkable

from twin_core.events import AnyEvent, dumps


@runtime_checkable
class EventSink(Protocol):
    """Receives events in batches, in non-decreasing ``ts`` order per producer."""

    async def write(self, events: Sequence[AnyEvent]) -> None: ...

    async def aclose(self) -> None: ...


class MemorySink:
    """Keeps every event in a list (tests)."""

    def __init__(self) -> None:
        self.events: list[AnyEvent] = []
        self.batches = 0
        self.closed = False

    async def write(self, events: Sequence[AnyEvent]) -> None:
        self.events.extend(events)
        self.batches += 1

    async def aclose(self) -> None:
        self.closed = True


class NullSink:
    """Counts and discards events."""

    def __init__(self) -> None:
        self.count = 0

    async def write(self, events: Sequence[AnyEvent]) -> None:
        self.count += len(events)

    async def aclose(self) -> None:
        return None


class JsonlSink:
    """Appends one compact JSON document per line to a file (UTF-8, ``\\n`` separated).

    The file is truncated when the sink is opened, so re-running a producer replaces its output.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] | None = self.path.open("w", encoding="utf-8", newline="\n")
        self.count = 0

    async def write(self, events: Sequence[AnyEvent]) -> None:
        if self._file is None:
            raise RuntimeError(f"sink {self.path} is closed")
        self._file.write("".join(dumps(event) + "\n" for event in events))
        self.count += len(events)

    async def aclose(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


SinkFactory = Callable[[str], EventSink]

_REGISTRY: dict[str, SinkFactory] = {}


def register_sink(scheme: str, factory: SinkFactory) -> None:
    """Register a factory for ``scheme:`` specs (the argument is the text after the colon)."""
    _REGISTRY[scheme] = factory


def _jsonl(arg: str) -> EventSink:
    if not arg:
        raise ValueError("jsonl sink needs a path: 'jsonl:PATH'")
    return JsonlSink(arg)


register_sink("jsonl", _jsonl)
register_sink("memory", lambda _arg: MemorySink())
register_sink("null", lambda _arg: NullSink())


def sink_schemes() -> list[str]:
    return sorted(set(_REGISTRY) | set(_LAZY))


_LAZY = {"db": "twin_core.db.sink"}
"""Schemes whose module registers itself on import (keeps asyncpg out of light imports)."""


def open_sink(spec: str) -> EventSink:
    """Open a sink from ``scheme:argument`` (see the module docstring)."""
    scheme, sep, arg = spec.partition(":")
    if sep and scheme not in _REGISTRY and scheme in _LAZY:
        importlib.import_module(_LAZY[scheme])
    if not sep or scheme not in _REGISTRY:
        raise ValueError(f"unknown event sink {spec!r}; known schemes: {', '.join(sink_schemes())}")
    return _REGISTRY[scheme](arg)


__all__ = [
    "EventSink",
    "JsonlSink",
    "MemorySink",
    "NullSink",
    "SinkFactory",
    "open_sink",
    "register_sink",
    "sink_schemes",
]
