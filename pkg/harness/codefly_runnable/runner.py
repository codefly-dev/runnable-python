"""The harness: it serves the operation until it is signalled.

One call is one POST to the procedure core pins, with the bounded input document
as the whole body. A success answers 200 with the bounded output document the
same way. Nothing here is per-process state, so concurrent calls are independent.

The failure-code header is set in exactly one place — the handler's own
``HandlerFailure`` — because it is the only thing that proves to a caller that no
effect committed. A crash, an untyped exception, a deadline and a payload the
contract refuses all answer without it and leave the effect unproven: the harness
cannot know which side of its effect a handler stopped on, and a caller that
believed otherwise would stop looking for a receipt that exists.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import socket
import sys
import threading
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .context import Context
from .contract import CONTRACT_FILE, RECEIPT_ATTRIBUTE, Contract
from .protocol import (
    ADDRESS_VARIABLE,
    FAILURE_CODE_HEADER,
    INVOKE_PROCEDURE,
    LOOKUP_PROCEDURE,
    MAX_ENVELOPE_BYTES,
    SHUTDOWN_GRACE_SECONDS,
    Call,
    HandlerFailure,
    ProtocolError,
    encode_output,
    read_call,
)
from .schema import SchemaError


@dataclass
class _Answer:
    """One call's answer before it becomes a response."""

    status: int
    body: bytes = b""
    code: str = ""
    message: str = ""
    failure: HandlerFailure | None = None


