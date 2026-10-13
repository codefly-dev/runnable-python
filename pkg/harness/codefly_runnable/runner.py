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
import io
import json
import os
import re
import signal
import socket
import sys
import threading
import time
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
    HARNESS_CONCURRENCY,
    IDLE_CONNECTIONS,
    IDLE_SECONDS,
    INVOKE_PROCEDURE,
    LOOKUP_PROCEDURE,
    MAX_HEAD_BYTES,
    MAX_READERS,
    READ_MEMORY_BYTES,
    REQUEST_READ_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    WRITE_STALL_SECONDS,
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


class _Slot:
    """One admitted call's place under the declared concurrency.

    It is held by the request that admitted it and, once the handler starts, by
    the handler's thread too, and it is freed only when both have let go. A call
    answered at its deadline keeps its slot while its handler is still running:
    the bound is on author code running, not on calls being answered.
    """

    def __init__(self, calls: "_Calls") -> None:
        self._calls = calls
        self._holders = 1
        self._lock = threading.Lock()

    def hold(self) -> None:
        with self._lock:
            self._holders += 1

    def release(self) -> None:
        with self._lock:
            self._holders -= 1
            freed = self._holders == 0
        if freed:
            self._calls._free()


class _Calls:
    """The calls running author code: admitted under the declared bound, and drained at a signal.

    Admission and the stop are one decision under one lock, so no call starts
    after the harness has begun draining and the drain cannot miss one.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0
        self._stopping = False
        self._changed = threading.Condition()

    def admit(self) -> _Slot | str:
        """A slot for one call, or why it is refused."""
        with self._changed:
            if self._stopping:
                return "the harness is stopping and takes no new calls"
            if self._active >= self.limit:
                return f"{self.limit} calls are already running, the concurrency bound"
            self._active += 1
            return _Slot(self)

    def _free(self) -> None:
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
        """Wait for every handler to end; return how many were still running."""
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
        calls = _Calls(contract.concurrency or HARNESS_CONCURRENCY)
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
    # Handlers still running are given their grace: cutting them off would turn
    # every one of them into an unproven effect at once, which is what the rest
    # of this harness spends its code avoiding. That includes a handler whose
    # call was already answered at its deadline. The harness ends as soon as
    # the last one has returned; an idle connection holds nothing.
    remaining = calls.drain(SHUTDOWN_GRACE_SECONDS)
    if remaining:
        print(
            f"[codefly] {remaining} handlers were still running when the {SHUTDOWN_GRACE_SECONDS:g}s grace ended; "
            "their effects are unproven",
            file=sys.stderr,
        )
        return 1
    return 0


class _Input(socket.SocketIO):
    """A connection's input, read against one wall-clock deadline per request.

    Between requests a read waits for the idle timeout. Once a request has
    begun, every read waits only for what is left of that request's deadline,
    so a peer trickling a byte at a time cannot stretch the request past it.
    """

    def __init__(self, connection: socket.socket) -> None:
        super().__init__(connection, "rb")
        self.deadline: float | None = None

    def readinto(self, buffer) -> int | None:
        if self.deadline is None:
            self._sock.settimeout(IDLE_SECONDS)
        else:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the request was not read within {REQUEST_READ_SECONDS:g}s")
            self._sock.settimeout(remaining)
        return super().readinto(buffer)


class _HeadTooLarge(Exception):
    """The request line and headers ran past MAX_HEAD_BYTES."""


class _Head:
    """The request's input while its head is parsed: every line counts against one bound.

    The stdlib bounds each line at 64 KiB and the count at 100; this bounds
    the head as a whole, which is what a peer holds in memory.
    """

    def __init__(self, source: io.BufferedReader, limit: int) -> None:
        self._source = source
        self._left = limit

    def readline(self, size: int = -1) -> bytes:
        allowed = self._left + 1
        line = self._source.readline(allowed if size < 0 else min(size, allowed))
        self._left -= len(line)
        if self._left < 0:
            raise _HeadTooLarge(f"the request head is over {MAX_HEAD_BYTES} bytes")
        return line


class _Server(ThreadingHTTPServer):
    """One thread per connection, bounded by memory, none of them holding the process open.

    Requests being read are bounded by READ_MEMORY_BYTES; calls running by the
    concurrency. Connections are bounded beside them, and at that bound a new
    connection closes the one idle the longest rather than being refused: an
    idle connection holds nothing a caller is waiting on.
    """

    # The drain in main() is what waits for handlers; a thread parked on an
    # idle connection is not one, and joining it would hold the exit.
    daemon_threads = True
    block_on_close = False

    def __init__(self, host: str, port: int, handler, *, readers: int, connections: int) -> None:
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.readers = threading.BoundedSemaphore(readers)
        self._connections = connections
        # Every open connection, oldest idle first, mapped to whether it is idle.
        self._open: dict[socket.socket, bool] = {}
        self._lock = threading.Lock()
        super().__init__((host, port), handler)

    def process_request(self, request, client_address) -> None:
        with self._lock:
            if len(self._open) >= self._connections:
                oldest = next((connection for connection, idle in self._open.items() if idle), None)
                if oldest is not None:
                    del self._open[oldest]
                    try:
                        oldest.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            self._open[request] = True
        super().process_request(request, client_address)

    def idle(self, request, idle: bool) -> None:
        """Mark a connection idle (moved to the back of the eviction order) or busy."""
        with self._lock:
            if request in self._open:
                del self._open[request]
                self._open[request] = idle

    def shutdown_request(self, request) -> None:
        with self._lock:
            self._open.pop(request, None)
        super().shutdown_request(request)


def _server(address: str, contract: Contract, calls: _Calls, handle, receipt_of) -> ThreadingHTTPServer:
    host, port = _listen(address)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Timeouts are set per read by _Input and per answer by _answer.
        timeout = None

        def setup(self) -> None:
            super().setup()
            # The stdlib's reader is replaced, and closed so it does not keep
            # the socket open past the connection.
            self.rfile.close()
            self._input = _Input(self.connection)
            self.rfile = self._body = io.BufferedReader(self._input)
            self._reading = False

        def handle_one_request(self) -> None:
            """Read one request under the harness's bounds, then serve it."""
            self.server.idle(self.connection, True)
            self._input.deadline = None
            try:
                begun = self._body.peek(1)
            except OSError:
                begun = b""
            if not begun:
                # The peer left, stayed idle past the idle timeout, or the
                # connection was closed to make room for a new one.
                self.close_connection = True
                return
            self.server.idle(self.connection, False)
            self._prepare()
            if not self.server.readers.acquire(blocking=False):
                # Refused before a byte of it is read, so no peer can make the
                # harness hold more requests than its memory bound.
                self._answer(_Answer(503, code="unavailable", message="too many requests are being read"))
                return
            self._reading = True
            self._input.deadline = time.monotonic() + REQUEST_READ_SECONDS
            self.rfile = _Head(self._body, MAX_HEAD_BYTES)
            try:
                super().handle_one_request()
            except _HeadTooLarge as err:
                self._prepare()
                self._answer(_Answer(431, code="invalid_argument", message=str(err)))
            finally:
                self.rfile = self._body
                self._done_reading()

        def _prepare(self) -> None:
            """Make an answer possible before a request line was parsed."""
            self.request_version, self.requestline, self.command = self.protocol_version, "", ""

        def _done_reading(self) -> None:
            if self._reading:
                self._reading = False
                self.server.readers.release()

        def handle_expect_100(self) -> bool:
            # A peer asking leave to send its body is told the refusal instead,
            # rather than being invited to upload a body that will not be read.
            if self.command == "POST" and self._route() is not None:
                framing = self._framing()
                if isinstance(framing, _Answer):
                    self._answer(framing)
                    return False
                return super().handle_expect_100()
            return True

        def _route(self) -> Callable[[Call, _Slot], _Answer] | None:
            if self.path == INVOKE_PROCEDURE:
                return lambda call, slot: _invoke(contract, handle, call, slot)
            if self.path == LOOKUP_PROCEDURE and receipt_of is not None:
                return lambda call, slot: _lookup(contract, receipt_of, call, slot)
            return None

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
            serve = self._route()
            if serve is None:
                self._answer(_Answer(404, code="unimplemented", message=f"{self.path} is not served"))
                return
            call = self._call()
            if isinstance(call, _Answer):
                self._answer(call)
                return
            # A slot is taken only once the whole request has arrived, so a
            # request still arriving never holds one.
            slot = calls.admit()
            if isinstance(slot, str):
                self._answer(_Answer(503, code="unavailable", message=slot))
                return
            try:
                try:
                    answer = serve(call, slot)
                except Exception as err:  # noqa: BLE001 — a defect in the framing still answers
                    answer = _Answer(500, code="internal", message=_describe(err))
                self._answer(answer)
            finally:
                slot.release()

        def do_GET(self) -> None:  # noqa: N802
            self._answer(_Answer(405, code="unimplemented", message="the operation is called with POST"))

        def _framing(self) -> int | _Answer:
            """The body's length, or why it is refused unread.

            The body is read only by a length stated in ASCII digits. Anything
            else — a missing length, a chunked or otherwise encoded body, a sign,
            a header line the parser could not read — is refused unread, and the
            refusal closes the connection, so bytes this call did not consume are
            never read as the next call.
            """
            if self.headers.defects:
                # A malformed line ends the parsed headers early, so every
                # header after it — a Transfer-Encoding, a second Work Context —
                # would go unseen.
                return _Answer(400, code="invalid_argument", message="the request carries a malformed header line")
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
            return declared

        def _call(self) -> Call | _Answer:
            """Read exactly one call off the connection, or refuse it."""
            try:
                declared = self._framing()
                if isinstance(declared, _Answer):
                    return declared
                try:
                    body = self._body.read(declared)
                except TimeoutError:
                    return _Answer(408, code="deadline_exceeded",
                                   message=f"the request was not read within {REQUEST_READ_SECONDS:g}s")
            finally:
                self._done_reading()
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
            self.connection.settimeout(WRITE_STALL_SECONDS)
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
            else:
                self.send_header("Keep-Alive", f"timeout={IDLE_SECONDS:g}")
            self.end_headers()
            self.wfile.write(body)
            if answer.message:
                print(f"[codefly] {answer.message}", file=sys.stderr)

        def log_request(self, *_: object) -> None:
            """No request log: an answer's diagnostics are printed by _answer."""

        def log_message(self, *_: object) -> None:
            """Diagnostics are what the harness prints deliberately, not a request log."""

    readers = max(1, min(MAX_READERS, READ_MEMORY_BYTES // (MAX_HEAD_BYTES + contract.max_input_bytes)))
    return _Server(host, port, Handler, readers=readers, connections=readers + calls.limit + IDLE_CONNECTIONS)


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


def _invoke(contract: Contract, handle, call: Call, slot: _Slot) -> _Answer:
    """Answer one call: validate the payload, run the handler, validate what it returned."""
    try:
        contract.input.validate(call.payload, "input")
    except SchemaError as err:
        return _Answer(400, code="invalid_argument", message=str(err))

    outcome = _run(handle, _context(contract, call), call, slot)
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


def _lookup(contract: Contract, receipt_of, call: Call, slot: _Slot) -> _Answer:
    """Answer a recovery: what did this effect commit?

    Absent answers 404 and is never an error — absent is "not yet known", and a
    caller reads it as inconclusive rather than as permission to invoke again.
    """
    try:
        contract.input.validate(call.payload, "input")
    except SchemaError as err:
        return _Answer(400, code="invalid_argument", message=str(err))

    outcome = _run(receipt_of, _context(contract, call), call, slot)
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


def _run(handler: Callable[[Context, dict], Any], context: Context, call: Call, slot: _Slot) -> _Answer:
    """Run the handler under the caller's budget.

    It runs on its own thread because Python cannot raise into one: when the
    budget passes, this answers and the handler is still running. What it would
    have returned is no longer this call's outcome, which is why the deadline
    wins over anything the handler produces afterwards — but the handler holds
    its concurrency slot until it really returns.
    """
    produced: dict[str, Any] = {}

    def invoke() -> None:
        try:
            produced["output"] = handler(context, call.payload)
        except HandlerFailure as failure:
            produced["failure"] = failure
        except BaseException as err:  # noqa: BLE001 — an untyped error asserts nothing
            produced["error"] = _describe(err)
        finally:
            slot.release()

    worker = threading.Thread(target=invoke, daemon=True)
    slot.hold()
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
