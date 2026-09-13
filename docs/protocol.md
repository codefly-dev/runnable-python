# `codefly.runnable/v1` — the launcher/harness seam

One invocation is one process. A launcher writes a **request document**, starts
the process, and reads a **completion document** from a path it chose. Both are
UTF-8 JSON. `stdout` and `stderr` carry logs and never carry data, so a
handler that prints cannot change an outcome.

This document describes what `pkg/contract` (Go) and
`pkg/harness/codefly_runnable` (Python) both implement.
`codefly-dev/core#472` is freezing this framing as a shared contract; the two
implementations here are this agent's proposal and its proof, and
`pkg/contract/contract_test.go` fails if they drift apart.

## Starting an invocation

| Variable | Meaning |
| --- | --- |
| `CODEFLY_RUNNABLE_REQUEST` | path of the request document the launcher wrote |
| `CODEFLY_RUNNABLE_COMPLETION` | path the harness writes the completion to |

Both are required. Paths rather than pipes, because the same framing has to
work for a native process the CLI supervises and for a Kubernetes Job that
mounts its request and collects its completion from a volume.

## Request

```json
{
  "schema": "codefly.runnable.request/v1",
  "protocol": "codefly.runnable/v1",
  "invocation": { "invocation": "inv-7f3a", "intent": "intent-2b19", "effect": "effect-64c0" },
  "runnable": { "name": "word-count", "module": "text", "workspace": "proof", "version": "0.1.0" },
  "deadline": "2026-09-13T10:15:00Z",
  "input": { "text": "one two three" }
}
```

`invocation.invocation` is required; `intent` and `effect` carry the caller's
own identifiers, so a handler records an effect under the identifier the caller
will look it up by. `deadline` is required and has a time zone: an invocation
never runs unbounded, and a harness never invents a bound.

`input` is bounded by `execution.payload.max-input-bytes` (1 MiB by default).
The payload is measured as compact UTF-8 JSON (no ASCII escaping), independently
of the framing. The envelope has a separate 64 KiB bound. The file read is capped
at the payload bound plus 64 KiB plus one sentinel byte, before parsing; the
parsed input is then checked against its own payload bound. This framing remains
a proposal for core #472; callers must not assume it is already a shared core API.

## Completion

```json
{
  "schema": "codefly.runnable.completion/v1",
  "protocol": "codefly.runnable/v1",
  "invocation": { "invocation": "inv-7f3a", "intent": "intent-2b19", "effect": "effect-64c0" },
  "runnable": { "name": "word-count", "module": "text", "workspace": "proof", "version": "0.1.0" },
  "outcome": "completed",
  "recovery": "recompute",
  "output": { "count": 3 }
}
```

The completion repeats the identity the request carried: a completion that is
not bound to the invocation it answers is not an answer. `output` is present
only when the outcome is `completed`; every other outcome carries
`error: {kind, message}` and no output. `recovery` repeats the declared effect
semantics, so a caller resolving an uncertain outcome knows whether recomputing
is allowed without re-reading the declaration.

The document is written to a temporary name and renamed into place, so a
launcher reads the whole document or none of it.

## Outcomes

| Outcome | Exit | When |
| --- | --- | --- |
| `completed` | 0 | the handler returned output that satisfies the contract |
| `invalid_input` | 64 | the request payload does not satisfy the input contract |
| `invalid_output` | 65 | the handler returned something the output contract rejects, or over the output bound |
| `failed` | 66 | the handler raised, exited, or could not be imported |
| `timeout` | 67 | the deadline passed |
| `interrupted` | 68 | the launcher signalled the invocation |
| — | 69 | protocol error: no completion is written because none is bound to an identity |

**Exit zero alone is not success.** A launcher reads the completion and checks
its schema, protocol and identity. A process that exits 0 without a readable,
identity-bound completion is an *ambiguous* invocation, and `recovery` decides
what may be done about it — never a silent retry of an external effect.

The first observed deadline or interruption retains precedence even when the
handler catches it and fails during cleanup or returns invalid output. Imports
run under the same timer and signal handlers as the function call; an already
expired request never imports author code.

## Logs

`stdout` and `stderr` are the log streams. Each forwards at most 256 KiB per
invocation plus one truncation marker; past the bound the harness writes one
`[codefly] log truncated at N bytes` marker and drops the rest. Truncation
never changes an outcome. The harness redirects file descriptors, so Python,
native-library and inherited subprocess writes all traverse the bound. The
launcher must additionally bound its log transport and supervise the entire
process group, including descendants that outlive the harness. That ownership
must be ratified in core #472.

## The bounded schema profile

`object`, `array`, `string`, `integer` (signed 64-bit) and `boolean`, with
`optional` (the key may be absent) and `nullable` (the value may be null)
independent of each other. Validation refuses every coercion: `"3"` is not an
integer, `3.0` is not an integer, `1` is not a boolean, and a key the contract
does not declare is an error rather than an ignored extra. An explicitly empty
object schema is valid and accepts exactly `{}`.

Errors name the path they were found at — `input.options.stop_words[1] must be
a string, got integer` — because the caller that wrote the payload is the one
who has to fix it.
