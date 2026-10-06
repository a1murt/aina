"""Human-readable configuration errors (FR-DOM-01)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConfigIssue:
    """One problem found in one config file.

    ``path`` is a dotted path in the YAML document where list items are shown by their
    ``code``/``id`` when they have one (``areas[WELD].lines[WELD-1].ict_seconds``), otherwise by
    index (``layout.flow_path[3]``).
    """

    file: str
    path: str
    message: str
    line: int | None = None
    value: object = None
    suggestion: str | None = None

    def render(self) -> str:
        where = f"{self.file}:{self.line}" if self.line is not None else self.file
        location = f"{where} › {self.path}" if self.path else where
        text = f"{location}: {self.message}"
        if self.suggestion is not None:
            text += f"; did you mean '{self.suggestion}'?"
        return text


class ConfigError(Exception):
    """Raised when config/*.yaml cannot be loaded; carries every issue found, not just the first."""

    def __init__(self, config_dir: Path | str, issues: Sequence[ConfigIssue]) -> None:
        self.config_dir = Path(config_dir)
        self.issues: tuple[ConfigIssue, ...] = tuple(issues)
        super().__init__(self.render())

    def render(self) -> str:
        count = len(self.issues)
        noun = "problem" if count == 1 else "problems"
        lines = [f"Invalid plant configuration in {self.config_dir} ({count} {noun}):"]
        lines.extend(f"  - {issue.render()}" for issue in self.issues)
        return "\n".join(lines)
