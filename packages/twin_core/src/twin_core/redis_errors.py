"""Exceptions a Redis call may raise when the server is unavailable or refuses the command.

``redis.exceptions.ConnectionError`` / ``TimeoutError`` derive from ``RedisError``, not from the
builtin ``ConnectionError`` / ``OSError``, so ``except (OSError, ConnectionError)`` lets them
through (STAB-3). Catch :data:`REDIS_ERRORS` around every Redis call that must not kill a loop.
"""

from __future__ import annotations

from redis.exceptions import RedisError

REDIS_ERRORS: tuple[type[BaseException], ...] = (OSError, ConnectionError, RedisError)
"""Transport (``OSError``, builtin ``ConnectionError``) and client (``RedisError``) errors."""

__all__ = ["REDIS_ERRORS"]
