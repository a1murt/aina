"""The only source of "now" in Qost Twin (SPEC §5.2, NFR-11).

Business logic never calls ``datetime.now()`` / ``time.time()``; it receives a :class:`Clock`.

* ``CLOCK_MODE=system`` — :class:`SystemClock`, the wall clock (pilot on a real plant).
* ``CLOCK_MODE=sim`` — :class:`SimClock`, plant time published by ``services/sim`` in Redis.
* Tests — :class:`ManualClock` (alias :data:`FixedClock`), set and advanced explicitly.

All clocks return timezone-aware UTC datetimes. Convert for display with :func:`to_plant_tz`.

Redis key ``plant:clock`` (string, JSON, format version 1)::

    {"v": 1,
     "plant_time": "2026-10-16T02:00:00.000000Z",   # plant time at the moment of the write (UTC)
     "wall_ts":    "2026-10-06T20:15:42.123456Z",   # writer's wall clock at that moment (UTC)
     "speed": 60.0,                                  # plant seconds per wall second
     "paused": false,                                # true: plant time is frozen at plant_time
     "sim_mode": "live"}                             # live | backfill | ml-dataset

Readers extrapolate between writes: ``plant_now = plant_time + (wall_now - wall_ts) * speed``
(no extrapolation while paused). The simulator rewrites the key on every start / pause / speed
change / reset and periodically (about once per wall second) while running, so readers stay
exact even after a missed update; :attr:`SimClock.is_stale` flags a key that stopped updating.
Writers and readers run on the same host, so their wall clocks agree.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, Self
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from twin_core.settings import TwinSettings

PLANT_CLOCK_KEY = "plant:clock"
CLOCK_FORMAT_VERSION = 1

ClockMode = Literal["system", "sim", "manual"]


def system_now() -> datetime:
    """Wall-clock "now" (aware UTC). Only clocks and infrastructure code may call this."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` converted to UTC; naive datetimes are rejected (never guess a zone)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime {value.isoformat()} — timestamps must be timezone-aware")
    return value.astimezone(UTC)


def to_plant_tz(value: datetime, tz: ZoneInfo | str) -> datetime:
    """Convert an aware datetime to the plant time zone (``plant.yaml: site.timezone``)."""
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    return ensure_utc(value).astimezone(zone)


def format_utc(value: datetime) -> str:
    """ISO 8601 with microseconds and a ``Z`` suffix."""
    return ensure_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ClockError(RuntimeError):
    """Base class for clock problems."""


class ClockNotReadyError(ClockError):
    """``CLOCK_MODE=sim`` but no plant time has been read from Redis yet."""


class ClockStateError(ClockError, ValueError):
    """The ``plant:clock`` value is malformed."""


class Clock(ABC):
    """Source of the current plant time."""

    mode: ClassVar[ClockMode]

    @abstractmethod
    def now(self) -> datetime:
        """Current plant time, aware UTC."""

    @property
    def speed(self) -> float:
        """Plant seconds per wall second (1 for the system clock)."""
        return 1.0

    def now_in(self, tz: ZoneInfo | str) -> datetime:
        """Current plant time in the given zone (display only)."""
        return to_plant_tz(self.now(), tz)


class SystemClock(Clock):
    """Wall clock (``CLOCK_MODE=system``)."""

    mode: ClassVar[ClockMode] = "system"

    def now(self) -> datetime:
        return system_now()


class ManualClock(Clock):
    """Deterministic clock for tests and offline computations."""

    mode: ClassVar[ClockMode] = "manual"

    def __init__(self, start: datetime, *, speed: float = 1.0) -> None:
        self._now = ensure_utc(start)
        self._speed = speed

    def now(self) -> datetime:
        return self._now

    @property
    def speed(self) -> float:
        return self._speed

    def set(self, value: datetime) -> None:
        self._now = ensure_utc(value)

    def advance(self, delta: timedelta | float) -> datetime:
        """Move forward by a timedelta or a number of plant seconds; returns the new time."""
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
        if step < timedelta(0):
            raise ValueError("a clock cannot go backwards")
        self._now += step
        return self._now


FixedClock = ManualClock
"""Alias: a ManualClock that a test simply never advances."""


@dataclass(frozen=True)
class ClockState:
    """Value of the ``plant:clock`` Redis key (see module docstring)."""

    plant_time: datetime
    wall_ts: datetime
    speed: float
    paused: bool = False
    sim_mode: str = "live"

    def __post_init__(self) -> None:
        object.__setattr__(self, "plant_time", ensure_utc(self.plant_time))
        object.__setattr__(self, "wall_ts", ensure_utc(self.wall_ts))
        if self.speed <= 0:
            raise ClockStateError(f"speed must be positive, got {self.speed}")

    def plant_time_at(self, wall_now: datetime) -> datetime:
        """Extrapolated plant time at wall-clock instant ``wall_now``."""
        if self.paused:
            return self.plant_time
        elapsed = max(ensure_utc(wall_now) - self.wall_ts, timedelta(0))
        return self.plant_time + elapsed * self.speed

    def advanced_to(self, wall_now: datetime) -> ClockState:
        """The same clock re-anchored at ``wall_now`` (what a writer stores periodically)."""
        return replace(self, plant_time=self.plant_time_at(wall_now), wall_ts=wall_now)

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": CLOCK_FORMAT_VERSION,
                "plant_time": format_utc(self.plant_time),
                "wall_ts": format_utc(self.wall_ts),
                "speed": self.speed,
                "paused": self.paused,
                "sim_mode": self.sim_mode,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> ClockState:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("not a JSON object")
            version = data.get("v")
            if version != CLOCK_FORMAT_VERSION:
                raise ValueError(f"unsupported format version {version!r}")
            return cls(
                plant_time=datetime.fromisoformat(str(data["plant_time"])),
                wall_ts=datetime.fromisoformat(str(data["wall_ts"])),
                speed=float(data["speed"]),
                paused=bool(data.get("paused", False)),
                sim_mode=str(data.get("sim_mode", "live")),
            )
        except ClockStateError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ClockStateError(f"malformed {PLANT_CLOCK_KEY} value {raw!r}: {exc}") from exc


