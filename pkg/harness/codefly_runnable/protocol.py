"""codefly.runnable.served/v1: what one call carries, defined by codefly-dev/core.

A runnable is called, not launched. The bounded input document is the whole
request body — a field Codefly added to it would be a field the operation never
described — and the per-call facts that are not the body travel as headers whose
spellings core pins. They are restated here because the harness runs inside a
package that carries no Go dependency: a harness reading one spelling while its
caller writes another is an unreachable owner, which reads as an outage rather
than as a mistake in a string.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

PROTOCOL = "codefly.runnable.served/v1"

# The address the placement allocated. It is the one fact the harness cannot
# derive: a harness choosing its own port would be right on one machine for ten
# minutes.
ADDRESS_VARIABLE = "CODEFLY__RUNNABLE_ADDRESS"

# The routes a generated harness answers on, and the headers one call's facts
# arrive in. All six are core's constants (codefly-dev/core runnable/served.go).
INVOKE_PROCEDURE = "/codefly.runnable.v0.Runnable/Invoke"
LOOKUP_PROCEDURE = "/codefly.runnable.v0.Runnable/Lookup"
WORK_CONTEXT_HEADER = "X-Codefly-Work-Context"
EFFECT_HEADER = "Codefly-Runnable-Effect-Id"
DEADLINE_HEADER = "Codefly-Runnable-Deadline"
FAILURE_CODE_HEADER = "Codefly-Runnable-Failure-Code"

# Core's bounds on the per-call facts (codefly-dev/core
# proto/codefly/base/v0/runnable_invocation.proto): RunnableInvocationIdentity's
# work_context and RunnableInvocation's effect_id. A value over them is not one
# any caller core admits could have sent.
MAX_WORK_CONTEXT_LENGTH = 8192
MAX_EFFECT_ID_LENGTH = 128

# The failure-code grammar the durable-work runtime reads the header with. It is
# narrower than core's RunnableError.code (any 1 to 128 characters), so a code
# that satisfies it satisfies both, and a code outside it would be read by that
# caller as no assertion at all while the handler believed it had asserted.
FAILURE_CODE_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")

# How long reading one request may take, its head and its body together. It is
# one wall-clock deadline rather than a bound on each read, which a peer sending
# a byte just inside it would hold open forever.
REQUEST_READ_SECONDS = 10.0

# How long one write of an answer may stall before the connection is dropped.
WRITE_STALL_SECONDS = 10.0

# How long a kept-alive connection may sit idle between calls. It is longer than
# the idle timeout of the pool the durable-work runtime calls through (Go's
# net/http, 90 s by default), so the caller retires an idle connection before
# the harness closes it: a call written onto a connection the harness had just
# closed would come back as a reset, an uncertain outcome for a call that never
# ran. Answers advertise it in a Keep-Alive header for clients that read one.
IDLE_SECONDS = 120.0

# The request line and every header together. The per-call facts need under
# 9 KiB at core's bounds; the stdlib alone would admit 100 lines of 64 KiB.
MAX_HEAD_BYTES = 32 * 1024

# Memory for requests being read, head and body. It sizes how many requests are
# read at once: a request over that count is refused before anything of it is
# read, and a call holds no slot while its request is still arriving.
READ_MEMORY_BYTES = 256 * 1024 * 1024
MAX_READERS = 1024

# Idle connections kept beside every request being read and every call
# running. Past them, a new connection closes the one idle the longest.
IDLE_CONNECTIONS = 64

# The harness's own bound on simultaneous calls when the declaration states
# none (concurrency 0, which core leaves to the facility). It is this harness's
# number, not one any placement resolved; see docs/protocol.md.
HARNESS_CONCURRENCY = 32

# How long a call in flight has to finish after a signal. A handler interrupted
# mid-effect leaves it unproven, so the process waits rather than making every
# call in flight unproven at once.
SHUTDOWN_GRACE_SECONDS = 30.0


class ProtocolError(Exception):
    """The call does not satisfy the framing, so the handler never ran."""


class HandlerFailure(Exception):
    """An operation's explicit, certain failure: the handler knows its effect did not happen.

    Raising it is the ONLY thing that sets the failure-code header, and that
    header is the only thing that proves to a caller that nothing committed.
    Never raise it for an effect whose fate the handler does not know.
    """

    def __init__(self, code: str, message: str) -> None:
        # The code becomes a response header, so it is held to a grammar that
        # cannot carry a line break or a character a header cannot encode.
        if not isinstance(code, str) or not FAILURE_CODE_PATTERN.fullmatch(code):
            raise ValueError(f"failure code must match {FAILURE_CODE_PATTERN.pattern}")
        if not isinstance(message, str):
            raise ValueError("failure message must be a string")
        self._code = code
        super().__init__(message)

    @property
    def code(self) -> str:
        """The validated code. Read-only: what was checked is what is sent."""
        return self._code


@dataclass(frozen=True)
class RunnableIdentity:
    """The release this package implements."""

    name: str
    module: str
    workspace: str
    version: str

    def document(self) -> dict[str, str]:
        return vars(self).copy()


@dataclass(frozen=True)
class InvocationIdentity:
    """What the caller said about this call.

    ``effect`` is the caller's idempotency key: an effect a handler records
    under it is the one the caller looks up when an outcome is uncertain.

    ``work_context`` is the caller's minted capability, carried verbatim. A
    handler forwards it to whatever it calls in turn and never parses or
    reconstructs it; the harness does neither too. It is kept out of the repr,
    so a handler logging its context does not log the capability.
    """

    effect: str
    work_context: str = field(repr=False)


@dataclass(frozen=True)
class Call:
    """One validated call: the per-call facts, the payload and the budget."""

    identity: InvocationIdentity
    runnable: RunnableIdentity
    deadline: datetime
    budget: float
    payload: dict[str, Any]


def decode(raw: bytes) -> Any:
    """Decode one JSON document, refusing a repeated key.

    json keeps the last of a repeated key, which would let one document mean two
    things to two readers. Numbers are kept as written, so the bounded profile's
    integer stays distinct from a value carrying a fraction.
    """

    def constant(value):
        raise ValueError(f"invalid JSON constant {value}")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), parse_constant=constant, object_pairs_hook=unique)


def header(headers, name: str) -> str:
    """The one value of a per-call header, or "" when the call carries none.

    A repeated header is refused rather than resolved to its first value: one
    call naming two identities means two things to two readers. So is a value
    carrying a control character, which is how an obsolete line fold arrives.
    """
    values = headers.get_all(name) or []
    if len(values) > 1:
        raise ProtocolError(f"{name} is repeated")
    value = values[0] if values else ""
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ProtocolError(f"{name} carries a control character")
    return value


def read_call(headers, body: bytes, contract) -> Call:
    """Read the per-call facts and the bounded body of one call.

    Every refusal here happened before the handler ran, so nothing committed —
    but a refused request is not the operation speaking, and only the operation's
    own typed failure proves a no-effect failure. The caller reads these as
    unproven and retries under its budget, which is harmless for a call that
    never ran.
    """
    if len(body) > contract.max_input_bytes:
        raise ProtocolError(
            f"input is {len(body)} bytes, over the declared {contract.max_input_bytes} byte bound"
        )

    work_context = header(headers, WORK_CONTEXT_HEADER)
    if not work_context:
        # A call with no identity is the one thing the contract exists to
        # prevent, so it is refused rather than run under whatever identity this
        # process happens to have.
        raise ProtocolError("the call carries no Work Context")
    if len(work_context) > MAX_WORK_CONTEXT_LENGTH:
        raise ProtocolError(f"the Work Context is over core's {MAX_WORK_CONTEXT_LENGTH} character bound")
    effect = header(headers, EFFECT_HEADER)
    if len(effect) > MAX_EFFECT_ID_LENGTH:
        raise ProtocolError(f"the effect identity is over core's {MAX_EFFECT_ID_LENGTH} character bound")
    if contract.recovery == "receipt" and not effect:
        raise ProtocolError("an effect identity is required for receipt recovery")

    budget = contract.timeout_seconds
    raw_deadline = header(headers, DEADLINE_HEADER)
    if raw_deadline:
        deadline = _instant(raw_deadline)
        # The caller's instant read against this harness's clock, so an offset
        # between the two clocks moves it: a harness clock running ahead
        # shortens the budget, and one running behind extends it — never past
        # the declared timeout, which bounds every call.
        budget = (deadline - datetime.now(timezone.utc)).total_seconds()
        if budget <= 0:
            raise ProtocolError("the deadline had passed before the call was read")
        budget = min(budget, contract.timeout_seconds)

    try:
        decoded = decode(body)
    except (UnicodeDecodeError, ValueError) as err:
        raise ProtocolError(f"request body is not UTF-8 JSON: {err}") from err
    except RecursionError as err:
        raise ProtocolError("request body nests deeper than the decoder reads") from err
    if not isinstance(decoded, dict):
        raise ProtocolError("request body must be one JSON object")

    return Call(
        identity=InvocationIdentity(effect=effect, work_context=work_context),
        runnable=contract.release,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=budget),
        budget=budget,
        payload=decoded,
    )


def encode_output(output: Any, max_output_bytes: int) -> bytes:
    """Render what the handler returned as the answer's body.

    The bound is on the document that is actually sent, so it is measured after
    encoding rather than estimated from the value.
    """
    if not isinstance(output, dict):
        raise ProtocolError(f"handler returned {type(output).__name__}, expected an object")
    try:
        encoded = json.dumps(
            output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as err:
        raise ProtocolError(f"output is not JSON: {err}") from err
    if len(encoded) > max_output_bytes:
        raise ProtocolError(f"output is {len(encoded)} bytes, over the declared {max_output_bytes} byte bound")
    return encoded


def _instant(raw: str) -> datetime:
    """Read an RFC 3339 instant, as the deadline header spells one."""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as err:
        raise ProtocolError(f"{DEADLINE_HEADER} is not an RFC 3339 instant") from err
    if parsed.tzinfo is None:
        raise ProtocolError(f"{DEADLINE_HEADER} carries no offset, so it names no instant")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError as err:
        # An offset that carries the instant outside the representable range.
        raise ProtocolError(f"{DEADLINE_HEADER} names no representable instant") from err
