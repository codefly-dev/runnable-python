"""Bounded log streams.

A handler's diagnostics never decide an outcome, so an over-talkative handler
is truncated with a visible marker instead of failing the invocation.
"""

from __future__ import annotations

import sys
from typing import TextIO

TRUNCATION_MARKER = "\n[codefly] log truncated at {limit} bytes\n"


class BoundedStream:
    """A text stream that stops forwarding once the byte bound is reached."""

    def __init__(self, wrapped: TextIO, limit: int) -> None:
        self._wrapped = wrapped
        self._limit = limit
        self._written = 0
        self._truncated = False

    def write(self, text: str) -> int:
        size = len(text.encode("utf-8"))
        if self._truncated:
            return len(text)
        if self._written + size > self._limit:
            self._truncated = True
            self._wrapped.write(TRUNCATION_MARKER.format(limit=self._limit))
            self._wrapped.flush()
            return len(text)
        self._written += size
        return self._wrapped.write(text)

    def flush(self) -> None:
        self._wrapped.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._wrapped.fileno()

    @property
    def truncated(self) -> bool:
        return self._truncated


class bounded_logs:
    """Bound both log streams for the duration of handler execution."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._stdout: TextIO | None = None
        self._stderr: TextIO | None = None

    def __enter__(self) -> "bounded_logs":
        self._stdout, self._stderr = sys.stdout, sys.stderr
        sys.stdout = BoundedStream(self._stdout, self._limit)
        sys.stderr = BoundedStream(self._stderr, self._limit)
        return self

    def __exit__(self, *_: object) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout, sys.stderr = self._stdout, self._stderr
