"""The harness under real processes: it serves, and one call is one POST.

Every test here starts the harness the way a placement starts it — an allocated
address in the environment, nothing else — and calls it over HTTP the way the
runtime's invoker does. What a caller concludes from each answer is core's
judgement, not this suite's, so the properties asserted are the ones core reads:
the failure-code header, the status, and the answer document.
"""

import signal
import threading
import time

from conftest import LOOKUP, RUNNABLE, WORK_CONTEXT

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


def test_the_harness_serves_rather_than_answering_once(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT).serve()

    single = unit.call({"text": "one two three", "double": False})
    doubled = unit.call({"text": "one two three", "double": True})
    shorter = unit.call({"text": "one two", "double": False})

    assert single.status == 200
    assert single.document() == {"count": 3}
    assert doubled.document() == {"count": 6}
    assert shorter.document() == {"count": 2}


def test_the_answer_is_the_bounded_output_document_itself(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT).serve()

    answer = unit.call({"text": "a b", "double": False})

    # No Codefly envelope around it: a field Codefly added to an owner's body
    # would be a field the owner never described.
    assert answer.document() == {"count": 2}


def test_only_the_handlers_own_failure_proves_no_effect_committed(runnable):
    """The property the whole taxonomy rests on.

    The failure-code header, and nothing else, tells a caller that no effect
    committed. A harness that set it for a crash, an untyped error or a deadline
    would be asserting on the handler's behalf that its effect did not happen,
    and a caller would stop looking for a receipt that exists.
    """
    unit = runnable(
        """
from codefly_runnable import HandlerFailure

def handle(context, input):
    if input["text"] == "declared":
        raise HandlerFailure("card_declined", "the issuer declined")
    if input["text"] == "untyped":
        raise RuntimeError("something went wrong")
    if input["text"] == "exit":
        raise SystemExit(3)
    return {"count": 1}
""",
        **COUNT_CONTRACT,
    ).serve()

    declared = unit.call({"text": "declared", "double": False})
    assert declared.proven_no_effect
    assert declared.failure_code == "card_declined"
    assert declared.status == 422

    for text in ("untyped", "exit"):
        answer = unit.call({"text": text, "double": False})
        assert not answer.proven_no_effect, f"{text} must leave the effect unproven"
        assert answer.status == 500


def test_a_call_with_no_work_context_is_refused(runnable):
    """The one thing the required identity slot exists to prevent.

    A call carrying no capability is refused rather than run under whatever
    identity this process happens to have.
    """
    unit = runnable(
        """
def handle(context, input):
    raise AssertionError("the handler must not be reached")
""",
        **COUNT_CONTRACT,
    ).serve()

    answer = unit.call({"text": "a b", "double": False}, headers={"X-Codefly-Work-Context": None})

    assert answer.status == 400
    assert not answer.proven_no_effect
    assert "Work Context" in answer.document()["message"]


def test_the_handler_sees_the_callers_identity(runnable):
    """The only thing a handler may do with a Work Context is forward it.

    Parsing or reconstructing one is not the harness's business and it never
    does either: the capability is carried verbatim.
    """
    unit = runnable(
        """
def handle(context, input):
    return {"seen": context.invocation.work_context + "|" + context.invocation.effect
                    + "|" + context.runnable.name + "|" + str(context.remaining() > 0)}


def receipt_of(context, input):
    return None
""",
        input=COUNT_CONTRACT["input"],
        output={"fields": [{"name": "seen", "type": "string"}]},
        recovery="receipt",
    ).serve()

    seen = unit.call({"text": "a", "double": False}).document()["seen"]

    assert seen == f"{WORK_CONTEXT}|effect-64c0|{RUNNABLE['name']}|True"


