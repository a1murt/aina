"""YAML reading with source positions.

PyYAML silently keeps the last of duplicated mapping keys and loses line numbers once data is
constructed. To report errors as ``plant.yaml:52 › areas[WELD].lines[WELD-1].ict_seconds`` we
compose the node graph first, record the line of every path, and detect duplicate keys.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from twin_core.config.errors import ConfigIssue

Loc = tuple[str | int, ...]


@dataclass
class YamlSource:
    """A parsed YAML file: plain data plus a ``path -> 1-based line`` index."""

    name: str
    path: Path
    data: Any
    lines: dict[Loc, int] = field(default_factory=dict)

    def line_of(self, loc: Sequence[str | int]) -> int | None:
        """Line of the deepest existing prefix of ``loc`` (None if nothing matches)."""
        parts = tuple(loc)
        while parts:
            line = self.lines.get(parts)
            if line is not None:
                return line
            parts = parts[:-1]
        return None

    def describe(self, loc: Sequence[str | int]) -> str:
        """Render ``loc`` against the raw data: list items by ``code``/``id`` when available.

        Location parts that do not exist in the data (pydantic union tags such as
        ``inject.failure``) are skipped when the next part does exist.
        """
        out = ""
        node: Any = self.data
        parts = list(loc)
        i = 0
        while i < len(parts):
            part = parts[i]
            if isinstance(node, dict) and isinstance(part, str) and part not in node:
                nxt = parts[i + 1] if i + 1 < len(parts) else None
                if nxt is not None and nxt in node:
                    i += 1
                    continue
            if isinstance(part, int):
                label: str = str(part)
                child: Any = None
                if isinstance(node, list) and 0 <= part < len(node):
                    child = node[part]
                    if isinstance(child, dict):
                        key = child.get("code", child.get("id"))
                        if isinstance(key, str | int) and not isinstance(key, bool):
                            label = str(key)
                out += f"[{label}]"
                node = child
            else:
                out += f".{part}" if out else str(part)
                node = node.get(part) if isinstance(node, dict) else None
            i += 1
        return out

    def issue(
        self,
        loc: Sequence[str | int],
        message: str,
        *,
        value: object = None,
        suggestion: str | None = None,
    ) -> ConfigIssue:
        return ConfigIssue(
            file=self.name,
            path=self.describe(loc),
            message=message,
            line=self.line_of(loc),
            value=value,
            suggestion=suggestion,
        )


def _index(node: yaml.Node, loc: Loc, source: YamlSource, issues: list[ConfigIssue]) -> None:
    source.lines.setdefault(loc, node.start_mark.line + 1)
    if isinstance(node, yaml.MappingNode):
        seen: dict[object, int] = {}
        for key_node, value_node in node.value:
            key: str | int
            raw_key = key_node.value if isinstance(key_node, yaml.ScalarNode) else None
            key = raw_key if isinstance(raw_key, str) else str(raw_key)
            # Integer keys (tag_map state_enum) are constructed as int by safe_load.
            if key_node.tag == "tag:yaml.org,2002:int":
                with contextlib.suppress(ValueError):
                    key = int(key)
            line = key_node.start_mark.line + 1
            if key in seen and key != "<<":
                issues.append(
                    ConfigIssue(
                        file=source.name,
                        path=source.describe((*loc, key)) if source.data is not None else str(key),
                        message=(
                            f"duplicate key '{key}' (first defined on line {seen[key]}); "
                            "YAML would silently keep only the last value"
                        ),
                        line=line,
                    )
                )
            seen.setdefault(key, line)
            source.lines.setdefault((*loc, key), line)
            _index(value_node, (*loc, key), source, issues)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            _index(item, (*loc, i), source, issues)


def read_yaml(path: Path, name: str | None = None) -> tuple[YamlSource | None, list[ConfigIssue]]:
    """Read and parse one YAML file. Returns ``(None, issues)`` if it cannot be parsed."""
    display = name or path.name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [ConfigIssue(display, "", f"required config file not found: {path}")]
    except (OSError, UnicodeDecodeError) as exc:
        return None, [ConfigIssue(display, "", f"cannot read file: {exc}")]
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark is not None else None
        column = f", column {mark.column + 1}" if mark is not None else ""
        problem = " ".join(p for p in (exc.context, exc.problem) if p) or str(exc)
        return None, [ConfigIssue(display, "", f"invalid YAML syntax{column}: {problem}", line)]
    except yaml.YAMLError as exc:
        return None, [ConfigIssue(display, "", f"invalid YAML: {exc}")]
    source = YamlSource(name=display, path=path, data=data)
    issues: list[ConfigIssue] = []
    if node is not None:
        _index(node, (), source, issues)
    if not isinstance(data, dict):
        issues.append(ConfigIssue(display, "", "top level must be a mapping (key: value)", 1))
    return source, issues