class KeyValueStore(Protocol):
    """The two Redis string commands the clock needs (``redis.asyncio`` via :class:`RedisKV`)."""

    async def get(self, key: str) -> bytes | str | None: ...

    async def set(self, key: str, value: str) -> None: ...


class RedisKV:
    """Adapter from ``redis.asyncio.Redis`` to :class:`KeyValueStore`."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @classmethod
    def from_url(cls, url: str) -> RedisKV:
        from redis.asyncio import Redis

        return cls(Redis.from_url(url))

    async def get(self, key: str) -> bytes | str | None:
        value = await self._redis.get(key)
        if value is None or isinstance(value, bytes | str):
            return value
        raise ClockStateError(f"unexpected Redis value type {type(value).__name__}")

    async def set(self, key: str, value: str) -> None:
        await self._redis.set(key, value)

    async def aclose(self) -> None:
        await self._redis.aclose()


async def publish_clock_state(
    store: KeyValueStore, state: ClockState, *, key: str = PLANT_CLOCK_KEY
) -> None:
    """Write the clock (used by ``services/sim``)."""
    await store.set(key, state.to_json())


async def read_clock_state(
    store: KeyValueStore, *, key: str = PLANT_CLOCK_KEY
) -> ClockState | None:
    raw = await store.get(key)
    return None if raw is None else ClockState.from_json(raw)


class SimClock(Clock):
    """Plant time from Redis ``plant:clock`` (``CLOCK_MODE=sim``).

    ``now()`` is synchronous and cheap: it extrapolates the last state read from Redis.
    Keep the state fresh by running the clock as an async context manager (polls in background)
    or by calling :meth:`refresh` yourself.
    """

    mode: ClassVar[ClockMode] = "sim"

    def __init__(
        self,
        store: KeyValueStore,
        *,
        key: str = PLANT_CLOCK_KEY,
        poll_interval_s: float = 0.25,
        stale_after: timedelta = timedelta(seconds=10),
        wall: Callable[[], datetime] = system_now,
    ) -> None:
        self._store = store
        self._key = key
        self._poll_interval_s = poll_interval_s
        self._stale_after = stale_after
        self._wall = wall
        self._state: ClockState | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> ClockState | None:
        return self._state

    @property
    def speed(self) -> float:
        """Current speed; 0 while paused or before the first read."""
        if self._state is None or self._state.paused:
            return 0.0
        return self._state.speed

    @property
    def is_stale(self) -> bool:
        """True when the last state is older than ``stale_after`` wall time (sim not writing)."""
        if self._state is None:
            return True
        return self._wall() - self._state.wall_ts > self._stale_after

    def now(self) -> datetime:
        if self._state is None:
            raise ClockNotReadyError(
                f"no plant time yet: Redis key '{self._key}' has not been read "
                "(is services/sim running? call refresh() / wait_ready() first)"
            )
        return self._state.plant_time_at(self._wall())

    async def refresh(self) -> ClockState | None:
        """Read the key once; keeps the previous state if the key is missing."""
        state = await read_clock_state(self._store, key=self._key)
        if state is not None:
            self._state = state
        return self._state

    async def wait_ready(self, timeout_s: float = 30.0) -> ClockState:
        """Poll until the simulator has published a clock (or raise ClockNotReadyError)."""
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    state = await self.refresh()
                    if state is not None:
                        return state
                    await asyncio.sleep(self._poll_interval_s)
        except TimeoutError:
            raise ClockNotReadyError(
                f"Redis key '{self._key}' did not appear within {timeout_s:g} s"
            ) from None

    async def run(self) -> None:
        """Poll forever (until cancelled)."""
        while True:
            with contextlib.suppress(ClockStateError, OSError):
                await self.refresh()
            await asyncio.sleep(self._poll_interval_s)

    async def __aenter__(self) -> Self:
        await self.refresh()
        self._task = asyncio.create_task(self.run(), name="sim-clock-poll")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def create_clock(settings: TwinSettings, store: KeyValueStore | None = None) -> Clock:
    """Clock for ``CLOCK_MODE``: system clock, or SimClock over Redis ``REDIS_URL``."""
    if settings.clock_mode == "system":
        return SystemClock()
    return SimClock(store if store is not None else RedisKV.from_url(settings.redis_url))
