"""Safe compiler for the telemetry signal models of ``simulation.yaml: telemetry``.

An expression such as ``"12 + 7*d + N(0, 0.4)"`` is parsed with :mod:`ast` and accepted only if
it consists of numbers, whitelisted variables, ``+ - * / ^`` and whitelisted functions; there are
no attributes, subscripts, keywords or other names, so evaluating the compiled code is safe.

Functions: ``sin cos exp log sqrt abs min max clip(x, lo, hi)``, ``N(mu, sd)`` (normal draw),
``Poisson(lam)`` and ``precursor(h)`` — 0 → 1 over the last ``h`` operating hours before the
unit's next wear-reason failure (random failures never have a precursor, SPEC §11.1).
"""

from __future__ import annotations

import ast
import math
import random
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from types import CodeType
from typing import Any

from qost_sim.model.rng import poisson

CONSTANTS: Mapping[str, float] = {"pi": math.pi, "e": math.e}

_ARITY: Mapping[str, tuple[int, int]] = {
    "sin": (1, 1),
    "cos": (1, 1),
    "exp": (1, 1),
    "log": (1, 1),
    "sqrt": (1, 1),
    "abs": (1, 1),
    "min": (2, 8),
    "max": (2, 8),
    "clip": (3, 3),
    "N": (2, 2),
    "Poisson": (1, 1),
    "precursor": (1, 1),
}
_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_UNARY = (ast.UAdd, ast.USub)


class ExpressionError(ValueError):
    """The expression is not a valid signal model."""


def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


@dataclass(frozen=True)
class CompiledExpr:
    source: str
    names: frozenset[str]
    """Variables the expression reads."""
    functions: frozenset[str]
    _code: CodeType

    @property
    def uses_precursor(self) -> bool:
        return "precursor" in self.functions

    def bind(
        self, rng: random.Random, precursor: Callable[[float], float] | None = None
    ) -> BoundExpr:
        """Bind random draws and the precursor function of one unit and signal."""
        namespace: dict[str, Any] = {
            "__builtins__": {},
            "sin": math.sin,
            "cos": math.cos,
            "exp": math.exp,
            "log": math.log,
            "sqrt": math.sqrt,
            "abs": abs,
            "min": min,
            "max": max,
            "clip": _clip,
            "N": rng.gauss,
            "Poisson": lambda lam: poisson(rng, lam),
            "precursor": precursor if precursor is not None else (lambda _h: 0.0),
            **CONSTANTS,
        }
        return BoundExpr(self, namespace)


class BoundExpr:
    """A compiled expression bound to its random stream; ``evaluate(variables)``."""

    __slots__ = ("_code", "_namespace", "expr")

    def __init__(self, expr: CompiledExpr, namespace: dict[str, Any]) -> None:
        self.expr = expr
        self._code = expr._code
        self._namespace = namespace

    def evaluate(self, variables: Mapping[str, float]) -> float:
        value = eval(self._code, self._namespace, dict(variables))  # whitelisted AST only
        return float(value)


def compile_expr(text: str, variables: Collection[str]) -> CompiledExpr:
    """Validate and compile ``text``; ``variables`` are the names it may read."""
    source = text.strip()
    try:
        tree = ast.parse(source.replace("^", "**"), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error in {source!r}: {exc.msg}") from None
    allowed_vars = set(variables) | set(CONSTANTS)
    names: set[str] = set()
    functions: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            visit(node.body)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, _BINOPS):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, _UNARY):
            visit(node.operand)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int | float):
                raise ExpressionError(f"only numbers are allowed in {source!r}")
        elif isinstance(node, ast.Name):
            if node.id not in allowed_vars:
                known = ", ".join(sorted(allowed_vars))
                raise ExpressionError(
                    f"unknown variable {node.id!r} in {source!r} (known: {known})"
                )
            if node.id not in CONSTANTS:
                names.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ARITY:
                raise ExpressionError(
                    f"unknown function in {source!r} (allowed: {', '.join(sorted(_ARITY))})"
                )
            if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                raise ExpressionError(f"only positional arguments are allowed in {source!r}")
            low, high = _ARITY[node.func.id]
            if not low <= len(node.args) <= high:
                raise ExpressionError(
                    f"{node.func.id}() takes {low}..{high} arguments in {source!r}"
                )
            functions.add(node.func.id)
            for arg in node.args:
                visit(arg)
        else:
            raise ExpressionError(f"{type(node).__name__} is not allowed in {source!r}")

    visit(tree)
    code = compile(tree, f"<signal model {source}>", "eval")
    return CompiledExpr(source, frozenset(names), frozenset(functions), code)
