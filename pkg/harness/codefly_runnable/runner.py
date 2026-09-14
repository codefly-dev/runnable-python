"""The invocation harness: one request in, one bound completion out."""

from __future__ import annotations

import importlib
import os
import signal
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .context import Context
from .contract import CONTRACT_FILE, Contract
from .logs import bounded_logs
from .protocol import (
    COMPLETED,
    COMPLETION_PATH_VARIABLE,
    EXIT_CODES,
    EXIT_PROTOCOL,
    FAILED,
    INTERRUPTED,
    INVALID_INPUT,
    INVALID_OUTPUT,
    MAX_ENVELOPE_BYTES,
    REQUEST_PATH_VARIABLE,
    TIMEOUT,
    ProtocolError,
    HandlerFailure,
    PROTOCOL,
    PROTOCOL_VARIABLE,
    Request,
    completion_document,
    write_completion,
)
from .schema import SchemaError


class _Deadline(Exception):
    """Raised into the handler's frame when the caller's deadline passes."""


class _Interruption(Exception):
    """Raised into the handler's frame when the launcher asks it to stop."""


@dataclass
class _Outcome:
    outcome: str
    output: dict[str, Any] | None = None
    error_kind: str = ""
    error_message: str = ""
    failure: HandlerFailure | None = None


class _Signals:
    """Records why the handler was interrupted.

    A handler that catches broadly can swallow the exception raised into its
    frame; what the signal recorded here still decides the outcome, so a
    swallowed deadline never returns as a completed invocation.
    """

    def __init__(self) -> None:
        self.outcome = ""

    def deadline(self, *_: object) -> None:
        if not self.outcome:
            self.outcome = TIMEOUT
        raise _Deadline()

    def interruption(self, *_: object) -> None:
        if not self.outcome:
            self.outcome = INTERRUPTED
        raise _Interruption()


