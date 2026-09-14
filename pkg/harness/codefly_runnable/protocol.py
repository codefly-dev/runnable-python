"""codefly.runnable/v1 proto3 JSON, defined by codefly-dev/core."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PROTOCOL = "codefly.runnable/v1"
PROTOCOL_VARIABLE = "CODEFLY__RUNNABLE_PROTOCOL"
REQUEST_PATH_VARIABLE = "CODEFLY__RUNNABLE_INVOCATION"
COMPLETION_PATH_VARIABLE = "CODEFLY__RUNNABLE_RESULT"
DEFAULT_MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_ENVELOPE_BYTES = 64 * 1024
# The result is readable by the launcher that started this process.
RESULT_MODE = 0o644

# Internal diagnostics. These are not RunnableCompletion outcomes.
COMPLETED = "completed"
INVALID_INPUT = "invalid_input"
INVALID_OUTPUT = "invalid_output"
FAILED = "failed"
TIMEOUT = "timeout"
INTERRUPTED = "interrupted"
EXIT_CODES = {COMPLETED: 0, INVALID_INPUT: 64, INVALID_OUTPUT: 65,
              FAILED: 66, TIMEOUT: 67, INTERRUPTED: 68}
EXIT_PROTOCOL = 69


class ProtocolError(Exception):
    """The invocation or environment does not satisfy the framing."""


class HandlerFailure(Exception):
    """An operation's explicit, certain failure; never use for an unknown effect."""
    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or not 1 <= len(code) <= 128:
            raise ValueError("failure code must contain 1 to 128 characters")
        if not isinstance(message, str):
            raise ValueError("failure message must be a string")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class InvocationIdentity:
    invocation: str
    intent: str
    effect: str = ""


@dataclass(frozen=True)
class RunnableIdentity:
    name: str
    module: str
    workspace: str
    version: str

    def document(self) -> dict[str, str]:
        return vars(self).copy()


def _identifier(document: dict, key: str, required: bool = True) -> str:
    value = document.get(key, "")
    if not isinstance(value, str) or len(value) > 128 or (required and not value):
        raise ProtocolError(f"request.{key} must contain {'1' if required else '0'} to 128 characters")
    return value


def _json(raw: bytes) -> Any:
    def constant(value):
        raise ValueError(f"invalid JSON constant {value}")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=constant, object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError) as err:
        raise ProtocolError(f"request is not UTF-8 JSON: {err}") from err


@dataclass(frozen=True)
class Request:
    identity: InvocationIdentity
    runnable: RunnableIdentity
    deadline: datetime
    payload: dict[str, Any]

    @staticmethod
    def parse(raw: bytes, max_input_bytes: int, recovery: str, expected: dict) -> "Request":
        if len(raw) > 4 * ((max_input_bytes + 2) // 3) + MAX_ENVELOPE_BYTES:
            raise ProtocolError("request exceeds the payload plus envelope byte bound")
        document = _json(raw)
        if not isinstance(document, dict):
            raise ProtocolError("request must be an object")
        if document.get("protocol") != PROTOCOL:
            raise ProtocolError(f"request protocol {document.get('protocol')!r} is not {PROTOCOL!r}")
        allowed = {"protocol", "runnable", "invocation_id", "intent_id", "effect_id", "issued_at", "deadline", "input"}
        if document.keys() - allowed:
            raise ProtocolError("request contains unknown fields")
        identity = InvocationIdentity(_identifier(document, "invocation_id"),
                                      _identifier(document, "intent_id"),
                                      _identifier(document, "effect_id", False))
        if recovery == "receipt" and not identity.effect:
            raise ProtocolError("effect_id is required for receipt recovery")
        release = document.get("runnable")
        keys = {"name", "module", "workspace", "version"}
        if not isinstance(release, dict) or release.keys() != keys:
            raise ProtocolError("request.runnable requires name, module, workspace and version")
        if any(not isinstance(v, str) or not v for v in release.values()):
            raise ProtocolError("request.runnable identity fields must be nonempty strings")
        if any(release.get(key) != value for key, value in expected.items()):
            raise ProtocolError("request names another Runnable release")
        deadline = _instant(document.get("deadline"), "deadline")
        issued = _instant(document.get("issued_at"), "issued_at")
        if deadline <= issued:
            raise ProtocolError("request.deadline must be after issued_at")
        encoded = document.get("input")
        try:
            if not isinstance(encoded, str):
                raise ValueError("input must be base64 text")
            payload_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as err:
            raise ProtocolError(f"request.input is not base64: {err}") from err
        if len(payload_bytes) > max_input_bytes:
            raise ProtocolError(f"input is {len(payload_bytes)} bytes, over the declared {max_input_bytes} byte bound")
        payload = _json(payload_bytes)
        if not isinstance(payload, dict):
            raise ProtocolError("request.input must decode to one JSON object")
        return Request(identity, RunnableIdentity(**release), deadline, payload)


def _instant(raw: Any, field: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ProtocolError(f"request.{field} is required")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", raw):
        raise ProtocolError(f"request.{field} is not an RFC 3339 instant")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as err:
        raise ProtocolError(f"request.{field} is not an RFC 3339 instant") from err


def completion_document(*, identity: InvocationIdentity, output: dict | None = None,
                        failure: HandlerFailure | None = None, interrupted: bool = False) -> dict:
    document = {"protocol": PROTOCOL, "invocation_id": identity.invocation}
    if interrupted:
        document.update(status="INTERRUPTED")
    elif failure is not None:
        document.update(status="FAILED", error={"code": failure.code, "message": str(failure)})
    else:
        payload = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        document.update(status="SUCCEEDED", output=base64.b64encode(payload).decode("ascii"))
    return document


def write_completion(path: str, document: dict, max_output_bytes: int) -> None:
    if document["status"] == "SUCCEEDED" and len(base64.b64decode(document["output"])) > max_output_bytes:
        raise ProtocolError(f"output exceeds the declared {max_output_bytes} byte bound")
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    staging = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), prefix=".result-", delete=False) as handle:
            staging = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # The rename carries the staging file's mode onto the result, and a
        # temporary file is private to this process. Whether a launcher reads
        # the result under another account is the facility's choice, not one
        # the harness should make for it by leaving the mode at 0600.
        os.chmod(staging, RESULT_MODE)
        os.replace(staging, path)
    finally:
        if staging and os.path.exists(staging):
            os.unlink(staging)