def main() -> int:
    """Serve this runnable until it is signalled. The return value is the exit code."""
    root = Path(__file__).resolve().parent.parent
    try:
        contract = Contract.load(root / CONTRACT_FILE)
        address = os.environ.get(ADDRESS_VARIABLE)
        if not address:
            raise ProtocolError(
                f"{ADDRESS_VARIABLE} is required: the port is allocated by whatever placed this runnable"
            )
        handle = _load(contract, root, contract.handler_attribute)
        # A receipt-recovery package serves the receipt route, so a handler
        # module without one is refused at startup rather than at the call that
        # needed it: an owner serving no receipt cannot report an effect it did
        # commit, and a caller reads that silence as inconclusive forever.
        receipt_of = _load(contract, root, RECEIPT_ATTRIBUTE) if contract.recovery == "receipt" else None
        server = _server(address, contract, handle, receipt_of)
    except (OSError, KeyError, ValueError, ProtocolError, AttributeError, ImportError) as err:
        print(f"[codefly] {err}", file=sys.stderr)
        return 1

    stopping = threading.Event()

    def stop(*_: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        # shutdown() blocks until the serving loop ends, and a signal handler
        # runs on the thread that is inside it.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        # Calls already in flight are given their grace: cutting them off would
        # turn every one of them into an unproven effect at once, which is what
        # the rest of this harness spends its code avoiding.
        watchdog = threading.Timer(SHUTDOWN_GRACE_SECONDS, lambda: os._exit(0))
        watchdog.daemon = True
        watchdog.start()
        server.server_close()
        watchdog.cancel()
    return 0


def _server(address: str, contract: Contract, handle, receipt_of) -> ThreadingHTTPServer:
    host, port = _listen(address)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
            if self.path == INVOKE_PROCEDURE:
                self._answer(_invoke(contract, handle, self._call()))
            elif self.path == LOOKUP_PROCEDURE and receipt_of is not None:
                self._answer(_lookup(contract, receipt_of, self._call()))
            else:
                self._answer(_Answer(404, code="unimplemented", message=f"{self.path} is not served"))

        def do_GET(self) -> None:  # noqa: N802
            self._answer(_Answer(405, code="unimplemented", message="the operation is called with POST"))

        def _call(self) -> Call | ProtocolError:
            limit = contract.max_input_bytes + MAX_ENVELOPE_BYTES
            declared = int(self.headers.get("Content-Length") or 0)
            if declared > limit:
                return ProtocolError(
                    f"input is {declared} bytes, over the declared {contract.max_input_bytes} byte bound"
                )
            try:
                return read_call(self.headers, self.rfile.read(declared), contract)
            except ProtocolError as err:
                return err

        def _answer(self, answer: _Answer) -> None:
            if answer.failure is not None:
                # The one place this header is set. A caller reads it before the
                # status, and without it no status proves anything.
                self.send_response(answer.status)
                self.send_header(FAILURE_CODE_HEADER, answer.failure.code)
                body = _error("failed_precondition", str(answer.failure))
            elif answer.status == 200:
                self.send_response(200)
                body = answer.body
            else:
                self.send_response(answer.status)
                body = _error(answer.code, answer.message)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if answer.message:
                print(f"[codefly] {answer.message}", file=sys.stderr)

        def log_message(self, *_: object) -> None:
            """Diagnostics are what the harness prints deliberately, not a request log."""

    server = ThreadingHTTPServer.__new__(ThreadingHTTPServer)
    ThreadingHTTPServer.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    ThreadingHTTPServer.__init__(server, (host, port), Handler)
    # In-flight calls are joined at close; the grace above bounds how long.
    server.daemon_threads = False
    server.block_on_close = True
    return server


def _listen(address: str) -> tuple[str, int]:
    """Split the allocated host:port. An address with no port names no listener."""
    if address.startswith("["):
        host, _, port = address.partition("]:")
        host = host[1:]
    else:
        host, _, port = address.rpartition(":")
    if not port.isdigit():
        raise ProtocolError(f"{ADDRESS_VARIABLE}={address!r} is not host:port")
    return host, int(port)


def _invoke(contract: Contract, handle, call: Call | ProtocolError) -> _Answer:
    """Answer one call: validate the payload, run the handler, validate what it returned."""
    if isinstance(call, ProtocolError):
        return _Answer(400, code="invalid_argument", message=str(call))
    try:
        contract.input.validate(call.payload, "input")
    except SchemaError as err:
        return _Answer(400, code="invalid_argument", message=str(err))

    outcome = _run(handle, _context(contract, call), call)
    if outcome.failure is not None or outcome.status != 200:
        return outcome
    try:
        contract.output.validate(outcome.output, "output")
        encoded = encode_output(outcome.output, contract.max_output_bytes)
    except (SchemaError, ProtocolError) as err:
        # An answer the contract refuses is not this call's valid response, and
        # the effect may well have committed before the answer was spoiled.
        return _Answer(500, code="internal", message=str(err))
    return _Answer(200, body=encoded)


def _lookup(contract: Contract, receipt_of, call: Call | ProtocolError) -> _Answer:
    """Answer a recovery: what did this effect commit?

    Absent answers 404 and is never an error — absent is "not yet known", and a
    caller reads it as inconclusive rather than as permission to invoke again.
    """
    if isinstance(call, ProtocolError):
        return _Answer(400, code="invalid_argument", message=str(call))
    try:
        contract.input.validate(call.payload, "input")
    except SchemaError as err:
        return _Answer(400, code="invalid_argument", message=str(err))

    outcome = _run(receipt_of, _context(contract, call), call)
    if outcome.failure is not None or outcome.status != 200:
        return outcome
    if outcome.output is None:
        return _Answer(404, code="not_found", message="no receipt for this effect")
    try:
        contract.output.validate(outcome.output, "output")
        encoded = encode_output(outcome.output, contract.max_output_bytes)
    except (SchemaError, ProtocolError) as err:
        return _Answer(500, code="internal", message="receipt: " + str(err))
    return _Answer(200, body=encoded)


@dataclass
class _Outcome:
    """What the handler produced, before the contract is applied to it."""

    status: int
    output: Any = None
    failure: HandlerFailure | None = None
    message: str = ""


def _run(handler: Callable[[Context, dict], Any], context: Context, call: Call) -> Any:
    """Run the handler under the caller's budget.

    It runs on its own thread because Python cannot raise into one: when the
    budget passes, this answers and the handler is still running. What it would
    have returned is no longer this call's outcome, which is why the deadline
    wins over anything the handler produces afterwards.
    """
    produced: dict[str, Any] = {}

    def invoke() -> None:
        try:
            produced["output"] = handler(context, call.payload)
        except HandlerFailure as failure:
            produced["failure"] = failure
        except BaseException as err:  # noqa: BLE001 — an untyped error asserts nothing
            produced["error"] = _describe(err)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    worker.join(call.budget)
    if worker.is_alive():
        return _Answer(504, code="deadline_exceeded", message="the deadline passed")
    if "failure" in produced:
        # The operation's own typed failure: the one answer that proves the
        # effect did not happen.
        return _Answer(422, failure=produced["failure"])
    if "error" in produced:
        # An untyped exception is not the operation asserting anything, so it
        # stays unproven rather than being reported as a failure a caller would
        # read as proof no effect committed.
        return _Answer(500, code="internal", message=produced["error"])
    answer = _Answer(200)
    answer.output = produced.get("output")
    return answer


def _context(contract: Contract, call: Call) -> Context:
    return Context(
        invocation=call.identity,
        runnable=call.runnable,
        deadline=call.deadline,
        recovery=contract.recovery,
    )


def _load(contract: Contract, root: Path, attribute: str) -> Callable[[Context, dict], Any]:
    source = str(root.parent)
    if source not in sys.path:
        sys.path.insert(0, source)
    module = importlib.import_module(contract.handler_module)
    handler = getattr(module, attribute, None)
    if not callable(handler):
        raise AttributeError(f"{contract.handler_module}.{attribute} is not callable")
    return handler


def _error(code: str, message: str) -> bytes:
    """An error answer in the Connect JSON shape.

    On its own it proves nothing about the effect, which is the point: a caller
    that cannot see the operation's own assertion treats the attempt as
    unresolved.
    """
    return json.dumps({"code": code, "message": message}, ensure_ascii=False).encode("utf-8")


def _describe(err: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(err), err)).strip()
