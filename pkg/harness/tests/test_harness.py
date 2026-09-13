"""The harness under real processes: one request in, one bound completion out."""

import json

from conftest import IDENTITY, RUNNABLE

COUNT_CONTRACT = {
    "input": {
        "fields": [
            {"name": "text", "type": "string"},
            {"name": "double", "type": "boolean"},
        ]
    },
    "output": {"fields": [{"name": "count", "type": "integer"}]},
}

COUNT_HANDLER = """
def handle(context, input):
    count = len(input["text"].split())
    if input["double"]:
        count *= 2
    return {"count": count}
"""


def test_typed_output_depends_on_every_input(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT)

    single = unit.invoke({"text": "one two three", "double": False})
    doubled = unit.invoke({"text": "one two three", "double": True})
    shorter = unit.invoke({"text": "one two", "double": False})

    assert single.exit_code == 0
    assert single.completion["outcome"] == "completed"
    assert single.completion["output"] == {"count": 3}
    assert doubled.completion["output"] == {"count": 6}
    assert shorter.completion["output"] == {"count": 2}


def test_completion_is_bound_to_the_requested_identity(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT)

    result = unit.invoke({"text": "a b", "double": False})

    assert result.completion["invocation"] == IDENTITY
    assert result.completion["runnable"] == RUNNABLE
    assert result.completion["protocol"] == "codefly.runnable/v1"
    assert result.completion["recovery"] == "recompute"


def test_invalid_input_is_refused_before_the_handler_runs(runnable):
    unit = runnable(
        """
def handle(context, input):
    raise AssertionError("the handler must not be reached")
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": 1})

    assert result.exit_code == 64
    assert result.completion["outcome"] == "invalid_input"
    assert "must be a boolean" in result.completion["error"]["message"]
    assert result.completion["invocation"] == IDENTITY
    assert "output" not in result.completion


def test_invalid_output_is_reported_as_its_own_outcome(runnable):
    unit = runnable(
        """
def handle(context, input):
    return {"count": "three"}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b c", "double": False})

    assert result.exit_code == 65
    assert result.completion["outcome"] == "invalid_output"
    assert "output.count must be an integer" in result.completion["error"]["message"]


def test_a_handler_returning_a_non_object_is_invalid_output(runnable):
    unit = runnable(
        """
def handle(context, input):
    return 3
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b c", "double": False})

    assert result.exit_code == 65
    assert "expected an object" in result.completion["error"]["message"]


def test_a_raising_handler_fails_without_an_output(runnable):
    unit = runnable(
        """
def handle(context, input):
    raise RuntimeError("upstream refused")
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False})

    assert result.exit_code == 66
    assert result.completion["outcome"] == "failed"
    assert "upstream refused" in result.completion["error"]["message"]
    assert "output" not in result.completion


def test_an_exiting_handler_is_a_failure_not_a_success(runnable):
    unit = runnable(
        """
import sys

def handle(context, input):
    sys.exit(0)
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False})

    assert result.exit_code == 66
    assert result.completion["outcome"] == "failed"


def test_a_handler_past_the_deadline_times_out(runnable):
    unit = runnable(
        """
import time

def handle(context, input):
    time.sleep(30)
    return {"count": 0}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False}, deadline_in=1.0)

    assert result.exit_code == 67
    assert result.completion["outcome"] == "timeout"


def test_a_deadline_already_passed_never_starts_the_handler(runnable):
    unit = runnable(
        """
def handle(context, input):
    raise AssertionError("the handler must not be reached")
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False}, deadline_in=-1.0)

    assert result.exit_code == 67
    assert result.completion["outcome"] == "timeout"
    assert "before the handler started" in result.completion["error"]["message"]


def test_a_swallowed_deadline_is_still_a_timeout(runnable):
    unit = runnable(
        """
import time

def handle(context, input):
    try:
        time.sleep(30)
    except BaseException:
        pass
    return {"count": 99}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False}, deadline_in=1.0)

    assert result.exit_code == 67
    assert result.completion["outcome"] == "timeout"
    assert "output" not in result.completion


def test_an_interrupted_invocation_reports_interrupted(runnable):
    unit = runnable(
        """
import time

def handle(context, input):
    time.sleep(30)
    return {"count": 0}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False}, interrupt_after=1.0)

    assert result.exit_code == 68
    assert result.completion["outcome"] == "interrupted"


def test_a_swallowed_interruption_is_still_interrupted(runnable):
    unit = runnable(
        """
import time

def handle(context, input):
    try:
        time.sleep(30)
    except BaseException:
        pass
    return {"count": 99}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False}, interrupt_after=1.0)

    assert result.exit_code == 68
    assert result.completion["outcome"] == "interrupted"


