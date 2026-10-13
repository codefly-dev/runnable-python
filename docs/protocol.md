# `codefly.runnable.served/v1` — the harness a Python runnable serves

Core defines the contract in
[`runnable/served.go`](https://github.com/codefly-dev/core/blob/main/runnable/served.go)
and [`runnable_invocation.proto`](https://github.com/codefly-dev/core/blob/main/proto/codefly/base/v0/runnable_invocation.proto),
and supplies `PrepareInvocation`, `ParseResult`, `ClassifyServed` and
`ServedOutcomeIsCertain`. This harness implements the owner's half; the Go
qualification tests call the real package over HTTP the way the durable-work
runtime's invoker calls it and classify every answer with core's own
`ClassifyServed`, so a harness that drifted from the contract fails there rather
than in a live composition.

**A runnable is called, not launched.** The launcher framing this replaced —
two document paths in the environment, a request read off disk, a result renamed
into place, and an exit code a launcher interpreted — went with the native
placement (core#681). A package naming `codefly.runnable/v1` is refused by name.

## Transport

The package's command starts a server. The one fact it cannot derive is where to
listen: the port is allocated by whatever placed it, and a harness choosing its
own would be right on one machine for ten minutes.

| Environment variable | Value |
| --- | --- |
| `CODEFLY__RUNNABLE_ADDRESS` | the allocated `host:port`, required |

Both halves are required: `[v6]:port` or `host:port`, with a port from 1 to
65535 in ASCII digits. A missing host would bind every interface, and port `0`
would let the operating system pick a port nobody was told about, so the
harness refuses either and exits 1.

## What the harness serves

The generated harness answers in the reply to the call. A declaration it cannot
honour is refused at Builder `Load` and at generation, before any package
exists:

- `completion: submit` — a submitted call owes an acceptance, a status route and
  callbacks, none of which this harness serves.
- `cancellation: signal` — the handler runs on a thread Python cannot interrupt,
  so the explicit interrupted outcome `signal` promises could never be reported.
  Declare `none`; the deadline still bounds every call (see below).

One call is `POST` to a procedure core pins, with the bounded input document as
the whole body and no Codefly envelope around it — a field Codefly added to an
owner's body would be a field the owner never described.

The body is read only by a `Content-Length` written in ASCII digits, at most
`max-input-bytes`. A missing length is refused `411`; a `Transfer-Encoding`, a
repeated length, a sign or any other spelling is refused `400`, unread; a body
that ends short of its length is refused `400`. **Every answer but a `200`
closes the connection** (`Connection: close`), so bytes a refused call did not
consume are never read as the next call — under whatever Work Context those
bytes name.

The harness bounds what a peer can hold. A read or write that stalls for
`IO_TIMEOUT_SECONDS` (10 s) ends the connection, answering `408` when it was a
body that stalled; the same timeout releases an idle kept-alive connection.
Simultaneous calls are bounded by the declaration's `concurrency`
(`DEFAULT_CONCURRENCY`, 32, when it declares 0, the facility default); a call
over the bound is refused `503` before the handler runs. Open connections are
bounded at twice that, and one over it is closed unread.

| Route | Answers |
| --- | --- |
| `/codefly.runnable.v0.Runnable/Invoke` | one call: the bounded output document, 200 |
| `/codefly.runnable.v0.Runnable/Lookup` | what an effect committed; served only by a `receipt` package |

The per-call facts that are not the body travel as headers, spelled exactly as
core spells them. `pkg/contract`'s `TestTheFramingIsOneContract` holds the Python
literals equal to core's constants, because nothing at build time connects them.

| Header | Meaning |
| --- | --- |
| `X-Codefly-Work-Context` | the caller's minted capability, carried verbatim — **required** |
| `Codefly-Runnable-Effect-Id` | the effect identity a receipt is keyed by; required for `receipt` recovery |
| `Codefly-Runnable-Deadline` | when the caller stops waiting, RFC 3339 with nanoseconds |
| `Codefly-Runnable-Failure-Code` | on the answer: the operation's own typed failure code |

Each per-call header is read as exactly one value with no control character — a
repeated header or an obsolete line fold is refused rather than resolved — and
within core's bounds on the same values (`runnable_invocation.proto`): 8,192
characters for the Work Context, 128 for the effect id. `TestTheHeaderBoundsAreCoresBounds`
reads both out of core's descriptors.

A call carrying no Work Context is **refused**, not run under whatever identity
the process happens to have: that is the state core's required identity slot
exists to remove. The harness never parses or reconstructs the capability — the
handler reads it from `context.invocation.work_context` and forwards it to
whatever it calls in turn, and that is the whole of what either does with it.

## What an answer proves

Only the handler's own `HandlerFailure` proves that no effect committed, and it
is the only thing that sets the failure-code header:

```python
from codefly_runnable import HandlerFailure
raise HandlerFailure("card_declined", "the issuer declined")
```

The code becomes a response header, so it must match
`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$` — the grammar the durable-work runtime reads
it with, which is narrower than core's 1 to 128 characters. A code outside it
raises `ValueError` where it is constructed, and the handler's call then answers
as any untyped error does: unproven.

Everything else leaves the effect **unproven**, because the harness cannot know
which side of its effect a handler stopped on. A caller that read a bare status
as proof would stop looking for a receipt that exists.

| The handler | Answer | What the caller concludes |
| --- | --- | --- |
| returned output the contract admits | `200`, the output document | proven committed |
| raised `HandlerFailure` | `422` + the failure-code header | proven not committed |
| raised anything else | `500`, no failure code | unproven |
| was still running at the deadline | `504`, no failure code | unproven |
| returned an answer the contract refuses | `500`, no failure code | unproven |
| was never reached (payload, identity, effect id, framing) | `400`/`408`/`411`, no failure code | unproven |
| was never reached (over the concurrency, or stopping) | `503`, no failure code | unproven |

The statuses are for humans and proxies: core classifies on the failure-code
header and the answer document, never on a status class.

## Deadlines

`Codefly-Runnable-Deadline` bounds the call. It is an absolute instant, and the
budget is that instant less the harness's own clock, so an offset between the
caller's clock and the harness's moves it: a harness clock running ahead
shortens the budget, and one running behind extends it. Neither can carry it
past the declared timeout: a caller may shorten the declared timeout and never
overrule it, the budget is the smaller of the two, and the declared one applies
when no header is sent. A deadline already in the past, or one naming no
representable instant, is refused before the handler runs.

The handler runs on its own thread, because Python cannot raise into one. When
the budget passes the harness answers and the handler is still running; what it
would have returned is no longer that call's outcome.

## Signals and logs

A signal stops the harness taking new calls — on new connections and on ones
already open, which answer `503` with `Connection: close` — and gives the calls
in flight `SHUTDOWN_GRACE_SECONDS` (30 s) to finish. Cutting them off would turn
every one of them into an unproven effect at once. The harness exits `0` as
soon as the last call in flight has answered; an idle connection holds nothing.
If the grace ends with calls still running, they are cut off unanswered and the
harness exits `1`, saying how many effects it left unproven.

`stdout` and `stderr` carry diagnostics only, never completion data: a handler
that printed its output would be indistinguishable from a library that printed a
warning. The harness does **not** bound them. Per-call stream bounding is not
something a server can do — concurrent calls share the process's descriptors —
and `max_log_bytes` describes what the placement capturing those streams
enforces, which is where it belongs.

## The receipt route

A package declaring `recovery: receipt` serves `Lookup`, and its handler module
defines `receipt_of(context, input)` beside `handle`. Returning the output
document answers 200; returning `None` answers 404, which is **not** an error:
absent is "not yet known", never "no", and a caller reads it as inconclusive
rather than as permission to invoke again.

**`receipt_of` must authorize the lookup itself.** The harness requires a Work
Context on the call but does not verify it, and it does not bind the effect id to
the identity that created the effect: whoever can reach the route and names an
effect id is asking. A receipt function answers only for effects the calling
Work Context may read.

The function is required at startup rather than at the call that needed it: an
owner that cannot answer what an effect committed leaves every uncertain outcome
uncertain forever, and that is not something to discover during a recovery. A
`recompute` package has nothing to look up, so the route is absent rather than
present and answering "never".

## Payload schema

`object`, `array`, `string`, signed 64-bit `integer` and `boolean` are supported.
`optional` and `nullable` remain distinct. There is no coercion: `3.0` is not an
integer, `1` is not a boolean, undeclared keys are errors, and a repeated key is
refused rather than resolved to its last value. Empty schemas accept exactly
`{}`. Validation errors identify the offending input or output path. Both
payloads are bounded by the declared `max-input-bytes` / `max-output-bytes`,
measured on the document that is actually sent.
