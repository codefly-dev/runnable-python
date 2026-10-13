"""The harness at the wire: what it reads as one call, and everything it refuses.

These tests send bytes no client library would, because the refusals are about
exactly those bytes. Two properties run through all of them. A refused request
never reaches the handler. And a refusal closes the connection, so bytes the
refused call did not consume are never read as the next call — under whatever
identity those bytes happen to name.
"""

import http.client
import json
import signal
import socket
import struct
import subprocess
import threading
import time

import urllib.error
import urllib.request

import pytest

from conftest import RUNNABLE, WORK_CONTEXT, request

from codefly_runnable import runner
from codefly_runnable.contract import Contract
from codefly_runnable.protocol import (
    IDLE_CONNECTIONS,
    IDLE_SECONDS,
    MAX_HEAD_BYTES,
    READ_MEMORY_BYTES,
    REQUEST_READ_SECONDS,
    HandlerFailure,
    InvocationIdentity,
    RunnableIdentity,
)
from codefly_runnable.schema import Schema

TEXT_CONTRACT = {
    "input": {"fields": [{"name": "text", "type": "string"}]},
    "output": {"fields": [{"name": "count", "type": "integer"}]},
}

# Every call the handler runs is recorded beside it, so a test can prove a call
# never ran rather than inferring it from an answer.
RECORDING_HANDLER = """
import atexit
import pathlib
import time

RAN = pathlib.Path(__file__).with_name("ran.log")
atexit.register(lambda: RAN.with_name("exited").write_text("atexit ran"))

def handle(context, input):
    with RAN.open("a") as ran:
        ran.write(input["text"] + "\\n")
    if input["text"] == "raise":
        raise RuntimeError("raised on purpose")
    if input["text"].startswith("slow"):
        time.sleep(float(input["text"][4:] or 2))
    return {"count": 1}
"""

SMUGGLED = request(
    b'{"text":"SMUGGLED"}',
    [("X-Codefly-Work-Context", "smuggled-identity")],
)


def ran(unit) -> list[str]:
    log = unit.root / "ran.log"
    return log.read_text().split() if log.exists() else []


def body(text: str) -> bytes:
    return json.dumps({"text": text}).encode()


def exited_cleanly(unit) -> bool:
    """Whether the handler module's atexit hook ran when the harness ended."""
    return (unit.root / "exited").exists()


