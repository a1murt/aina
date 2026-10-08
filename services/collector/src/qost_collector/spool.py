"""Store-and-forward spool (FR-ING-03): append-only segments on disk, replayed in order.

``{dir}/{seq:012d}.jsonl`` segments of ~``segment_bytes``; one line = one batch (a JSON array of
events). ``cursor.json`` holds the position of the next unsent line and is rewritten atomically
after every acknowledged batch, so a crash re-sends at most one batch (harmless: writes are
idempotent by ``event_id``). A torn last line (crash while appending) is truncated on open.
Drained segments are deleted; beyond ``max_bytes`` the oldest segment is dropped (counted).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger("qost_collector.spool")


@dataclass(frozen=True, slots=True)
class Cursor:
    segment: int
    offset: int


class Spool:
    def __init__(self, directory: Path, *, segment_bytes: int, max_bytes: int, fsync: bool = True):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.segment_bytes = segment_bytes
        self.max_bytes = max_bytes
        self.fsync = fsync
        self.dropped_segments = 0
        self.appended = 0
        self._cursor = self._load_cursor()
        self._repair_tail()

    # ------------------------------------------------------------------ files

    def _segments(self) -> list[int]:
        return sorted(int(p.stem) for p in self.dir.glob("*.jsonl") if p.stem.isdigit())

    def _path(self, seq: int) -> Path:
        return self.dir / f"{seq:012d}.jsonl"

    def _load_cursor(self) -> Cursor:
        path = self.dir / "cursor.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return Cursor(int(data["segment"]), int(data["offset"]))
            except (ValueError, KeyError):
                log.warning("spool_cursor_corrupt", path=str(path))
        segs = self._segments()
        return Cursor(segs[0] if segs else 0, 0)

    def _save_cursor(self) -> None:
        path = self.dir / "cursor.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"segment": self._cursor.segment, "offset": self._cursor.offset}))
        os.replace(tmp, path)

    def _repair_tail(self) -> None:
        segs = self._segments()
        if not segs:
            return
        path = self._path(segs[-1])
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n") + 1
            with path.open("r+b") as fh:
                fh.truncate(cut)
            log.warning("spool_torn_tail", path=str(path), dropped_bytes=len(data) - cut)

    # ------------------------------------------------------------------ api

    @property
    def size_bytes(self) -> int:
        total = 0
        for seq in self._segments():
            size = self._path(seq).stat().st_size
            total += size - (self._cursor.offset if seq == self._cursor.segment else 0)
        return total

    def empty(self) -> bool:
        for seq in self._segments():
            if seq < self._cursor.segment:
                continue
            size = self._path(seq).stat().st_size
            if size > (self._cursor.offset if seq == self._cursor.segment else 0):
                return False
        return True

    def append(self, line: str) -> None:
        data = (line.rstrip("\n") + "\n").encode()
        segs = self._segments()
        seq = segs[-1] if segs else self._cursor.segment
        path = self._path(seq)
        if (
            path.exists()
            and path.stat().st_size + len(data) > self.segment_bytes
            and path.stat().st_size > 0
        ):
            seq += 1
            path = self._path(seq)
        with path.open("ab") as fh:
            fh.write(data)
            fh.flush()
            if self.fsync:
                os.fsync(fh.fileno())
        self.appended += 1
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        while True:
            segs = self._segments()
            if len(segs) <= 1:
                return
            total = sum(self._path(s).stat().st_size for s in segs)
            if total <= self.max_bytes:
                return
            oldest = segs[0]
            self._path(oldest).unlink()
            self.dropped_segments += 1
            log.error("spool_cap_dropped_segment", segment=oldest, max_bytes=self.max_bytes)
            if self._cursor.segment <= oldest:
                self._cursor = Cursor(segs[1], 0)
                self._save_cursor()

    def peek(self) -> tuple[Cursor, str] | None:
        """The next unsent batch line and where it ends (``ack`` that cursor after sending)."""
        for seq in self._segments():
            if seq < self._cursor.segment:
                self._path(seq).unlink(missing_ok=True)
                continue
            offset = self._cursor.offset if seq == self._cursor.segment else 0
            path = self._path(seq)
            with path.open("rb") as fh:
                fh.seek(offset)
                raw = fh.readline()
            if raw.endswith(b"\n"):
                return Cursor(seq, offset + len(raw)), raw.decode()
            if seq != self._segments()[-1]:
                continue
            return None
        return None

    def ack(self, cursor: Cursor) -> None:
        self._cursor = cursor
        path = self._path(cursor.segment)
        segs = self._segments()
        if path.exists() and cursor.offset >= path.stat().st_size and cursor.segment != segs[-1]:
            path.unlink()
            self._cursor = Cursor(cursor.segment + 1, 0)
        self._save_cursor()


__all__ = ["Cursor", "Spool"]
