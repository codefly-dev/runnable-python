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
repeated length, a sign or any other spelling is refused `400`, unread; so is a
request carrying a header line the parser could not read, which would otherwise
end the parsed headers early and hide every header after it. A body that ends
short of its length is refused `400`. A request asking `Expect: 100-continue` is
told the refusal instead of being invited to upload. **Every answer but a `200`
closes the connection** (`Connection: close`), so bytes a refused call did not
consume are never read as the next call — under whatever Work Context those
bytes name.

### What a peer can hold

Every bound below is in `codefly_runnable/protocol.py`, and each is the harness's
own: none is a value a placement resolved.

| Bound | Value | At the bound |
| --- | --- | --- |
| reading one request, head and body together (`REQUEST_READ_SECONDS`) | 10 s wall clock, from its first byte | `408` once the body has begun; a head that has not arrived is dropped unanswered |
| the request line and headers (`MAX_HEAD_BYTES`) | 32 KiB | `431` |
| requests being read at once (`READ_MEMORY_BYTES` ÷ (32 KiB + `max-input-bytes`), at most 1,024) | 256 MiB of heads and bodies: 248 readers at the default 1 MiB input | `503`, before any of the request is read |
| one request's body, whatever the declaration (`READ_MEMORY_BYTES` − 32 KiB) | just under 256 MiB | `413`: a declared `max-input-bytes` larger than the whole read budget is held to the budget |
| documents being decoded | one at a time | waits its turn, still holding its reader |
| calls whose handler is running (the declared `concurrency`) | `HARNESS_CONCURRENCY`, 32, when the declaration states 0 | `503` |
| open connections (readers + concurrency + `IDLE_CONNECTIONS`, 64) | — | the connection idle the longest is closed to make room; a new one is never refused, and a request that begins on a connection in the instant it is closed is refused unread rather than run with nowhere to answer |
| an idle kept-alive connection (`IDLE_SECONDS`) | 120 s, advertised as `Keep-Alive: timeout=120` | closed |
| one write of an answer (`WRITE_STALL_SECONDS`) | 10 s | the connection is dropped |

The read deadline is one instant per request, not a bound on each read: a peer
sending a byte every few seconds is cut when the request's time is up, however
regularly it sends. A call takes its concurrency slot only once its whole
request has arrived, so a request still arriving holds no slot, and a slow peer
costs a reader — bounded by memory, released at the deadline — rather than a
call. A slot is held until the handler really returns, not until the call is
answered: a call answered `504` at its deadline keeps its slot while its handler
runs on, so the declared concurrency bounds author code running.

A request keeps its reader until its body has been decoded and checked against
the input schema: a decoded document can be many times the bytes it came from —
an array of empty objects is about twenty — so the readers' bound is a bound on
decoding only while decoding sits inside it. The order is: read the body, take a
concurrency slot (or answer `503` without decoding anything), decode and check
the payload one call at a time, release the reader, run the handler. A payload
the schema refuses is dropped before the next is decoded, so beyond the read
budget the process holds one document being decoded and, under each running
call's slot, a payload its contract admits — the envelope the declaration's
`max-input-bytes` and `concurrency` state.

Decoding one call at a time is first come, first served, so a small call can
wait behind every decode queued ahead of it. The wait is bounded: admission comes
before the queue, so at most min(concurrency, readers) calls are ahead, each one
decode and check of at most `max-input-bytes`. A 1 MiB body whose last element
is the one the schema refuses takes about 0.2 s, so at concurrency 32 a call
waits at most about six seconds behind a full queue. A review measured the
median of small calls rising from 0.58 s to 1.69 s against 24 peers looping such
bodies, every call still answered `200`.

**Concurrency is not yet one number.** Core's declaration says `0` means the
facility default and defines none, so the harness admits 32 when the declaration
is silent. The durable-work runtime decides how many calls it dispatches from
its own installation material (1 to 200), not from the declaration. When the
runtime dispatches more than the harness admits, each excess call answers `503`:
an unproven outcome the caller recovers by its recovery policy. Until the
placement passes the number it resolved, declare `concurrency` and install with
the same number. A declaration of `0` is not refused: core's own scaffold
declares no concurrency, so refusing it would refuse every scaffolded runnable.

