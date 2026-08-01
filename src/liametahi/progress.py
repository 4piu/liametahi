"""Interactive progress reporting (TTY only).

A run spends minutes inside three phases -- an IMAP fetch, a sequence of
model calls, and a sequence of mailbox mutations -- and until now said
nothing while it did. `info` logging marks the phase boundaries, which
is the right granularity for a cron log, but at a terminal it leaves
long stretches where nothing at all appears and the tool looks wedged.

This module supplies the interactive half. `Progress` is the protocol the
pipeline calls; `NullProgress` is the default and does nothing, so
non-interactive runs (cron, redirected output, `--format json` piped
somewhere) behave exactly as before, byte for byte. `TtyProgress` renders
a single self-erasing status line to stderr and is selected by `cli.py`
only when stderr is a terminal.

Why a thread: the slow steps here are *few and long* rather than many and
quick -- one model call can take a minute -- so a bar that only advances
on completion would sit frozen through the very wait it exists to
explain. A low-frequency repaint keeps an elapsed counter moving, which
is the part that actually tells a human the process is alive.
"""

import itertools
import shutil
import sys
import threading
import time
from types import TracebackType
from typing import IO, Protocol


class Progress(Protocol):
    """What the pipeline calls. Every method must be safe to call from a
    non-interactive run, cheap, and never raise -- progress reporting is
    cosmetic and must never be able to fail a run."""

    def start(
        self, label: str, total: int | None = None, *, unit: str = "mails"
    ) -> None: ...
    def advance(self, n: int = 1) -> None: ...
    def stop(self) -> None: ...


class NullProgress:
    """The default. Does nothing, so a non-TTY run is unchanged."""

    def start(
        self, label: str, total: int | None = None, *, unit: str = "mails"
    ) -> None:
        return

    def advance(self, n: int = 1) -> None:
        return

    def stop(self) -> None:
        return


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class TtyProgress:
    """A single self-erasing status line on stderr, repainted ~8x/second
    by a daemon thread so the elapsed counter keeps moving during a long
    model call.

    Also exposes `clear()`, which `liametahi.logging` calls before it
    emits a record: both write to stderr, so without that the log line
    would be printed over the half-drawn status line.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.RLock()
        self._label: str | None = None
        self._total: int | None = None
        self._unit = "mails"
        self._done = 0
        self._started_at = 0.0
        self._spin = itertools.cycle(_SPINNER)
        self._painted = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Progress protocol ------------------------------------------

    def start(
        self, label: str, total: int | None = None, *, unit: str = "mails"
    ) -> None:
        with self._lock:
            self._label = label
            self._total = total
            self._unit = unit
            self._done = 0
            self._started_at = time.monotonic()
        self._ensure_thread()
        self._paint()

    def advance(self, n: int = 1) -> None:
        with self._lock:
            self._done += n
        self._paint()

    def stop(self) -> None:
        with self._lock:
            self._label = None
        self.clear()

    # --- Used by liametahi.logging ----------------------------------

    def clear(self) -> None:
        """Erase the status line so another writer can use stderr."""
        with self._lock:
            if not self._painted:
                return
            self._painted = False
            try:
                self._stream.write("\r\x1b[K")
                self._stream.flush()
            except OSError, ValueError:  # pragma: no cover - closed stream
                pass

    def close(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self.clear()

    def __enter__(self) -> TtyProgress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Internals ---------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        # Daemon: a repaint loop must never keep the process alive, and
        # it holds no state the run depends on.
        self._thread = threading.Thread(
            target=self._repaint_loop, name="liametahi-progress", daemon=True
        )
        self._thread.start()

    def _repaint_loop(self) -> None:
        while not self._stop_event.wait(0.125):
            self._paint()

    def _render(self) -> str | None:
        if self._label is None:
            return None
        elapsed = time.monotonic() - self._started_at
        parts = [next(self._spin), self._label]
        if self._total:
            # The unit is not decoration: without it "1/3" alongside a log
            # line reading "batch 2/3" invites reading the bar as an index
            # into the same sequence, when it is a completed count of a
            # different thing entirely.
            parts.append(f"{self._done}/{self._total} {self._unit}")
            width = 24
            filled = int(width * min(self._done / self._total, 1.0))
            parts.append("[" + "#" * filled + "-" * (width - filled) + "]")
        elif self._done:
            parts.append(str(self._done))
        parts.append(f"{elapsed:5.1f}s")
        line = " ".join(parts)
        # Never wrap: a wrapped line cannot be erased with one \r.
        limit = max(shutil.get_terminal_size((80, 24)).columns - 1, 20)
        return line[:limit]

    def _paint(self) -> None:
        with self._lock:
            line = self._render()
            if line is None:
                return
            try:
                self._stream.write("\r\x1b[K" + line)
                self._stream.flush()
                self._painted = True
            except OSError, ValueError:  # pragma: no cover - closed stream
                pass
