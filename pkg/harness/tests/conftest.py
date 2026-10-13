import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "codefly_runnable"

RUNNABLE = {
    "name": "word-count",
    "module": "text",
    "workspace": "proof",
    "version": "0.1.0",
}
WORK_CONTEXT = "eyJ0eXAiOiJjb2RlZmx5LndvcmstY29udGV4dC92MSJ9.signed"

INVOKE = "/codefly.runnable.v0.Runnable/Invoke"
LOOKUP = "/codefly.runnable.v0.Runnable/Lookup"


@dataclass
class Exchange:
    """Everything one raw connection received, and whether the harness closed it."""

    received: bytes
    closed: bool

    @property
    def responses(self) -> int:
        return self.received.count(b"HTTP/1.1 ")

    @property
    def status(self) -> int:
        return int(self.received.split(b" ", 2)[1])

    def header(self, name: str) -> str:
        head = self.received.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        for line in head.split("\r\n")[1:]:
            key, _, value = line.partition(":")
            if key.strip().lower() == name.lower():
                return value.strip()
        return ""

    def has_header(self, name: str) -> bool:
        head = self.received.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        return any(line.partition(":")[0].strip().lower() == name.lower() for line in head.split("\r\n")[1:])


def request(body: bytes = b"", headers: list | None = None, *, procedure: str = INVOKE, length: bool = True) -> bytes:
    """One request as bytes on the wire, so a test can send what no client library would."""
    lines = [f"POST {procedure} HTTP/1.1", "Host: harness"]
    if length:
        lines.append(f"Content-Length: {len(body)}")
    for key, value in headers if headers is not None else [("X-Codefly-Work-Context", WORK_CONTEXT)]:
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


@dataclass
class Answer:
    """What a caller saw of one call."""

    status: int
    body: str
    failure_code: str

    def document(self) -> dict:
        return json.loads(self.body)

    @property
    def proven_no_effect(self) -> bool:
        """Only the operation's own typed code proves nothing committed.

        A status says nothing on its own, which is the property the whole
        taxonomy rests on: a caller that read a 4xx as proof would stop looking
        for a receipt that exists.
        """
        return bool(self.failure_code)


class Runnable:
    """A runnable materialized on disk exactly as the agent generates it, and served."""

    def __init__(self, root: Path, handler: str, contract: dict) -> None:
        self.root = root
        self.contract = contract
        generated = root / ".codefly"
        generated.mkdir(parents=True)
        shutil.copytree(PACKAGE, generated / "codefly_runnable")
        (root / "handler.py").write_text(handler, encoding="utf-8")
        (generated / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
        self.generated = generated
        self.process: subprocess.Popen | None = None
        self.host, self.port, self.address = "", 0, ""

    def serve(self, *, environment: dict | None = None, wait: bool = True) -> "Runnable":
        """Start the harness on a port the operating system chose.

        Nothing here picks a number: the placement allocates the port and hands
        it over, which is the one fact the harness cannot derive.
        """
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self.host, self.port = "127.0.0.1", reserved.getsockname()[1]
            self.address = f"{self.host}:{self.port}"

        env = dict(os.environ)
        env["CODEFLY__RUNNABLE_ADDRESS"] = self.address
        env["PYTHONPATH"] = str(self.generated)
        for key, value in (environment or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value

        self.process = subprocess.Popen(
            [sys.executable, "-m", "codefly_runnable"],
            cwd=self.generated,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if wait:
            self._await()
        return self

    def _await(self) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"the harness exited with {self.process.returncode}\n{self.diagnostics()}")
            try:
                with socket.create_connection((self.host, self.port), 0.1):
                    return
            except OSError:
                time.sleep(0.02)
        raise AssertionError(f"the harness never answered on {self.address}\n{self.diagnostics()}")

    def call(
        self,
        payload,
        *,
        procedure: str = INVOKE,
        headers: dict | None = None,
        raw: bytes | None = None,
        timeout: float = 30.0,
    ) -> Answer:
        """Send one call the way the runtime's invoker sends one."""
        body = raw if raw is not None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sent = {"Content-Type": "application/json", "X-Codefly-Work-Context": WORK_CONTEXT}
        if self.contract["recovery"] == "receipt":
            sent["Codefly-Runnable-Effect-Id"] = "effect-64c0"
        for key, value in (headers or {}).items():
            if value is None:
                sent.pop(key, None)
            else:
                sent[key] = value

        request = urllib.request.Request(f"http://{self.address}{procedure}", data=body, headers=sent, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Answer(response.status, response.read().decode("utf-8"),
                              response.headers.get("Codefly-Runnable-Failure-Code", ""))
        except urllib.error.HTTPError as refused:
            return Answer(refused.status, refused.read().decode("utf-8"),
                          refused.headers.get("Codefly-Runnable-Failure-Code", ""))

    def exchange(self, raw: bytes, *, timeout: float = 5.0, half_close: bool = False) -> Exchange:
        """Send raw bytes on one connection and read until the harness closes it or goes quiet.

        ``half_close`` ends the sending side after the bytes, as a peer whose
        body stops short of its stated length does.
        """
        received, closed = b"", False
        with socket.create_connection((self.host, self.port), timeout) as connection:
            connection.sendall(raw)
            if half_close:
                connection.shutdown(socket.SHUT_WR)
            try:
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        closed = True
                        break
                    received += chunk
            except ConnectionResetError:
                closed = True
            except TimeoutError:
                pass
        return Exchange(received, closed)

    def deadline_in(self, seconds: float) -> dict:
        instant = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return {"Codefly-Runnable-Deadline": instant.isoformat().replace("+00:00", "Z")}

    def exit_code(self, timeout: float = 45.0) -> int:
        """Wait for a harness that ends on its own, and return its exit code.

        A harness that refuses to serve exits rather than answering, so a test
        of that refusal waits for the exit instead of stopping the process and
        reading back the signal it sent itself.
        """
        assert self.process is not None
        return self.process.wait(timeout=timeout)

    def stop(self, signal_number: int | None = None) -> int:
        if self.process is None:
            return 0
        if self.process.poll() is None:
            self.process.send_signal(signal_number) if signal_number else self.process.terminate()
        try:
            self.process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        return self.process.returncode

    def diagnostics(self) -> str:
        if self.process is None:
            return ""
        try:
            return self.process.communicate(timeout=5)[1]
        except subprocess.TimeoutExpired:
            return ""


@pytest.fixture
def runnable(tmp_path):
    served: list[Runnable] = []

    def build(handler: str, *, input=None, output=None, **overrides) -> Runnable:
        contract = {
            "schema": "codefly.runnable-generated-contract/v1",
            "protocol": "codefly.runnable.served/v1",
            "runnable": RUNNABLE,
            "handler": {"module": "handler", "attribute": "handle"},
            "input": input or {},
            "output": output or {},
            "recovery": "recompute",
            "max-input-bytes": 65536,
            "max-output-bytes": 65536,
            "timeout-nanoseconds": 120_000_000_000,
            "concurrency": 0,
        }
        contract.update(overrides)
        instance = Runnable(tmp_path / f"word-count-{len(served)}", handler, contract)
        served.append(instance)
        return instance

    yield build

    for instance in served:
        instance.stop()
