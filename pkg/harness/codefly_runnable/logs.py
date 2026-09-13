"""Bound process file descriptors, including native and inherited child writes."""

from __future__ import annotations

import os
import select
import sys
import threading
import time

TRUNCATION_MARKER = "\n[codefly] log truncated at {limit} bytes\n"


class _Stream:
    def __init__(self, descriptor: int, limit: int) -> None:
        self.descriptor = descriptor
        self.limit = limit
        self.saved = os.dup(descriptor)
        self.reader, writer = os.pipe()
        self.stop_at: float | None = None
        self.thread = threading.Thread(target=self._drain, daemon=True)
        os.dup2(writer, descriptor)
        os.close(writer)
        self.thread.start()

    def _write(self, data: bytes) -> None:
        while data:
            written = os.write(self.saved, data)
            data = data[written:]

    def _drain(self) -> None:
        written = 0
        truncated = False
        try:
            while self.stop_at is None or time.monotonic() < self.stop_at:
                ready, _, _ = select.select([self.reader], [], [], 0.05)
                if not ready:
                    if self.stop_at is not None:
                        break
                    continue
                data = os.read(self.reader, 8192)
                if not data:
                    break
                keep = data[:max(0, self.limit - written)]
                self._write(keep)
                written += len(keep)
                if len(keep) < len(data) and not truncated:
                    self._write(TRUNCATION_MARKER.format(limit=self.limit).encode())
                    truncated = True
        except (BrokenPipeError, OSError):
            # A detached log consumer cannot change an invocation's outcome.
            pass
        finally:
            os.close(self.reader)

    def close(self) -> None:
        os.dup2(self.saved, self.descriptor)
        # A child which outlives its handler may still hold the pipe open.
        # Drain pending diagnostics without waiting indefinitely for that child.
        self.stop_at = time.monotonic() + 0.2
        self.thread.join()
        os.close(self.saved)


class bounded_logs:
    """Forward at most limit bytes plus one truncation marker per stream.

    The launcher must also supervise the whole process group and bound its log
    transport. A harness cannot control a child after the harness has exited.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.streams: list[_Stream] = []

    def __enter__(self) -> "bounded_logs":
        sys.stdout.flush()
        sys.stderr.flush()
        self.streams = [_Stream(1, self.limit), _Stream(2, self.limit)]
        return self

    def __exit__(self, *_: object) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        for stream in self.streams:
            stream.close()
