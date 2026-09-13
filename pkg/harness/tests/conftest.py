import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "codefly_runnable"

IDENTITY = {
    "invocation": "inv-7f3a",
    "intent": "intent-2b19",
    "effect": "effect-64c0",
}
RUNNABLE = {
    "name": "word-count",
    "module": "text",
    "workspace": "proof",
    "version": "0.1.0",
}


@dataclass
class Invocation:
    exit_code: int
    stdout: str
    stderr: str
    completion: dict | None


class Runnable:
    """A runnable materialized on disk exactly as the agent generates it."""

    def __init__(self, root: Path, handler: str, contract: dict) -> None:
        self.root = root
        generated = root / ".codefly"
        generated.mkdir(parents=True)
        shutil.copytree(PACKAGE, generated / "codefly_runnable")
        (root / "handler.py").write_text(handler, encoding="utf-8")
        (generated / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
        self.generated = generated

    def invoke(
        self,
        payload,
        *,
        timeout: float = 30.0,
        deadline_in: float = 20.0,
        interrupt_after: float | None = None,
        environment: dict | None = None,
        raw_request: bytes | None = None,
    ) -> Invocation:
        request = self.generated / "request.json"
        completion = self.generated / "completion.json"
        completion.unlink(missing_ok=True)
        if raw_request is None:
            deadline = datetime.now(timezone.utc) + timedelta(seconds=deadline_in)
            raw_request = json.dumps(
                {
                    "schema": "codefly.runnable.request/v1",
                    "protocol": "codefly.runnable/v1",
                    "invocation": IDENTITY,
                    "runnable": RUNNABLE,
                    "deadline": deadline.isoformat().replace("+00:00", "Z"),
                    "input": payload,
                }
            ).encode("utf-8")
        request.write_bytes(raw_request)

        env = dict(os.environ)
        env["CODEFLY_RUNNABLE_REQUEST"] = str(request)
        env["CODEFLY_RUNNABLE_COMPLETION"] = str(completion)
        env["PYTHONPATH"] = str(self.generated)
        if environment is not None:
            for key, value in environment.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value

        process = subprocess.Popen(
            [sys.executable, "-m", "codefly_runnable"],
            cwd=self.generated,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if interrupt_after is not None:
            try:
                process.wait(timeout=interrupt_after)
            except subprocess.TimeoutExpired:
                process.terminate()
        stdout, stderr = process.communicate(timeout=timeout)
        recorded = None
        if completion.exists():
            recorded = json.loads(completion.read_text(encoding="utf-8"))
        return Invocation(process.returncode, stdout, stderr, recorded)


@pytest.fixture
def runnable(tmp_path):
    def build(handler: str, *, input=None, output=None, **overrides) -> Runnable:
        contract = {
            "schema": "codefly.runnable-generated-contract/v1",
            "protocol": "codefly.runnable/v1",
            "handler": {"module": "handler", "attribute": "handle"},
            "input": input or {},
            "output": output or {},
            "recovery": "recompute",
            "max-input-bytes": 65536,
            "max-output-bytes": 65536,
            "max-log-bytes": 262144,
        }
        contract.update(overrides)
        return Runnable(tmp_path / "word-count", handler, contract)

    return build
