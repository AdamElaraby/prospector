"""Real progress reporting, for a progress bar that means something.

The web UI runs each stage as a subprocess and tails its stdout. A spinner
driven by "is the process alive" tells you nothing — it looks identical at 2%
and 98%, and identical again when a scraper has silently hung. So the stages
emit their actual position instead, and the bar is driven by that.

One marker line per step:

    @@PROSPECTOR-PROGRESS@@ {"label": "auditing", "done": 7, "total": 40, ...}

Markers are only emitted when PROSPECTOR_PROGRESS=1, which the web UI sets on
the subprocess it starts. A person running the CLI in a terminal gets the
normal output and never sees them.

Totals are honest: a stage that genuinely cannot know its total up front (a
scraper paging until Google stops) reports total=None, and the UI shows an
indeterminate bar rather than a made-up percentage creeping towards 100.
"""

from __future__ import annotations

import json
import os
import time

MARKER = "@@PROSPECTOR-PROGRESS@@"


def enabled() -> bool:
    return os.environ.get("PROSPECTOR_PROGRESS") == "1"


def emit(payload: dict) -> None:
    if not enabled():
        return
    # flush: the reader is a pipe, and a progress update buffered until the
    # stage finishes is not progress.
    print(f"{MARKER} {json.dumps(payload)}", flush=True)


def parse(line: str) -> dict | None:
    """Pull a progress payload out of a log line, or None if it is just output."""
    if not line.startswith(MARKER):
        return None
    try:
        payload = json.loads(line[len(MARKER):].strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class Progress:
    """One stage's progress. `total=None` means 'unknown, still working'."""

    def __init__(self, label: str, total: int | None = None, *, note: str = "") -> None:
        self.label = label
        self.total = total
        self.done = 0
        self.note = note
        self.started = time.monotonic()
        self._emit()

    def set_total(self, total: int | None) -> None:
        """Call once the real size is known — e.g. after the query returns."""
        self.total = total
        self._emit()

    def advance(self, step: int = 1, note: str | None = None) -> None:
        self.done += step
        if note is not None:
            self.note = note
        self._emit()

    def finish(self, note: str | None = None) -> None:
        if self.total is not None:
            self.done = self.total
        if note is not None:
            self.note = note
        self._emit(done=True)

    def _emit(self, done: bool = False) -> None:
        elapsed = time.monotonic() - self.started
        remaining = None
        # Only once there is enough of a sample for the estimate not to be
        # noise — an ETA from one item is a wild guess shown as a fact.
        if self.total and self.done >= 3 and not done:
            rate = self.done / elapsed if elapsed > 0 else 0
            if rate > 0:
                remaining = max(0, round((self.total - self.done) / rate))
        emit(
            {
                "label": self.label,
                "done": self.done,
                "total": self.total,
                "note": self.note,
                "elapsed": round(elapsed, 1),
                "eta": remaining,
                "finished": done,
            }
        )


def step(label: str, index: int, total: int, note: str = "") -> None:
    """One-shot marker, for stages that are a fixed list of phases."""
    emit({"label": label, "done": index, "total": total, "note": note, "finished": False})