def until(condition, timeout: float = 15.0, what: str = "the condition") -> None:
    """Wait for a condition the harness makes observable, never for a guessed interval."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen within {timeout:g}s")
        time.sleep(0.02)


def refused_to_connect(unit) -> bool:
    try:
        socket.create_connection((unit.host, unit.port), 0.5).close()
    except OSError:
        return True
    return False


def recording(runnable, **overrides):
    return runnable(RECORDING_HANDLER, **TEXT_CONTRACT, **overrides).serve()


def test_a_well_formed_connection_carries_one_call_after_another(runnable):
    """The control for every refusal below: kept alive, the next request is a call."""
    unit = recording(runnable)

    exchange = unit.exchange(request(body("first")) + request(body("second")), timeout=2)

    assert exchange.responses == 2
    assert ran(unit) == ["first", "second"]


@pytest.mark.parametrize(
    "case, headers, sent, status",
    [
        ("negative", [("Content-Length", "-1")], b"", 400),
        ("not a number", [("Content-Length", "abc")], b"", 400),
        ("signed", [("Content-Length", "+19")], body("x")[:19], 400),
        ("underscored", [("Content-Length", "1_9")], b"", 400),
        ("other digits", [("Content-Length", "１９")], b"", 400),
        ("repeated", [("Content-Length", "13"), ("Content-Length", "13")], body("x"), 400),
        ("also encoded", [("Content-Length", "13"), ("Transfer-Encoding", "chunked")], body("x"), 400),
        ("chunked", [("Transfer-Encoding", "chunked")], b"d\r\n" + body("x") + b"\r\n0\r\n\r\n", 400),
        ("absent", [], b"", 411),
        ("over the bound", [("Content-Length", str(65536 + 64 * 1024 + 1))], b"", 400),
        ("malformed line hiding an encoding",
         [("Content-Length", "13"), ("X-Bad ", "y"), ("Transfer-Encoding", "chunked")], body("x"), 400),
        ("malformed line hiding a second Work Context",
         [("Content-Length", "13"), ("X-Bad ", "y"), ("X-Codefly-Work-Context", "second")], body("x"), 400),
    ],
)
def test_a_body_is_read_only_by_one_decimal_length(runnable, case, headers, sent, status):
    """F1 and F2: a length int() would accept, or a body framed any other way, is refused unread.

    A negative length used to read to EOF, unbounded; and any body left unread
    used to be parsed as a second call on the same connection.
    """
    unit = recording(runnable)
    first = request(sent, [("X-Codefly-Work-Context", WORK_CONTEXT), *headers], length=False)

    exchange = unit.exchange(first + SMUGGLED)

    assert exchange.status == status, case
    assert exchange.responses == 1, f"{case}: the unread bytes were answered as a second call"
    assert exchange.closed, f"{case}: a refusal must close the connection"
    assert exchange.header("Connection") == "close"
    assert not exchange.has_header("Codefly-Runnable-Failure-Code")
    assert ran(unit) == [], f"{case}: a refused request reached the handler"


@pytest.mark.parametrize("case", ["unserved path", "GET with a body", "over the concurrency"])
def test_an_answer_given_before_the_body_is_read_closes_the_connection(runnable, case):
    """D7: the answers given before the body is read are exactly the smuggling shape."""
    unit = recording(runnable, concurrency=1)
    sent = body("x")
    if case == "unserved path":
        first = request(sent, procedure="/codefly.runnable.v0.Runnable/Nope")
        status = 404
    elif case == "GET with a body":
        first = request(sent).replace(b"POST ", b"GET ", 1)
        status = 405
    else:
        holder = threading.Thread(target=lambda: unit.call({"text": "slow3"}), daemon=True)
        holder.start()
        until(lambda: ran(unit) == ["slow3"], what="the holding call starting")
        first = request(sent)
        status = 503

    exchange = unit.exchange(first + SMUGGLED)

    assert exchange.status == status, case
    assert exchange.responses == 1 and exchange.closed, case
    assert "SMUGGLED" not in ran(unit), case


def test_every_error_answer_closes_the_connection(runnable):
    """Not only a framing refusal: a refused payload leaves the connection unusable too."""
    unit = recording(runnable)

    exchange = unit.exchange(request(b'{"text": 3}') + SMUGGLED)

    assert exchange.status == 400
    assert exchange.responses == 1 and exchange.closed
    assert ran(unit) == []


def test_a_body_ending_short_of_its_length_never_runs(runnable):
    """The bytes that did arrive are a whole document, and still not the call that was stated."""
    unit = recording(runnable)
    whole = body("short")

    exchange = unit.exchange(
        request(whole, [("X-Codefly-Work-Context", WORK_CONTEXT), ("Content-Length", str(len(whole) + 7))],
                length=False),
        half_close=True,
    )

    assert exchange.status == 400
    assert b"ended before its Content-Length" in exchange.received
    assert ran(unit) == []


def trickle(unit, head: bytes, every: float, stop: threading.Event, count: int = -1) -> socket.socket:
    """A peer sending its request a byte at a time, each byte well inside any per-read timeout."""
    connection = socket.create_connection((unit.host, unit.port))
    connection.sendall(head)

    def drip() -> None:
        sent = 0
        while sent != count and not stop.wait(every):
            sent += 1
            try:
                connection.sendall(b" ")
            except OSError:
                return

    threading.Thread(target=drip, daemon=True).start()
    return connection


def answered_within(connection: socket.socket, seconds: float) -> bytes:
    connection.settimeout(seconds)
    received = b""
    try:
        while chunk := connection.recv(65536):
            received += chunk
    except ConnectionResetError:
        pass
    return received


def test_a_trickled_body_is_cut_at_the_request_deadline(runnable):
    """D2: the deadline is on the request, not on each read, so a byte a second cannot hold it.

    The peer falls silent a second before the deadline: a read that waited a
    whole per-read timeout from there would answer long after it.
    """
    unit = recording(runnable)
    stop = threading.Event()
    started = time.monotonic()
    connection = trickle(unit, request(b"", [("X-Codefly-Work-Context", WORK_CONTEXT), ("Content-Length", "200")],
                                       length=False), 1.0, stop, count=int(REQUEST_READ_SECONDS) - 1)

    received = answered_within(connection, REQUEST_READ_SECONDS + 5)
    stop.set()

    assert received.startswith(b"HTTP/1.1 408"), received[:60]
    assert time.monotonic() - started < REQUEST_READ_SECONDS + 3
    assert ran(unit) == []


def test_a_trickled_request_line_is_cut_at_the_request_deadline(runnable):
    unit = recording(runnable)
    stop = threading.Event()
    started = time.monotonic()
    connection = trickle(unit, b"POST /codefly", 1.0, stop)

    answered_within(connection, REQUEST_READ_SECONDS + 5)
    stop.set()

    assert time.monotonic() - started < REQUEST_READ_SECONDS + 3, "the connection outlived the request deadline"


def test_a_request_still_arriving_holds_no_call_slot(runnable):
    """D2: two trickling peers used to take every slot of a concurrency of 2 and refuse every caller."""
    unit = recording(runnable, concurrency=1)
    stop = threading.Event()
    head = request(b"", [("X-Codefly-Work-Context", WORK_CONTEXT), ("Content-Length", "200")], length=False) + b"{"
    trickling = [trickle(unit, head, 3.0, stop) for _ in range(2)]
    # Settling only makes the mutation it guards against more likely to show:
    # the call below succeeds however long the trickling requests take to arrive.
    time.sleep(0.5)

    started = time.monotonic()
    answer = unit.call({"text": "legitimate"})
    stop.set()
    for connection in trickling:
        connection.close()

    assert answer.status == 200
    assert time.monotonic() - started < REQUEST_READ_SECONDS / 2
    assert ran(unit) == ["legitimate"]


def test_calls_over_the_declared_concurrency_are_refused(runnable):
    """F6: the declared bound is enforced here, before the handler runs."""
    unit = recording(runnable, concurrency=1)

    first = []
    caller = threading.Thread(target=lambda: first.append(unit.call({"text": "slow1.5"})))
    caller.start()
    until(lambda: ran(unit) == ["slow1.5"], what="the first call starting")
    over = unit.call({"text": "over"})
    caller.join(30)

    assert over.status == 503
    assert not over.proven_no_effect, "a refusal is never the operation's own assertion"
    assert first[0].status == 200
    assert ran(unit) == ["slow1.5"]


def test_a_handler_past_its_deadline_keeps_its_slot_until_it_returns(runnable):
    """D3: the bound is on author code running, not on calls answered."""
    unit = recording(runnable, concurrency=1)

    late = unit.call({"text": "slow4"}, headers=unit.deadline_in(0.5))
    over = unit.call({"text": "over"})

    assert late.status == 504
    assert over.status == 503, "a second handler ran beside one still running"
    assert not over.proven_no_effect
    until(lambda: unit.call({"text": "after"}).status == 200, what="the slot freeing once the handler returned")
    assert ran(unit) == ["slow4", "after"]


BIG_INPUT = {"max-input-bytes": 64 * 1024 * 1024}


def test_requests_being_read_are_bounded_by_memory(runnable):
    """D2 and D5: a request over the readers' memory is refused before any of it is read."""
    unit = recording(runnable, concurrency=1, **BIG_INPUT)
    readers = READ_MEMORY_BYTES // (MAX_HEAD_BYTES + BIG_INPUT["max-input-bytes"])
    assert readers == 3
    held = [socket.create_connection((unit.host, unit.port)) for _ in range(readers)]
    for connection in held:
        connection.sendall(b"POST /codefly")

    def refused() -> bool:
        return unit.exchange(b"GET / HTTP/1.1\r\nHost: harness\r\n\r\n", timeout=2).received.startswith(
            b"HTTP/1.1 503")

    until(refused, what="a request over the readers' bound being refused")
    for connection in held:
        connection.close()
    until(lambda: unit.call({"text": "after"}).status == 200, what="the readers freeing")
    assert ran(unit) == ["after"]


