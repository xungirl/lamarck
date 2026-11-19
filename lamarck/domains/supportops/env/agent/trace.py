"""Structured run traces.

Every event is one JSON object per line (JSONL), so traces can be streamed,
grepped, loaded with pandas, or shipped to a log pipeline unchanged. Events
are also kept in memory so the eval scorers can read them without re-parsing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self


class Tracer:
    def __init__(self, path: str | Path | None = None, *, run_id: str = "run", **context: Any) -> None:
        self.run_id = run_id
        self.context = context  # e.g. task_id, config name, repeat index: copied into every event
        self.events: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()
        self._fh: IO[str] | None = None
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {
            "event": event,
            "run_id": self.run_id,
            "t_ms": round((time.perf_counter() - self._t0) * 1000, 1),
            **self.context,
            **fields,
        }
        self.events.append(record)
        if self._fh is not None:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        return record

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
