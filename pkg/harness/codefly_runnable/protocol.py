"""Wire framing of ``codefly.runnable/v1``.

The launcher writes a request document, starts the process and reads a
completion document from a path it chose; ``stdout`` and ``stderr`` carry logs
only. See ``docs/protocol.md`` for the normative description.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PROTOCOL = "codefly.runnable/v1"
REQUEST_SCHEMA = "codefly.runnable.request/v1"
COMPLETION_SCHEMA = "codefly.runnable.completion/v1"

REQUEST_PATH_VARIABLE = "CODEFLY_RUNNABLE_REQUEST"
COMPLETION_PATH_VARIABLE = "CODEFLY_RUNNABLE_COMPLETION"

DEFAULT_MAX_LOG_BYTES = 256 * 1024

COMPLETED = "completed"
INVALID_INPUT = "invalid_input"
INVALID_OUTPUT = "invalid_output"
FAILED = "failed"
TIMEOUT = "timeout"
INTERRUPTED = "interrupted"

# A launcher distinguishes outcomes by the completion document; the exit code
# repeats the distinction so a missing completion is never read as success.
EXIT_CODES = {
    COMPLETED: 0,
    INVALID_INPUT: 64,
    INVALID_OUTPUT: 65,
    FAILED: 66,
    TIMEOUT: 67,
    INTERRUPTED: 68,
}
EXIT_PROTOCOL = 69


class ProtocolError(Exception):
    """The request or the environment does not satisfy the framing."""


@dataclass(frozen=True)
class InvocationIdentity:
    """Identity of one invocation, supplied by the caller and echoed back."""

    invocation: str
    intent: str = ""
    effect: str = ""

    @staticmethod
    def parse(raw: Any, at: str) -> "InvocationIdentity":
        if not isinstance(raw, dict):
            raise ProtocolError(f"{at} must be an object")
        invocation = raw.get("invocation")
        if not isinstance(invocation, str) or not invocation:
            raise ProtocolError(f"{at}.invocation is required")
        for key in ("intent", "effect"):
            if key in raw and not isinstance(raw[key], str):
                raise ProtocolError(f"{at}.{key} must be a string")
        return InvocationIdentity(
            invocation=invocation,
            intent=raw.get("intent", ""),
            effect=raw.get("effect", ""),
        )

    def document(self) -> dict[str, str]:
        return {"invocation": self.invocation, "intent": self.intent, "effect": self.effect}


@dataclass(frozen=True)
class RunnableIdentity:
    """Immutable release identity of the runnable being invoked."""

    name: str
    module: str = ""
    workspace: str = ""
    version: str = ""

    @staticmethod
    def parse(raw: Any, at: str) -> "RunnableIdentity":
        if not isinstance(raw, dict):
            raise ProtocolError(f"{at} must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(f"{at}.name is required")
        for key in ("module", "workspace", "version"):
            if key in raw and not isinstance(raw[key], str):
                raise ProtocolError(f"{at}.{key} must be a string")
        return RunnableIdentity(
            name=name,
            module=raw.get("module", ""),
            workspace=raw.get("workspace", ""),
            version=raw.get("version", ""),
        )

    def document(self) -> dict[str, str]:
        return {
            "name": self.name,
            "module": self.module,
            "workspace": self.workspace,
            "version": self.version,
        }


@dataclass(frozen=True)
class Request:
    """One invocation request."""

    identity: InvocationIdentity
    runnable: RunnableIdentity
    deadline: datetime
    payload: dict[str, Any]

    @staticmethod
    def parse(raw: bytes, max_input_bytes: int) -> "Request":
        if len(raw) > max_input_bytes:
            raise ProtocolError(
                f"request is {len(raw)} bytes, over the declared {max_input_bytes} byte bound"
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ProtocolError(f"request is not UTF-8 JSON: {err}") from err
        if not isinstance(document, dict):
            raise ProtocolError("request must be an object")
        if document.get("schema") != REQUEST_SCHEMA:
            raise ProtocolError(
                f"request schema {document.get('schema')!r} is not {REQUEST_SCHEMA!r}"
            )
        if document.get("protocol") != PROTOCOL:
            raise ProtocolError(
                f"request protocol {document.get('protocol')!r} is not {PROTOCOL!r}"
            )
        payload = document.get("input")
        if not isinstance(payload, dict):
            raise ProtocolError("request.input must be an object")
        return Request(
            identity=InvocationIdentity.parse(document.get("invocation"), "request.invocation"),
            runnable=RunnableIdentity.parse(document.get("runnable"), "request.runnable"),
            deadline=_parse_deadline(document.get("deadline")),
            payload=payload,
        )


def _parse_deadline(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ProtocolError("request.deadline is required: an invocation never runs unbounded")
    try:
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as err:
        raise ProtocolError(f"request.deadline {raw!r} is not an RFC 3339 instant") from err
    if deadline.tzinfo is None:
        raise ProtocolError(f"request.deadline {raw!r} has no time zone")
    return deadline.astimezone(timezone.utc)


def completion_document(
    *,
    identity: InvocationIdentity,
    runnable: RunnableIdentity,
    outcome: str,
    recovery: str,
    output: dict[str, Any] | None = None,
    error_kind: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    """Build the completion bound to the identity the request carried."""
    document: dict[str, Any] = {
        "schema": COMPLETION_SCHEMA,
        "protocol": PROTOCOL,
        "invocation": identity.document(),
        "runnable": runnable.document(),
        "outcome": outcome,
        "recovery": recovery,
    }
    if outcome == COMPLETED:
        document["output"] = output
    else:
        document["error"] = {"kind": error_kind or outcome, "message": error_message}
    return document


def write_completion(path: str, document: dict[str, Any], max_output_bytes: int) -> None:
    """Write the completion atomically, refusing an over-bound payload.

    The rename is what makes a truncated write unreadable rather than
    ambiguous: a launcher either sees the whole document or no document.
    """
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if document.get("outcome") == COMPLETED:
        payload = json.dumps(
            document.get("output"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > max_output_bytes:
            raise ProtocolError(
                f"output is {len(payload)} bytes, over the declared {max_output_bytes} byte bound"
            )
    staging = path + ".partial"
    with open(staging, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)