def test_a_running_call_holds_no_reader(runnable):
    """Once its body has arrived a call is no longer being read, so readers do not bound running calls."""
    unit = recording(runnable, concurrency=4, **BIG_INPUT)
    running = [threading.Thread(target=lambda: unit.call({"text": "slow3"}), daemon=True) for _ in range(3)]
    for caller in running:
        caller.start()
    until(lambda: ran(unit) == ["slow3"] * 3, what="three calls running")

    assert unit.call({"text": "fourth"}).status == 200


def test_an_idle_connection_makes_room_for_a_new_one(runnable):
    """D2: at the connection bound the idle connection goes, not the caller who needs one."""
    unit = recording(runnable, concurrency=1, **BIG_INPUT)
    bound = 3 + 1 + IDLE_CONNECTIONS
    held = [socket.create_connection((unit.host, unit.port)) for _ in range(bound)]

    answer = unit.call({"text": "newcomer"})

    assert answer.status == 200
    assert answered_within(held[0], 5) == b"", "the oldest idle connection was not the one closed"
    for connection in held:
        connection.close()


def test_a_negative_concurrency_refuses_to_start(runnable):
    unit = runnable(RECORDING_HANDLER, **TEXT_CONTRACT, concurrency=-1).serve(wait=False)

    assert unit.exit_code() == 1
    assert "negative concurrency" in unit.diagnostics()


@pytest.mark.parametrize(
    "code",
    ["x\r\nInjected: yes", "décliné", " ", "", "-leading", "a" * 65, "card declined"],
)
def test_a_failure_code_outside_the_grammar_is_refused(code):
    """F4: the code becomes a header, so it cannot carry a line break or a character a header cannot encode."""
    with pytest.raises(ValueError, match="failure code must match"):
        HandlerFailure(code, "message")


def test_a_failure_code_cannot_change_after_it_was_checked():
    failure = HandlerFailure("card_declined", "message")

    with pytest.raises(AttributeError):
        failure.code = "x\r\nInjected: yes"
    assert failure.code == "card_declined"