def test_the_payload_is_checked_before_the_handler_runs(runnable):
    unit = runnable(
        """
def handle(context, input):
    raise AssertionError("the handler must not be reached")
""",
        **COUNT_CONTRACT,
    ).serve()

    for payload, raw in (
        ({"double": False}, None),                                  # a missing required field
        ({"text": "a", "double": False, "extra": 1}, None),         # an undeclared field
        ({"text": 1, "double": False}, None),                       # the wrong value type
        ({"text": None, "double": False}, None),                    # a null where none is declared
        (None, b'["text"]'),                                        # a body that is not an object
        (None, b"{not json"),                                       # a body that is not JSON
        (None, b'{"text":"a","text":"b","double":false}'),          # a repeated key
    ):
        answer = unit.call(payload, raw=raw)
        assert answer.status == 400, answer.body
        assert not answer.proven_no_effect, "a refused payload is not the operation's own failure"


def test_an_answer_the_contract_refuses_is_not_a_success(runnable):
    unit = runnable(
        """
def handle(context, input):
    if input["text"] == "wrong-type":
        return {"count": "three"}
    if input["text"] == "not-an-object":
        return [1, 2, 3]
    return {"count": 1, "undeclared": True}
""",
        **COUNT_CONTRACT,
    ).serve()

    for text in ("wrong-type", "not-an-object", "undeclared"):
        answer = unit.call({"text": text, "double": False})
        assert answer.status == 500, answer.body
        # The effect may well have committed before the answer was spoiled, so
        # this is unproven rather than the operation's own failure.
        assert not answer.proven_no_effect


def test_the_bounds_are_the_declared_ones(runnable):
    over_input = runnable(COUNT_HANDLER, **COUNT_CONTRACT, **{"max-input-bytes": 256}).serve()
    answer = over_input.call({"text": "x" * 4096, "double": False})
    assert answer.status == 400
    assert "over the declared 256 byte bound" in answer.document()["message"]

    over_output = runnable(
        """
def handle(context, input):
    return {"text": "x" * 4096}
""",
        input={},
        output={"fields": [{"name": "text", "type": "string"}]},
        **{"max-output-bytes": 1024},
    ).serve()
    answer = over_output.call({})
    assert answer.status == 500
    assert "over the declared 1024 byte bound" in answer.document()["message"]


def test_the_deadline_header_bounds_the_call(runnable):
    unit = runnable(
        """
import time

def handle(context, input):
    time.sleep(30)
    return {"count": 0}
""",
        **COUNT_CONTRACT,
    ).serve()

    answer = unit.call({"text": "a", "double": False}, headers=unit.deadline_in(2))
    assert answer.status == 504
    assert not answer.proven_no_effect, "a deadline leaves the effect unproven"

    # A deadline already in the past never starts the handler: spending the
    # attempt would make a timeout look like work that happened.
    past = unit.call({"text": "a", "double": False}, headers=unit.deadline_in(-60))
    assert past.status == 400
    assert "deadline had passed" in past.document()["message"]


def test_a_deadline_cannot_extend_the_declared_timeout(runnable):
    """A caller may shorten the author's bound and never overrule it."""
    unit = runnable(
        """
import time

def handle(context, input):
    time.sleep(30)
    return {"count": 0}
""",
        **COUNT_CONTRACT,
        **{"timeout-nanoseconds": 2_000_000_000},
    ).serve()

    started = time.monotonic()
    answer = unit.call({"text": "a", "double": False}, headers=unit.deadline_in(600))

    assert answer.status == 504
    assert time.monotonic() - started < 20, "the declared timeout did not bound the call"


