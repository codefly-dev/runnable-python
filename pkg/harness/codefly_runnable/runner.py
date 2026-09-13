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
        request_path = _required(REQUEST_PATH_VARIABLE)
        completion_path = _required(COMPLETION_PATH_VARIABLE)
        with open(request_path, "rb") as request_file:
            raw = request_file.read(contract.max_input_bytes + MAX_ENVELOPE_BYTES + 1)
        request = Request.parse(raw, contract.max_input_bytes)
    except (OSError, KeyError, ValueError, ProtocolError) as err:
        # Nothing here is bound to an invocation identity, so there is no
        # completion to write: the launcher reads the exit code alone.
        print(f"[codefly] {err}", file=sys.stderr)
        return EXIT_PROTOCOL

    with bounded_logs(contract.max_log_bytes):
        result = _invoke(contract, request, root)

    document = completion_document(
        identity=request.identity,
        runnable=request.runnable,
        outcome=result.outcome,
        recovery=contract.recovery,
        output=result.output,
        error_kind=result.error_kind,
        error_message=result.error_message,
    )
    try:
        write_completion(completion_path, document, contract.max_output_bytes)
    except ProtocolError as err:
        document = completion_document(
            identity=request.identity,
            runnable=request.runnable,
            outcome=INVALID_OUTPUT,
            recovery=contract.recovery,
            error_kind="payload-bound",
            error_message=str(err),
        )
        write_completion(completion_path, document, contract.max_output_bytes)
        return EXIT_CODES[INVALID_OUTPUT]
    return EXIT_CODES[result.outcome]


def _required(variable: str) -> str:
    value = os.environ.get(variable)
    if not value:
        raise KeyError(f"{variable} is required: the launcher chooses both document paths")
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
        signal.SIGTERM: signal.signal(signal.SIGTERM, signals.interruption),
        signal.SIGINT: signal.signal(signal.SIGINT, signals.interruption),
    }
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