@pytest.mark.parametrize("code", ["card_declined", "a" * 64, "A.b-9", "0"])
def test_a_failure_code_in_the_grammar_is_kept(code):
    assert HandlerFailure(code, "message").code == code


def test_a_handler_raising_an_ill_formed_failure_asserts_nothing(runnable):
    """F4: refusing the code turns it into an untyped error, which leaves the effect unproven."""
    unit = runnable(
        """
from codefly_runnable import HandlerFailure

def handle(context, input):
    raise HandlerFailure("x\\r\\nInjected: yes", "smuggled header")
""",
        **TEXT_CONTRACT,
    ).serve()

    exchange = unit.exchange(request(body("a")))

    assert exchange.status == 500
    assert not exchange.has_header("Codefly-Runnable-Failure-Code")
    assert not exchange.has_header("Injected")


@pytest.mark.parametrize(
    "case, headers, message",
    [
        ("repeated Work Context",
         [("X-Codefly-Work-Context", "first"), ("X-Codefly-Work-Context", "second")], "is repeated"),
        ("folded Work Context", [("X-Codefly-Work-Context", "first\r\n second")], "control character"),
        ("Work Context over core's bound", [("X-Codefly-Work-Context", "w" * 8193)], "8192 character bound"),
        ("repeated deadline",
         [("X-Codefly-Work-Context", WORK_CONTEXT), ("Codefly-Runnable-Deadline", "2999-01-01T00:00:00Z"),
          ("Codefly-Runnable-Deadline", "2999-01-01T00:00:00Z")], "is repeated"),
        ("unrepresentable deadline",
         [("X-Codefly-Work-Context", WORK_CONTEXT), ("Codefly-Runnable-Deadline", "0001-01-01T00:00:00+01:00")],
         "no representable instant"),
        ("effect id over core's bound",
         [("X-Codefly-Work-Context", WORK_CONTEXT), ("Codefly-Runnable-Effect-Id", "e" * 129)], "128 character bound"),
        ("repeated effect id",
         [("X-Codefly-Work-Context", WORK_CONTEXT), ("Codefly-Runnable-Effect-Id", "one"),
          ("Codefly-Runnable-Effect-Id", "two")], "is repeated"),
    ],
)
def test_per_call_headers_are_held_to_cores_bounds(runnable, case, headers, message):
    """F8 and F7: one value, no control characters, within core's bounds — or the call never runs."""
    unit = recording(runnable)

    exchange = unit.exchange(request(body("a"), headers))

    assert exchange.status == 400, case
    assert message in exchange.received.decode("utf-8", "replace"), case
    assert exchange.closed
    assert ran(unit) == [], case


def test_the_bounds_on_per_call_headers_are_core_bounds_not_less(runnable):
    unit = recording(runnable)

    exchange = unit.exchange(
        request(body("at the bound"), [("X-Codefly-Work-Context", "w" * 8192), ("Codefly-Runnable-Effect-Id", "e" * 128)]),
        timeout=2,
    )

    assert exchange.status == 200


@pytest.mark.parametrize("case", ["headers", "request line"])
def test_the_request_head_is_bounded(runnable, case):
    """D5: the stdlib alone admits 100 header lines of 64 KiB, about 6 MiB per connection."""
    unit = recording(runnable)
    if case == "headers":
        first = request(body("x"), [("X-Codefly-Work-Context", WORK_CONTEXT)]
                        + [(f"X-Pad-{index}", "a" * 8000) for index in range(5)])
    else:
        first = request(body("x"), procedure="/" + "a" * (MAX_HEAD_BYTES + 1))

    exchange = unit.exchange(first + SMUGGLED)

    assert exchange.status == 431, case
    assert exchange.responses == 1 and exchange.closed
    assert ran(unit) == []


def test_expect_continue_is_answered_with_the_refusal(runnable):
    """A peer asking leave to send its body is not invited to upload one that will not be read."""
    unit = recording(runnable)

    refused = unit.exchange(request(b"", [("X-Codefly-Work-Context", WORK_CONTEXT), ("Expect", "100-continue"),
                                          ("Content-Length", str(10 * 1024 * 1024))], length=False))

    assert refused.status == 400
    assert b"100 Continue" not in refused.received
    assert refused.closed

    with socket.create_connection((unit.host, unit.port), 5) as connection:
        sent = body("continued")
        connection.sendall(request(b"", [("X-Codefly-Work-Context", WORK_CONTEXT), ("Expect", "100-continue"),
                                         ("Content-Length", str(len(sent)))], length=False))
        assert connection.recv(1024).startswith(b"HTTP/1.1 100")
        connection.sendall(sent)
        assert connection.recv(1024).startswith(b"HTTP/1.1 200")
    assert ran(unit) == ["continued"]


