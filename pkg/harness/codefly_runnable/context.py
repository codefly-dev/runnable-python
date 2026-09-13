"""What a handler is given besides its input."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from .protocol import InvocationIdentity, RunnableIdentity


@dataclass(frozen=True)
class Context:
    """The invocation a handler is running under.

    The identity is the caller's, not one the handler invents: an effect a
    handler records under ``effect`` is the same effect the caller will look up
    when an outcome is uncertain.
    """

    invocation: InvocationIdentity
    runnable: RunnableIdentity
    deadline: datetime
    recovery: str

    def remaining(self) -> float:
        """Seconds left before the deadline; negative once it has passed."""
        return (self.deadline - datetime.now(timezone.utc)).total_seconds()

    def log(self, message: str) -> None:
        """Write a diagnostic. Logs never carry invocation output."""
        print(message, file=sys.stderr)
