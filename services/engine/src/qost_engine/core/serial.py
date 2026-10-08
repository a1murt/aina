"""JSON round-trip of the core's state dataclasses (engine snapshot, NFR-03).

Supports dataclasses, lists, tuples, deques, dicts with ``str`` keys, ``X | None`` and primitives;
nested types come from the dataclass annotations, so a snapshot is plain JSON (``jsonb``).
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections import deque
from functools import cache
from typing import Any, Union, get_args, get_origin


def dump(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, list | tuple | deque):
        return [dump(x) for x in list(obj)]
    if isinstance(obj, dict):
        return {str(k): dump(v) for k, v in obj.items()}
    return obj


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def load(tp: Any, data: Any) -> Any:
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        if data is None:
            return None
        args = [a for a in get_args(tp) if a is not type(None)]
        return load(args[0], data) if len(args) == 1 else data
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        hints = _hints(tp)
        kwargs = {
            f.name: load(hints[f.name], data[f.name])
            for f in dataclasses.fields(tp)
            if f.name in data and f.init
        }
        return tp(**kwargs)
    if origin is list:
        (arg,) = get_args(tp)
        return [load(arg, x) for x in data]
    if origin is deque:
        (arg,) = get_args(tp)
        return deque(load(arg, x) for x in data)
    if origin is tuple:
        targs = get_args(tp)
        if len(targs) == 2 and targs[1] is Ellipsis:
            return tuple(load(targs[0], x) for x in data)
        return tuple(load(a, x) for a, x in zip(targs, data, strict=True))
    if origin is dict:
        _key, value = get_args(tp)
        return {k: load(value, v) for k, v in data.items()}
    if tp is float and isinstance(data, int):
        return float(data)
    return data