def test_a_kept_connection_advertises_how_long_it_is_kept(runnable):
    """D6: a client that reads Keep-Alive retires the connection before the harness closes it."""
    unit = recording(runnable)

    kept = unit.exchange(request(body("a")), timeout=1)
    refused = unit.exchange(request(b'{"text": 3}'))

    assert kept.header("Keep-Alive") == f"timeout={IDLE_SECONDS:g}"
    assert not kept.closed
    assert refused.header("Connection") == "close" and not refused.has_header("Keep-Alive")


def test_a_document_nested_past_the_decoder_is_refused(runnable):
    """F7: a refusal with an answer, never a dropped connection."""
    unit = recording(runnable)

    exchange = unit.exchange(request(b"[" * 30000))

    assert exchange.status == 400
    assert "nests deeper" in exchange.received.decode()
    assert ran(unit) == []


@pytest.mark.parametrize(
    "address",
    ["18517", ":18518", "127.0.0.1:0", "127.0.0.1:１８５", "127.0.0.1:99999", "[::1]", "127.0.0.1:"],
)
def test_the_listen_address_names_a_host_and_a_port(runnable, address):
    """F9: no host would bind every interface, and port 0 is a port nobody was told about."""
    unit = runnable(RECORDING_HANDLER, **TEXT_CONTRACT).serve(
        wait=False, environment={"CODEFLY__RUNNABLE_ADDRESS": address}
    )

    assert unit.exit_code() == 1
    assert "is not host:port" in unit.diagnostics()


def test_a_stopping_harness_refuses_calls_on_connections_already_open(runnable):
    """F5: a signal stops new calls everywhere, not only on new connections."""
    unit = recording(runnable)
    kept = http.client.HTTPConnection(unit.host, unit.port, timeout=10)
    headers = {"X-Codefly-Work-Context": WORK_CONTEXT, "Content-Type": "application/json"}
    kept.request("POST", "/codefly.runnable.v0.Runnable/Invoke", body=body("before"), headers=headers)
    before = kept.getresponse()
    before.read()
    assert before.status == 200

    in_flight = []
    caller = threading.Thread(target=lambda: in_flight.append(unit.call({"text": "slow2"})))
    caller.start()
    until(lambda: "slow2" in ran(unit), what="the call in flight starting")
    unit.process.send_signal(signal.SIGTERM)
    until(lambda: refused_to_connect(unit), what="the harness stopping")

    kept.request("POST", "/codefly.runnable.v0.Runnable/Invoke", body=body("after"), headers=headers)
    refused = kept.getresponse()
    refused.read()
    caller.join(30)

    assert refused.status == 503
    assert refused.getheader("Connection") == "close"
    assert in_flight[0].status == 200, "the call in flight was still given its grace"
    assert ran(unit) == ["before", "slow2"]
    assert unit.exit_code() == 0


def test_a_stopping_harness_ends_once_no_call_is_running(runnable):
    """F5: an idle kept-alive connection holds nothing, so the grace is not spent waiting on it."""
    unit = recording(runnable)
    kept = http.client.HTTPConnection(unit.host, unit.port, timeout=10)
    kept.request("POST", "/codefly.runnable.v0.Runnable/Invoke", body=body("a"),
                 headers={"X-Codefly-Work-Context": WORK_CONTEXT})
    kept.getresponse().read()

    started = time.monotonic()
    unit.process.send_signal(signal.SIGTERM)

    assert unit.exit_code(timeout=10) == 0
    assert time.monotonic() - started < 5
    assert exited_cleanly(unit), "the handler module's atexit hook did not run"
    kept.close()


def test_a_call_cut_off_by_the_grace_is_reported_by_the_exit_code(runnable):
    """F5: ending with a call still running leaves its effect unproven, and the exit says so."""
    unit = recording(runnable)
    outcome = []

    def call() -> None:
        try:
            outcome.append(unit.call({"text": "slow60"}, timeout=60))
        except OSError as dropped:
            outcome.append(dropped)

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    until(lambda: ran(unit) == ["slow60"], what="the call starting")

    unit.process.send_signal(signal.SIGTERM)

    assert unit.exit_code(timeout=45) == 1
    assert "unproven" in unit.diagnostics()
    assert exited_cleanly(unit), "the atexit hook did not run beside a handler cut off by the grace"
    caller.join(10)
    assert isinstance(outcome[0], OSError), "a call cut off at the end of the grace is never answered"


def test_the_work_context_stays_out_of_the_repr():
    """F11: a handler logging its context does not log the capability it carries."""
    identity = InvocationIdentity(effect="effect-64c0", work_context="secret-capability")

    assert "secret-capability" not in repr(identity)
    assert identity.work_context == "secret-capability"