def test_concurrent_calls_are_independent(runnable):
    unit = runnable(
        """
import time

def handle(context, input):
    time.sleep(0.5)
    return {"count": len(input["text"])}
""",
        **COUNT_CONTRACT,
    ).serve()

    answers = {}

    def call(index: int) -> None:
        answers[index] = unit.call({"text": "x" * index, "double": False})

    threads = [threading.Thread(target=call, args=(index,)) for index in range(1, 5)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    elapsed = time.monotonic() - started

    assert {index: answer.document()["count"] for index, answer in answers.items()} == {1: 1, 2: 2, 3: 3, 4: 4}
    assert elapsed < 2, "the calls were served one after another"


def test_a_receipt_recovery_package_serves_the_receipt_route(runnable):
    """Absent is "not yet known", never "no"."""
    unit = runnable(
        """
def handle(context, input):
    return {"count": 1}


def receipt_of(context, input):
    if input["text"] == "committed":
        return {"count": 7}
    return None
""",
        **COUNT_CONTRACT,
        recovery="receipt",
    ).serve()

    found = unit.call({"text": "committed", "double": False}, procedure=LOOKUP)
    assert found.status == 200
    assert found.document() == {"count": 7}

    absent = unit.call({"text": "never", "double": False}, procedure=LOOKUP)
    assert absent.status == 404
    assert absent.document()["code"] == "not_found"
    assert not absent.proven_no_effect, "an absent receipt is inconclusive, never proof"

    # An effect identity is what a receipt is keyed by, so a receipt-recovery
    # call carrying none is refused rather than run.
    none = unit.call({"text": "a", "double": False}, headers={"Codefly-Runnable-Effect-Id": None})
    assert none.status == 400
    assert "effect identity" in none.document()["message"]


def test_a_recompute_package_serves_no_receipt_route(runnable):
    """It has nothing to look up, so the route is absent rather than answering "never"."""
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT).serve()

    answer = unit.call({"text": "a", "double": False}, procedure=LOOKUP)

    assert answer.status == 404
    assert answer.document()["code"] == "unimplemented"


def test_a_receipt_package_without_a_lookup_refuses_to_start(runnable):
    """The refusal is at startup, not at the call that needed it.

    An owner serving no receipt cannot report an effect it did commit, and a
    caller reads that silence as inconclusive forever.
    """
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT, recovery="receipt").serve(wait=False)

    assert unit.exit_code() == 1
    assert "receipt_of" in unit.diagnostics()


def test_the_harness_cannot_choose_its_own_address(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT).serve(
        wait=False, environment={"CODEFLY__RUNNABLE_ADDRESS": None}
    )

    assert unit.exit_code() == 1
    assert "CODEFLY__RUNNABLE_ADDRESS is required" in unit.diagnostics()


def test_a_contract_of_another_protocol_is_refused(runnable):
    unit = runnable(COUNT_HANDLER, **COUNT_CONTRACT, protocol="codefly.runnable/v1").serve(wait=False)

    assert unit.exit_code() == 1
    assert "codefly.runnable.served/v1" in unit.diagnostics()


def test_a_handler_that_cannot_be_imported_refuses_to_start(runnable):
    unit = runnable("import nonexistent_module\n", **COUNT_CONTRACT).serve(wait=False)

    assert unit.exit_code() == 1
    assert "nonexistent_module" in unit.diagnostics()


def test_a_call_in_flight_is_given_its_grace(runnable):
    """Cutting calls off at a signal would make every one of them unproven at once."""
    unit = runnable(
        """
import time

def handle(context, input):
    time.sleep(2)
    return {"count": 42}
""",
        **COUNT_CONTRACT,
    ).serve()

    answers = []
    caller = threading.Thread(target=lambda: answers.append(unit.call({"text": "a", "double": False})))
    caller.start()
    time.sleep(0.5)
    unit.process.send_signal(signal.SIGTERM)
    caller.join(30)

    assert answers and answers[0].status == 200
    assert answers[0].document() == {"count": 42}
    assert unit.exit_code() == 0, "the signalled harness ended on its own once the call had finished"


def test_logs_never_carry_completion_data(runnable):
    """A handler that printed its output would be indistinguishable from a library that printed a warning."""
    unit = runnable(
        """
def handle(context, input):
    print("diagnostic on stdout")
    context.log("diagnostic on stderr")
    return {"count": 1}
""",
        **COUNT_CONTRACT,
    ).serve()

    answer = unit.call({"text": "a", "double": False})

    assert answer.document() == {"count": 1}
    assert "diagnostic" not in answer.body