def main() -> int:
    """Run one invocation. The return value is the process exit code."""
    root = Path(__file__).resolve().parent.parent
    try:
        contract = Contract.load(root / CONTRACT_FILE)
        if _required(PROTOCOL_VARIABLE) != PROTOCOL:
            raise ProtocolError("unsupported protocol environment")
        request_path = _required(REQUEST_PATH_VARIABLE)
        completion_path = _required(COMPLETION_PATH_VARIABLE)
        # A result document is this run's or it is nothing. An uncertain
        # outcome writes none, so an earlier attempt's document left at the
        # same path would be read as this invocation's proven result.
        _clear(completion_path)
        with open(request_path, "rb") as request_file:
            raw = request_file.read(4 * ((contract.max_input_bytes + 2) // 3) + MAX_ENVELOPE_BYTES + 1)
        request = Request.parse(raw, contract.max_input_bytes, contract.recovery, contract.runnable)
    except (OSError, KeyError, ValueError, ProtocolError) as err:
        # Nothing here is bound to an invocation identity, so there is no
        # result to write: the launcher reads the exit code alone.
        print(f"[codefly] {err}", file=sys.stderr)
        return EXIT_PROTOCOL

    with bounded_logs(contract.max_log_bytes):
        result = _invoke(contract, request, root)

    # The harness reports itself only after the handler's log budget is
    # released. Inside the bound a chatty handler would spend the budget first
    # and evict the diagnostic explaining why the invocation ended.
    #
    # Only a validated success or the operation's explicit failure is certain.
    # An INTERRUPTED result reports a handled signal while leaving the effect
    # uncertain. Crashes and invalid payloads still write no result.
    if result.outcome != COMPLETED:
        print(f"[codefly] {result.outcome}: {result.error_message}", file=sys.stderr)
    if result.outcome not in (COMPLETED, INTERRUPTED) and result.failure is None:
        return EXIT_CODES[result.outcome]
    try:
        document = completion_document(identity=request.identity, output=result.output,
                                       failure=result.failure, interrupted=result.outcome == INTERRUPTED)
        write_completion(completion_path, document, contract.max_output_bytes)
    except (OSError, ValueError, ProtocolError) as err:
        print(f"[codefly] invalid_output: {err}", file=sys.stderr)
        return EXIT_CODES[INVALID_OUTPUT]
    return EXIT_CODES[result.outcome]


def _clear(path: str) -> None:
    """Remove a document left at the result path by an earlier attempt.

    A path the harness cannot clear is one it cannot own, so the invocation
    refuses to start rather than run toward a result it may not be able to
    report.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _required(variable: str) -> str:
    value = os.environ.get(variable)
    if not value:
        raise KeyError(f"{variable} is required: the launcher chooses both document paths")
    if variable != PROTOCOL_VARIABLE and not os.path.isabs(value):
        raise ProtocolError(f"{variable} must be an absolute path")
    return value


def _invoke(contract: Contract, request: Request, root: Path) -> _Outcome:
    try:
        contract.input.validate(request.payload, "input")
    except SchemaError as err:
        return _Outcome(INVALID_INPUT, error_kind="contract", error_message=str(err))

    context = Context(
        invocation=request.identity,
        runnable=request.runnable,
        deadline=request.deadline,
        recovery=contract.recovery,
    )
    remaining = context.remaining()
    if remaining <= 0:
        return _Outcome(
            TIMEOUT,
            error_kind="deadline",
            error_message="the deadline had passed before the handler started",
        )

    signals = _Signals()
    result = _run(contract, context, request.payload, root, remaining, signals)
    if signals.outcome and result.outcome != signals.outcome:
        return _Outcome(
            signals.outcome,
            error_kind="swallowed",
            error_message=f"the invocation received a {signals.outcome} signal; "
            f"subsequent outcome: {result.outcome}: {result.error_message}",
        )
    if result.outcome != COMPLETED:
        return result

    try:
        contract.output.validate(result.output, "output")
    except SchemaError as err:
        return _Outcome(INVALID_OUTPUT, error_kind="contract", error_message=str(err))
    return result


def _run(
    contract: Contract,
    context: Context,
    payload: dict[str, Any],
    root: Path,
    remaining: float,
    signals: _Signals,
) -> _Outcome:
    previous = {
        signal.SIGALRM: signal.signal(signal.SIGALRM, signals.deadline),
    }
    if contract.cancellation == "signal":
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, signals.interruption)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    stage = "handler-import"
    try:
        handler = _load_handler(contract, root)
        if signals.outcome:
            return _Outcome(signals.outcome, error_kind="signal", error_message="interrupted while importing the handler")
        stage = "handler"
        output = handler(context, payload)
    except _Deadline:
        return _Outcome(TIMEOUT, error_kind="deadline", error_message="the deadline passed")
    except _Interruption:
        return _Outcome(
            INTERRUPTED, error_kind="signal", error_message="the invocation was interrupted"
        )
    except HandlerFailure as err:
        if stage == "handler":
            return _Outcome(FAILED, failure=err)
        return _Outcome(FAILED, error_message=_describe(err))
    except BaseException as err:  # noqa: BLE001 — a handler exit is a failure, not a success
        return _Outcome(FAILED, error_kind=stage, error_message=_describe(err))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for number, handler_before in previous.items():
            signal.signal(number, handler_before)
    if not isinstance(output, dict):
        return _Outcome(
            INVALID_OUTPUT,
            error_kind="contract",
            error_message=f"handler returned {type(output).__name__}, expected an object",
        )
    return _Outcome(COMPLETED, output=output)


def _load_handler(contract: Contract, root: Path) -> Callable[[Context, dict[str, Any]], Any]:
    source = str(root.parent)
    if source not in sys.path:
        sys.path.insert(0, source)
    module = importlib.import_module(contract.handler_module)
    handler = getattr(module, contract.handler_attribute, None)
    if not callable(handler):
        raise AttributeError(
            f"{contract.handler_module}.{contract.handler_attribute} is not callable"
        )
    return handler


def _describe(err: BaseException) -> str:
    frames = traceback.format_exception_only(type(err), err)
    return "".join(frames).strip()