class _Defective:
    """An input schema whose validation fails in a way the framing never anticipated."""

    def validate(self, payload, path):
        raise RuntimeError("a defect in the framing")


def test_a_defect_in_the_framing_still_answers_and_asserts_nothing():
    """F7: an exception the framing did not anticipate answers 500, never a dropped connection."""
    contract = Contract(
        handler_module="handler", handler_attribute="handle", input=_Defective(), output=Schema.parse({}),
        recovery="recompute", max_input_bytes=1024, max_output_bytes=1024, timeout_seconds=5.0,
        concurrency=1, release=RunnableIdentity(**RUNNABLE),
    )
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    server = runner._server(f"127.0.0.1:{port}", contract, runner._Calls(1), lambda context, input: {}, None)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    serving.start()
    try:
        call = urllib.request.Request(
            f"http://127.0.0.1:{port}/codefly.runnable.v0.Runnable/Invoke",
            data=b"{}", headers={"X-Codefly-Work-Context": WORK_CONTEXT}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as answered:
            urllib.request.urlopen(call, timeout=5)
    finally:
        server.shutdown()
        server.server_close()

    assert answered.value.status == 500
    assert "a defect in the framing" in answered.value.read().decode()
    assert answered.value.headers.get("Codefly-Runnable-Failure-Code") is None


def rss_mib(pid: int) -> float:
    """The harness's resident memory, as the operating system reports it."""
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 1024


class _Peak:
    """The harness's peak resident memory over a stretch of a test, sampled every 10 ms."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.baseline = rss_mib(pid)
        self.peak = self.baseline
        self._done = threading.Event()
        self._sampler = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._done.wait(0.01):
            self.peak = max(self.peak, rss_mib(self.pid))

    def __enter__(self) -> "_Peak":
        self._sampler.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._done.set()
        self._sampler.join(5)

    @property
    def growth(self) -> float:
        return self.peak - self.baseline


def _send(unit, raw: bytes, *, withhold_last: bool = False) -> socket.socket:
    connection = socket.create_connection((unit.host, unit.port))
    try:
        connection.sendall(raw[:-1] if withhold_last else raw)
    except OSError:
        # Refused unread once the readers are taken: the harness closed it.
        pass
    return connection


def test_decoding_stays_within_the_read_budget(runnable):
    """M1: a decoded document is many times its bytes, so decoding is bounded with the reading.

    Each body is 1 MiB of empty objects, about 24 MiB decoded. A request keeps
    its reader until it has been decoded, calls are decoded one at a time,
    and a document the schema refuses is dropped before the next. Released
    early or decoded side by side, these took the process to many GiB.
    """
    # Every element is checked before the last refuses the document, so each
    # decoded document lives as long as it takes to check it: decoded side by
    # side, they would all be alive together.
    unit = runnable(
        RECORDING_HANDLER,
        input={"fields": [{"name": "items", "type": "array", "items": {"type": "object", "fields": []}}]},
        output=TEXT_CONTRACT["output"],
        concurrency=200,
        **{"max-input-bytes": 32 * 1024 * 1024},
    ).serve()
    readers = READ_MEMORY_BYTES // (MAX_HEAD_BYTES + 32 * 1024 * 1024)
    assert readers == 7
    objects = b"[" + b",".join([b"{}"] * ((1024 * 1024 - 40) // 3)) + b",1]"
    whole = request(b'{"items":' + objects + b"}")

    # Every body arrives but its last byte, then every last byte at once:
    # decoding is what is left, and all of it would happen together.
    with _Peak(unit.process.pid) as together:
        connections = [_send(unit, whole, withhold_last=True) for _ in range(120)]
        for connection in connections:
            try:
                connection.sendall(whole[-1:])
            except OSError:
                pass
        answers = [answered_within(connection, 20)[:12] for connection in connections]
    # Whole bodies from eight senders at once: a reader freed before its body
    # is decoded would let them all be read and wait, in memory, for their turn.
    sent: list[socket.socket] = []

    def sender() -> None:
        for _ in range(30):
            sent.append(_send(unit, whole))

    with _Peak(unit.process.pid) as queued:
        senders = [threading.Thread(target=sender) for _ in range(8)]
        for thread in senders:
            thread.start()
        for thread in senders:
            thread.join(60)
        answers += [answered_within(connection, 20)[:12] for connection in sent]
    connections += sent
    for connection in connections:
        connection.close()

    assert answers.count(b"HTTP/1.1 400") >= 2, "no call was decoded at all"
    assert set(answers) <= {b"HTTP/1.1 400", b"HTTP/1.1 503", b""}, set(answers)
    # Seven readers of 1 MiB, one decoded document of about 24 MiB, and the
    # allocator's slack: well under what 120 bodies read or decoded at once take.
    for stretch in (together, queued):
        assert stretch.growth < 100, f"peak {stretch.peak:.0f} MiB over a {stretch.baseline:.0f} MiB baseline"
    assert ran(unit) == []


def test_a_call_refused_for_want_of_a_slot_is_never_decoded(runnable):
    """M1: admission comes before decoding, so a call over the concurrency costs no decode."""
    unit = recording(runnable, concurrency=1)
    holder = threading.Thread(target=lambda: unit.call({"text": "slow3"}), daemon=True)
    holder.start()
    until(lambda: ran(unit) == ["slow3"], what="the holding call starting")

    exchange = unit.exchange(request(b"{not json"))

    assert exchange.status == 503, "the body was decoded before admission refused it"


def test_a_handler_thread_that_cannot_start_frees_its_slot(runnable):
    """M2: a slot held for a thread that never ran would refuse every later call, and stall shutdown."""
    unit = runnable(
        """
import threading

_start = threading.Thread.start
_failures = [2]

def _failing(self):
    if getattr(self, "_target", None) is not None and self._target.__name__ == "invoke" and _failures[0]:
        _failures[0] -= 1
        raise RuntimeError("can't start new thread")
    return _start(self)

threading.Thread.start = _failing

def handle(context, input):
    return {"count": 1}
""",
        **TEXT_CONTRACT,
        concurrency=2,
    ).serve()

    failed = [unit.call({"text": "a"}), unit.call({"text": "b"})]
    after = unit.call({"text": "c"})
    started = time.monotonic()
    unit.process.send_signal(signal.SIGTERM)

    assert [answer.status for answer in failed] == [500, 500]
    assert not any(answer.proven_no_effect for answer in failed)
    assert after.status == 200, "a slot stayed held for a handler that never started"
    assert unit.exit_code(timeout=45) == 0
    assert time.monotonic() - started < 5, "shutdown waited on a handler that never started"


def test_a_failure_code_changed_after_it_was_checked_asserts_nothing(runnable):
    """N1: the code is checked again where it is sent, so it never reaches a header unchecked."""
    unit = runnable(
        """
from codefly_runnable import HandlerFailure

def handle(context, input):
    failure = HandlerFailure("card_declined", "declined")
    failure._code = "x\\r\\nInjected: yes"
    raise failure
""",
        **TEXT_CONTRACT,
    ).serve()

    exchange = unit.exchange(request(body("a")))

    assert exchange.status == 500
    assert not exchange.has_header("Codefly-Runnable-Failure-Code")
    assert not exchange.has_header("Injected")


def test_a_signal_with_many_connections_open_ends_with_the_documented_code(runnable):
    """N2: no call in flight is exit 0, whatever the connection threads are doing.

    The stderr pipe here is never read until the end, so the diagnostics of a
    few long refusals fill it and leave a connection thread blocked holding
    the stream's lock — the state in which interpreter finalization aborts.
    """
    unit = recording(runnable)
    parked = []
    for _ in range(300):
        connection = socket.create_connection((unit.host, unit.port))
        connection.sendall(b"POST /codefly.runnable.v0.Runnable/Invoke HTTP/1.1\r\nX-Pad: " + b"a" * 1000)
        parked.append(connection)
    for _ in range(6):
        refused = unit.exchange(request(b"", procedure="/" + "a" * 30000), timeout=1)
        assert refused.status == 404

    unit.process.send_signal(signal.SIGTERM)

    assert unit.exit_code(timeout=45) == 0, unit.diagnostics()[-300:]
    assert exited_cleanly(unit), "the handler module's atexit hook did not run"
    for connection in parked:
        connection.close()


def test_a_connection_closed_to_make_room_cannot_begin_a_request():
    """N3: once a connection is closed to make room, a request that began on it is refused unread."""
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    contract = Contract(
        handler_module="handler", handler_attribute="handle", input=Schema.parse({}), output=Schema.parse({}),
        recovery="recompute", max_input_bytes=1024, max_output_bytes=1024, timeout_seconds=5.0,
        concurrency=1, release=RunnableIdentity(**RUNNABLE),
    )
    server = runner._server(f"127.0.0.1:{port}", contract, runner._Calls(1), lambda context, input: {}, None)
    first, second = socket.socketpair(), socket.socketpair()
    try:
        server._connections = 1
        server.register(first[0])
        server.register(second[0])

        assert first[1].recv(1) == b"", "the idle connection was not the one closed"
        assert not server.claim(first[0]), "a request began on a connection already closed to make room"
        assert server.claim(second[0])
    finally:
        server.server_close()
        for pair in (first, second):
            for end in pair:
                end.close()


def test_an_input_over_the_read_budget_is_refused(runnable):
    """N4: a declared bound larger than the whole read budget is held to the budget."""
    unit = recording(runnable, **{"max-input-bytes": 512 * 1024 * 1024})

    exchange = unit.exchange(
        request(b"", [("X-Codefly-Work-Context", WORK_CONTEXT), ("Content-Length", str(300 * 1024 * 1024))],
                length=False) + SMUGGLED)

    assert exchange.status == 413
    assert exchange.responses == 1 and exchange.closed
    assert ran(unit) == []


def test_a_graceful_stop_runs_the_handler_modules_exit_hooks_and_flushes_its_buffers(runnable):
    """N-c: a stop is an ordinary interpreter exit, so what the handler deferred to exit still happens."""
    unit = runnable(
        """
import atexit
import pathlib

HERE = pathlib.Path(__file__).parent
EFFECTS = (HERE / "effects.log").open("a")
atexit.register(lambda: (HERE / "exited").write_text("atexit ran"))

def handle(context, input):
    EFFECTS.write("effect " + input["text"] + "\\n")
    return {"count": 1}
""",
        **TEXT_CONTRACT,
    ).serve()

    assert unit.call({"text": "one"}).status == 200
    unit.process.send_signal(signal.SIGTERM)

    assert unit.exit_code(timeout=45) == 0
    assert exited_cleanly(unit), "the handler module's atexit hook did not run"
    assert (unit.root / "effects.log").read_text() == "effect one\n", "a buffered write was lost at exit"


@pytest.mark.parametrize("variant", ["changes after the first read", "raises when read"])
def test_a_failure_code_is_read_once_and_that_value_is_sent(runnable, variant):
    """N-b: the value checked is the value sent; a code that cannot be read asserts nothing."""
    unit = runnable(
        """
from codefly_runnable import HandlerFailure

class Changing(HandlerFailure):
    reads = 0

    @property
    def code(self):
        Changing.reads += 1
        return "card_declined" if Changing.reads == 1 else "x\\r\\nInjected: yes"

class Raising(HandlerFailure):
    @property
    def code(self):
        raise RuntimeError("no code")

def handle(context, input):
    raise (Changing if input["text"] == "changing" else Raising)("card_declined", "declined")
""",
        **TEXT_CONTRACT,
    ).serve()

    exchange = unit.exchange(request(body("changing" if variant == "changes after the first read" else "raising")))

    assert not exchange.has_header("Injected")
    if variant == "changes after the first read":
        assert exchange.status == 422
        assert exchange.header("Codefly-Runnable-Failure-Code") == "card_declined"
    else:
        assert exchange.status == 500
        assert not exchange.has_header("Codefly-Runnable-Failure-Code")


def _fill_stderr(unit) -> None:
    """Leave the harness's stderr pipe full: the test never reads it until the process has ended."""
    for _ in range(6):
        assert unit.exchange(request(b"", procedure="/" + "a" * 30000), timeout=1).status == 404


def test_an_unread_stderr_never_holds_a_call_slot(runnable):
    """A diagnostic written while a call holds its slot would, on an unread stderr, hold it forever."""
    unit = recording(runnable, concurrency=1)
    _fill_stderr(unit)

    failed = unit.call({"text": "raise"}, timeout=10)
    after = unit.call({"text": "after"}, timeout=10)

    assert failed.status == 500
    assert after.status == 200, "the failed call's diagnostic kept its slot"
    assert ran(unit) == ["raise", "after"]


def test_an_unread_stderr_cannot_hold_the_exit_past_the_grace(runnable):
    """The diagnostic naming handlers cut off by the grace is written without waiting on the stream."""
    unit = recording(runnable)
    caller = threading.Thread(target=lambda: _ignore(lambda: unit.call({"text": "slow60"}, timeout=60)), daemon=True)
    caller.start()
    until(lambda: ran(unit) == ["slow60"], what="the call starting")
    _fill_stderr(unit)

    unit.process.send_signal(signal.SIGTERM)

    assert unit.exit_code(timeout=45) == 1


def test_a_connection_failing_beside_an_unread_stderr_cannot_turn_the_exit_into_a_crash(runnable):
    """A caller resetting its connection before the answer makes the write raise.

    The stdlib reports that with a traceback on stderr; on an unread stderr
    the reporting thread would block holding the stream's lock, and
    interpreter shutdown would abort on it. The harness reports it as a
    diagnostic instead, which never touches the stream.
    """
    unit = recording(runnable)
    _fill_stderr(unit)
    resetting = socket.create_connection((unit.host, unit.port))
    resetting.sendall(request(body("slow1")))
    until(lambda: ran(unit) == ["slow1"], what="the call starting")
    resetting.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    resetting.close()
    # The handler returns, its answer meets the reset, and the call is over.
    until(lambda: unit.call({"text": "after"}, timeout=5).status == 200, what="the next call")

    unit.process.send_signal(signal.SIGTERM)

    assert unit.exit_code(timeout=45) == 0
    assert exited_cleanly(unit)


def _ignore(action) -> None:
    try:
        action()
    except OSError:
        pass
