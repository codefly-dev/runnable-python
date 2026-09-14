# `codefly.runnable/v1` — launcher and Python harness

Core defines the public protocol in
[`runnable_invocation.proto`](https://github.com/codefly-dev/core/blob/b3470f0096cd818d6a90c67dc31e9ad09824d822/proto/codefly/base/v0/runnable_invocation.proto)
and supplies `PrepareInvocation`, `EncodeInvocation`, `InvocationEnvironment`,
`ParseResult` and `Complete`. The Python process implements this wire format;
Go qualification tests encode real requests with core and parse the actual
Python result with core. The earlier agent-specific framing is obsolete.

## Transport

One invocation is one process, with package-relative argv and the unpacked
artifact root as its working directory. No shell expansion is involved.

| Environment variable | Value |
| --- | --- |
| `CODEFLY__RUNNABLE_PROTOCOL` | `codefly.runnable/v1` |
| `CODEFLY__RUNNABLE_INVOCATION` | absolute path to the launcher's request |
| `CODEFLY__RUNNABLE_RESULT` | absolute path for the harness's atomic result |

The documents use proto3 JSON with snake_case field names. `input` and `output`
are base64 encodings of exact UTF-8 JSON objects. Bounds apply to decoded bytes,
including whitespace, and preserve signed 64-bit integers. The harness caps its
request read at base64-expanded input capacity plus 64 KiB of framing.

```json
{
  "protocol": "codefly.runnable/v1",
  "runnable": {"name":"word-count","module":"proof","workspace":"proof","version":"0.0.1"},
  "invocation_id": "inv-1",
  "intent_id": "intent-1",
  "issued_at": "2026-09-13T10:00:00Z",
  "deadline": "2026-09-13T10:01:00Z",
  "input": "eyJ0ZXh0Ijoib25lIHR3byB0aHJlZSJ9"
}
```

This input decodes to `{"text":"one two three"}`. An invocation must match the
release embedded by the Builder. Standalone generation, which has no owning
workspace, checks the declaration's name and version. `invocation_id` and
`intent_id` are required and at most 128 characters. `effect_id` is required for
`receipt` recovery and optional for `recompute`.

## Results and certainty

A successful result for the example is:

```json
{"protocol":"codefly.runnable/v1","invocation_id":"inv-1","status":"SUCCEEDED","output":"eyJjb3VudCI6M30="}
```

The output decodes to `{"count":3}`. The harness validates the output schema and
byte limit before atomically renaming the result into place. It reports a certain
operation failure only when the handler explicitly raises:

```python
from codefly_runnable import HandlerFailure
raise HandlerFailure("unavailable", "The operation was refused")
```

This writes `status: FAILED` with `error: {code, message}` and no output. Use it
only when the operation knows its effect's disposition. An unexpected exception,
invalid payload, invalid return, import failure or timeout writes no result.
When the generated contract declares signal cancellation, a handled interruption
writes `status: INTERRUPTED` without output. Core classifies it as `CANCELED`,
which leaves the effect uncertain; it is never a certain operation failure.
A contract declaring no cancellation does not install interruption handlers.

To prevent an earlier result from surviving an outcome that writes nothing, the harness removes any document at the
result path before it reads the request, and refuses to start if it cannot. A
document at that path is always this invocation's, so a launcher reusing the
path cannot read an earlier attempt's result as this one's. The result is
written 0644: which accounts may read it is the facility's choice. Exit codes
are diagnostics, not portable completion outcomes:

| Exit | Diagnostic |
| --- | --- |
| 0 | validated success, unless author code bypassed the harness |
| 64 | input schema rejection before author import |
| 65 | invalid or oversized output, or result write failure |
| 66 | explicit failure or unexpected handler/import exception |
| 67 | harness deadline timer |
| 68 | harness interruption |
| 69 | invalid framing or environment |

The launcher calls core `Complete`. A valid result wins; otherwise a launcher
termination reason takes precedence, then an invalid result, then a nonzero
exit/signal, then exit zero with no result. A harness exit 67 by itself is
`CRASHED`; the launcher must record its own deadline termination to classify
`TIMED_OUT`. Only `SUCCEEDED` and explicit `FAILED` are certain.

## Deadlines and logs

For the native checkpoint, the harness preserves the original absolute deadline
on the same host. Expired requests never import author code. The timer covers
imports and the handler, and a swallowed signal cannot produce success. The
launcher remains responsible for terminating the process group and descendants.
Core #474 describes a clock-offset budget based on `deadline - issued_at`; the
cross-host clock policy needs reconciliation before Kubernetes qualification.
No distributed deadline or cancellation guarantee is claimed here.

`stdout` and `stderr` carry diagnostics only. The harness's own terminal
diagnostic is written after the handler's bound is released, so a handler that
exhausts the budget cannot erase the single record of why an uncertain
invocation ended. The generated harness uses the
release's `max_log_bytes` (4 MiB by default), bounding Python, native-library and
inherited subprocess writes. It may append one truncation marker. The launcher
must independently enforce its exact transport bound and record truncation.

## Payload schema

`object`, `array`, `string`, signed 64-bit `integer` and `boolean` are supported.
`optional` and `nullable` remain distinct. There is no coercion: `3.0` is not an
integer, `1` is not a boolean, and undeclared keys are errors. Empty schemas accept
exactly `{}`. Validation errors identify the offending input or output path.
