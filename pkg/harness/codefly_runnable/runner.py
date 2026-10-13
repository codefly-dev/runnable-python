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
import re
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
    DEFAULT_CONCURRENCY,
    FAILURE_CODE_HEADER,
    INVOKE_PROCEDURE,
    IO_TIMEOUT_SECONDS,
    LOOKUP_PROCEDURE,
    SHUTDOWN_GRACE_SECONDS,
    Call,
    HandlerFailure,
    ProtocolError,
    encode_output,
    read_call,
)
from .schema import SchemaError

# A length is ASCII digits and nothing else: int() would also take a sign, an
# underscore or a digit from another script, and a negative length reads to EOF.
_LENGTH = re.compile(r"[0-9]+")
_ADDRESS = re.compile(r"(?:\[(?P<v6>[^\[\]]+)\]|(?P<host>[^\[\]:]+)):(?P<port>[0-9]{1,5})")


@dataclass
class _Answer:
    """One call's answer before it becomes a response."""

    status: int
    body: bytes = b""
    code: str = ""
    message: str = ""
    failure: HandlerFailure | None = None


class _Calls:
    """The calls running author code: admitted under the declared bound, and drained at a signal.

    Admission and the stop are one decision under one lock, so no call starts
    after the harness has begun draining and the drain cannot miss one.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._stopping = False
        self._changed = threading.Condition()

    def admit(self) -> str:
        """Count a call in, or say why it is refused."""
        with self._changed:
            if self._stopping:
                return "the harness is stopping and takes no new calls"
            if self._active >= self._limit:
                return f"{self._limit} calls are already running, the declared concurrency"
            self._active += 1
            return ""

    def release(self) -> None:
        with self._changed:
            self._active -= 1
            self._changed.notify_all()

    @property
    def stopping(self) -> bool:
        with self._changed:
            return self._stopping

    def stop(self) -> None:
        with self._changed:
            self._stopping = True

    def drain(self, timeout: float) -> int:
        """Wait for the admitted calls to end; return how many were still running."""
        with self._changed:
            self._changed.wait_for(lambda: self._active == 0, timeout)
            return self._active


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
        calls = _Calls(contract.concurrency or DEFAULT_CONCURRENCY)
        server = _server(address, contract, calls, handle, receipt_of)
    except (OSError, KeyError, ValueError, ProtocolError, AttributeError, ImportError) as err:
        print(f"[codefly] {err}", file=sys.stderr)
        return 1

    stopping = threading.Event()

    def stop(*_: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        # From here every new call is refused, including one arriving on a
        # connection that was already open.
        calls.stop()
        # shutdown() blocks until the serving loop ends, and a signal handler
        # runs on the thread that is inside it.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
    # Calls already in flight are given their grace: cutting them off would
    # turn every one of them into an unproven effect at once, which is what the
    # rest of this harness spends its code avoiding. The harness ends as soon
    # as the last one has answered; an idle connection holds nothing.
    remaining = calls.drain(SHUTDOWN_GRACE_SECONDS)
    if remaining:
        print(
            f"[codefly] {remaining} calls were still running when the {SHUTDOWN_GRACE_SECONDS:g}s grace ended; "
            "their effects are unproven",
            file=sys.stderr,
        )
        return 1
    return 0


class _Server(ThreadingHTTPServer):
    """One thread per connection, up to a bound, none of them holding the process open."""

    # The drain above is what waits for calls; a thread parked on an idle
    # connection is not one, and joining it would hold the exit for its timeout.
    daemon_threads = True
    block_on_close = False

    def __init__(self, host: str, port: int, handler, connections: int) -> None:
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self._connections = threading.BoundedSemaphore(connections)
        super().__init__((host, port), handler)

    def process_request(self, request, client_address) -> None:
        # A connection over the bound is closed unread: no thread is spent on
        # it, and a call that was never read never ran.
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


def _server(address: str, contract: Contract, calls: _Calls, handle, receipt_of) -> ThreadingHTTPServer:
    host, port = _listen(address)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Applied to the connection's socket: a peer that stops sending, or
        # never sends the next request, releases its thread.
        timeout = IO_TIMEOUT_SECONDS

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
            if self.path == INVOKE_PROCEDURE:
                serve = lambda call: _invoke(contract, handle, call)  # noqa: E731
            elif self.path == LOOKUP_PROCEDURE and receipt_of is not None:
                serve = lambda call: _lookup(contract, receipt_of, call)  # noqa: E731
            else:
                self._answer(_Answer(404, code="unimplemented", message=f"{self.path} is not served"))
                return
            refused = calls.admit()
            if refused:
                self._answer(_Answer(503, code="unavailable", message=refused))
                return
            try:
                try:
                    answer = serve(self._call())
                except Exception as err:  # noqa: BLE001 — a defect in the framing still answers
                    answer = _Answer(500, code="internal", message=_describe(err))
                self._answer(answer)
            finally:
                calls.release()

        def do_GET(self) -> None:  # noqa: N802
            self._answer(_Answer(405, code="unimplemented", message="the operation is called with POST"))

        def _call(self) -> Call | _Answer:
            """Read exactly one call off the connection, or refuse it.

            The body is read only by a length stated in ASCII digits. Anything
            else — a missing length, a chunked or otherwise encoded body, a sign —
            is refused unread, and the refusal closes the connection, so bytes
            this call did not consume are never read as the next call.
            """
            if self.headers.get_all("Transfer-Encoding"):
                return _Answer(400, code="invalid_argument",
                               message="a call states its Content-Length; Transfer-Encoding is refused")
            lengths = self.headers.get_all("Content-Length") or []
            if not lengths:
                return _Answer(411, code="invalid_argument", message="a call states its Content-Length")
            if len(lengths) > 1 or not _LENGTH.fullmatch(lengths[0]):
                return _Answer(400, code="invalid_argument",
                               message="Content-Length is not one length in decimal digits")
            declared = int(lengths[0])
            if declared > contract.max_input_bytes:
                return _Answer(400, code="invalid_argument", message=(
                    f"input is {declared} bytes, over the declared {contract.max_input_bytes} byte bound"))
            try:
                body = self.rfile.read(declared)
            except TimeoutError:
                return _Answer(408, code="deadline_exceeded",
                               message=f"the body stalled for {IO_TIMEOUT_SECONDS:g}s")
            if len(body) != declared:
                return _Answer(400, code="invalid_argument", message="the body ended before its Content-Length")
            try:
                return read_call(self.headers, body, contract)
            except ProtocolError as err:
                return _Answer(400, code="invalid_argument", message=str(err))

        def _answer(self, answer: _Answer) -> None:
            # Anything but a success may have left request bytes unread, and a
            # stopping harness keeps no connection for a call it will refuse.
            close = answer.status != 200 or calls.stopping
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
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)
            if answer.message:
                print(f"[codefly] {answer.message}", file=sys.stderr)

        def log_message(self, *_: object) -> None:
            """Diagnostics are what the harness prints deliberately, not a request log."""

    # Every admitted call holds a connection, and a caller keeps a few idle
    # ones beside them; beyond that a connection is a thread spent on nothing.
    return _Server(host, port, Handler, connections=2 * (contract.concurrency or DEFAULT_CONCURRENCY))


def _listen(address: str) -> tuple[str, int]:
    """Split the allocated host:port.

    Both halves are required. A missing host would bind every interface, and a
    port of 0 would let the operating system choose one nobody was told about.
    """
    match = _ADDRESS.fullmatch(address)
    port = int(match["port"]) if match else 0
    if not match or not 1 <= port <= 65535:
        raise ProtocolError(f"{ADDRESS_VARIABLE}={address!r} is not host:port with a port from 1 to 65535")
    return match["v6"] or match["host"], port


def _invoke(contract: Contract, handle, call: Call | _Answer) -> _Answer:
    """Answer one call: validate the payload, run the handler, validate what it returned."""
    if isinstance(call, _Answer):
        return call
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


def _lookup(contract: Contract, receipt_of, call: Call | _Answer) -> _Answer:
    """Answer a recovery: what did this effect commit?

    Absent answers 404 and is never an error — absent is "not yet known", and a
    caller reads it as inconclusive rather than as permission to invoke again.
    """
    if isinstance(call, _Answer):
        return call
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