The idle timeout is longer than the idle timeout of the pool the durable-work
runtime calls through (Go's `net/http`, 90 s by default), so the caller retires
an idle connection before the harness closes it. The other order makes a call
written onto a connection the harness had just closed come back as a reset — an
unproven outcome for a call that never ran. A proxy pooling connections to the
harness needs an idle timeout under 120 s for the same reason; Envoy's default
of one hour is not. Under the connection bound, the harness closing an idle
connection to make room has the same cost, which is why that bound sits beside
every reader and every running call rather than in place of them.

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
as any untyped error does: unproven. The code is read-only, and where the
header is set it is read exactly once, checked, and that same value is sent: a
code changed after construction — even by a subclass whose `code` property
returns something else on a later read — answers `500` with no failure code, and
so does a code that raises when read.

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
| was never reached (payload, identity, effect id, framing) | `400`/`408`/`411`/`413`/`431`, no failure code | unproven |
| was never reached (over a bound, or stopping) | `503`, no failure code | unproven |

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
would have returned is no longer that call's outcome. It keeps its concurrency
slot until it returns.

## Signals and logs

A signal stops the harness taking new calls — on new connections and on ones
already open, which answer `503` with `Connection: close` — and gives the calls
in flight `SHUTDOWN_GRACE_SECONDS` (30 s) to finish — including a handler whose
call was already answered at its deadline. Cutting them off would turn every one
of them into an unproven effect at once. The harness exits `0` as soon as the
last handler has returned; an idle connection holds nothing. It is an ordinary
interpreter exit: the handler module's `atexit` hooks run and its buffered
writes are flushed, so a `logging` handler, a telemetry exporter or a client
that flushes at exit still does. If the grace ends with handlers still running,
they are cut off and the harness exits `1`, saying how many effects it left
unproven. That exit cannot wait for interpreter shutdown beside author code
that is still running and may hold any lock, so the `atexit` hooks run and the
standard streams are flushed for at most `CLOSE_SECONDS` (2 s), and the process
then ends with `1` directly; a buffered write the handler never flushed and no
hook flushes is lost there.

`stdout` and `stderr` carry diagnostics only, never completion data: a handler
that printed its output would be indistinguishable from a library that printed a
warning. The harness's own diagnostics never wait on the stream: they queue for
one writer that writes the descriptor directly, so a placement that does not
read stderr cannot stall a call that holds its slot, and no harness thread holds
the stream's lock when the process exits. Past 1,024 queued diagnostics a new
one is dropped and counted, and the count is written at exit if the stream then accepts it. A handler's own
`print` and `context.log` write the stream as any code does, and wait on it as
any code does. The harness does **not** bound the streams. Per-call stream bounding is not
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

`object`, `array`, `string`, signed 64-bit `integer`, `boolean` and `file` are supported.
`optional` and `nullable` remain distinct. There is no coercion: `3.0` is not an
integer, `1` is not a boolean, undeclared keys are errors, and a repeated key is
refused rather than resolved to its last value. Empty schemas accept exactly
`{}`. Validation errors identify the offending input or output path. Both
payloads are bounded by the declared `max-input-bytes` / `max-output-bytes`,
measured on the document that is actually sent.

### Documents

A `file` field is a document, declared with the exact media types its handler
reads (`media-types: [application/pdf]`). On the wire it is core's
`runnable.FileValue`: one closed object with `reference`, `media_type`, an
optional `name` and the `content` in standard padded base64. Any other member —
a `path`, a `url` — is refused `400` before the handler runs. The caller staged
the bytes with its own authority; the harness has no network and never
dereferences the reference.

The handler reads each input file as `codefly_runnable.File` (`media_type`,
`content` as `bytes`, `reference`, `name`), and the generated types annotate
the field with that class. A `File` it returns is encoded back and checked: its
media type must be declared, and its `reference`, when set, must be one of the
call's input references — the output is then a new version of that document;
absent, it is a new document. An output naming any other reference answers
`500`, unproven, like every answer the contract refuses. The caller publishes
what was returned; a runnable never writes a document in place. The content
travels inside the payload, so `max-input-bytes` and `max-output-bytes` bound
the documents one call carries.

## The image recipe

`Builder.Build` emits the Linux image of the prepared snapshot as a
`DockerBuildPlan`: the Dockerfile and its context assembled in the caller's
`output_directory` (`RECIPE_CONTEXT_ROOT_OUTPUT`), the image named by the
caller's `docker_repository` and the release version. The image installs the
same hashed requirements the native archive was prepared from, runs as UID
65532 and starts the same harness, so a placement that runs it serves exactly
what `Package` archived. The agent never runs a container build; the caller
verifies the plan and builds it.