def test_exit_zero_without_a_completion_leaves_nothing_to_read(runnable):
    unit = runnable(
        """
import os

def handle(context, input):
    os._exit(0)
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a b", "double": False})

    assert result.exit_code == 0
    assert result.completion is None


def test_logs_stay_out_of_the_completion(runnable):
    unit = runnable(
        """
import sys

def handle(context, input):
    print("handler progress")
    print("handler diagnostic", file=sys.stderr)
    context.log("context diagnostic")
    return {"count": 1}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a", "double": False})

    assert result.completion["output"] == {"count": 1}
    assert "handler progress" in result.stdout
    assert "handler diagnostic" in result.stderr
    assert "context diagnostic" in result.stderr
    assert "progress" not in json.dumps(result.completion)


def test_logs_are_truncated_at_the_declared_bound(runnable):
    unit = runnable(
        """
def handle(context, input):
    for _ in range(200):
        print("x" * 100)
    return {"count": 1}
""",
        **COUNT_CONTRACT,
        **{"max-log-bytes": 4096},
    )

    result = unit.invoke({"text": "a", "double": False})

    assert result.exit_code == 0
    assert result.completion["output"] == {"count": 1}
    assert "log truncated at 4096 bytes" in result.stdout
    assert len(result.stdout) < 8192


def test_an_output_over_the_bound_is_invalid_output(runnable):
    unit = runnable(
        """
def handle(context, input):
    return {"text": "x" * 4096}
""",
        input={},
        output={"fields": [{"name": "text", "type": "string"}]},
        **{"max-output-bytes": 1024},
    )

    result = unit.invoke({})

    assert result.exit_code == 65
    assert result.completion["outcome"] == "invalid_output"
    assert result.completion["error"]["kind"] == "payload-bound"


def test_a_request_over_the_bound_never_reaches_the_contract(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT, **{"max-input-bytes": 256})

    result = unit.invoke({"text": "x" * 4096, "double": False})

    assert result.exit_code == 69
    assert result.completion is None
    assert "over the declared 256 byte bound" in result.stderr


def test_a_malformed_request_is_a_protocol_error(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT)

    result = unit.invoke(None, raw_request=b"{not json")

    assert result.exit_code == 69
    assert result.completion is None
    assert "not UTF-8 JSON" in result.stderr


def test_a_request_without_a_deadline_is_refused(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT)

    result = unit.invoke(
        None,
        raw_request=json.dumps(
            {
                "schema": "codefly.runnable.request/v1",
                "protocol": "codefly.runnable/v1",
                "invocation": IDENTITY,
                "runnable": RUNNABLE,
                "input": {"text": "a", "double": False},
            }
        ).encode("utf-8"),
    )

    assert result.exit_code == 69
    assert "deadline is required" in result.stderr


def test_a_request_of_another_protocol_is_refused(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT)

    result = unit.invoke(
        None,
        raw_request=json.dumps(
            {
                "schema": "codefly.runnable.request/v1",
                "protocol": "codefly.runnable/v2",
                "invocation": IDENTITY,
                "runnable": RUNNABLE,
                "deadline": "2099-01-01T00:00:00Z",
                "input": {},
            }
        ).encode("utf-8"),
    )

    assert result.exit_code == 69
    assert "is not 'codefly.runnable/v1'" in result.stderr


def test_a_missing_completion_path_refuses_to_run(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT)

    result = unit.invoke(
        {"text": "a", "double": False},
        environment={"CODEFLY_RUNNABLE_COMPLETION": None},
    )

    assert result.exit_code == 69
    assert "CODEFLY_RUNNABLE_COMPLETION is required" in result.stderr


def test_a_handler_that_cannot_be_imported_fails(runnable):
    unit = runnable(
        """
import a_module_that_does_not_exist

def handle(context, input):
    return {"count": 1}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a", "double": False})

    assert result.exit_code == 66
    assert result.completion["error"]["kind"] == "handler-import"


def test_the_handler_receives_the_caller_identity_and_deadline(runnable):
    unit = runnable(
        """
def handle(context, input):
    assert context.invocation.invocation == "inv-7f3a"
    assert context.invocation.intent == "intent-2b19"
    assert context.invocation.effect == "effect-64c0"
    assert context.runnable.version == "0.1.0"
    assert 0 < context.remaining() <= 20
    assert context.recovery == "recompute"
    return {"count": 1}
""",
        **COUNT_CONTRACT,
    )

    result = unit.invoke({"text": "a", "double": False})

    assert result.exit_code == 0, result.stderr
    assert result.completion["outcome"] == "completed"


def test_payload_limit_excludes_framing(runnable):
    unit = runnable("def handle(context, input):\n    return {}\n",
                    **{"max-input-bytes": 2})
    result = unit.invoke({})
    assert result.exit_code == 0, result.stderr
    assert result.completion["output"] == {}


def test_expired_request_never_imports_author_code(runnable):
    unit = runnable('''
from pathlib import Path
Path(__file__).with_name("imported").write_text("author code ran")
def handle(context, input):
    return {}
''')
    result = unit.invoke({}, deadline_in=-1)
    assert result.exit_code == 67
    assert not (unit.root / "imported").exists()


def test_deadline_covers_author_imports(runnable):
    unit = runnable('''
import time
time.sleep(30)
def handle(context, input):
    return {}
''')
    result = unit.invoke({}, deadline_in=0.3, timeout=3)
    assert result.exit_code == 67
    assert result.completion["outcome"] == "timeout"


def test_cleanup_error_preserves_timeout(runnable):
    unit = runnable('''
import time
def handle(context, input):
    try:
        time.sleep(30)
    except Exception:
        raise ValueError("cleanup failed")
''')
    result = unit.invoke({}, deadline_in=0.3)
    assert result.exit_code == 67
    assert result.completion["outcome"] == "timeout"
    assert "cleanup failed" in result.completion["error"]["message"]


def test_invalid_return_preserves_interruption(runnable):
    unit = runnable('''
import time
def handle(context, input):
    try:
        time.sleep(30)
    except Exception:
        return None
''')
    result = unit.invoke({}, interrupt_after=0.3)
    assert result.exit_code == 68
    assert result.completion["outcome"] == "interrupted"


def test_file_descriptor_and_subprocess_logs_are_bounded(runnable):
    unit = runnable('''
import os
import subprocess
import sys
def handle(context, input):
    os.write(1, b"x" * 10000)
    subprocess.run([sys.executable, "-c", "import os; os.write(2, b'y' * 10000)"], check=True)
    return {}
''', **{"max-log-bytes": 1024})
    result = unit.invoke({})
    assert result.exit_code == 0, result.stderr
    for stream in (result.stdout, result.stderr):
        assert "log truncated at 1024 bytes" in stream
        assert len(stream.encode()) < 1100
