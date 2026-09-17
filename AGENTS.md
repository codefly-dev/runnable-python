# Working in codefly-dev/runnable-python

`github.com/codefly-dev/runnable-python` (Go 1.27) is the Codefly Runnable agent
for Python. It owns everything Python about a runnable: the handler scaffold and
the typed bindings of a contract, the `codefly.runnable/v1` harness generated
into every package, uv-locked dependency and interpreter preparation, the native
package and the build evidence identifying it, and the Linux image recipe.

It does **not** own the invocation and result framing, `RunnableLocation`,
`RunnableBuild` or `PackageArtifact.command` — those belong to
[`codefly-dev/core`](https://github.com/codefly-dev/core), and `pkg/contract`
here is agent-*private* generated configuration, not wire framing. It does not
own process supervision, the command surface, or image building and publishing
— those are the CLI's; nor installed releases, recorded invocations and recovery
— those are Orchestration's. It advertises Builder only: nothing here supervises
a running invocation. Generated handlers belong to their owner's workspace.

The package-by-package map is in the [README](README.md); read it there rather
than a second copy here.

## How to behave

Fleet standard — [handbook#68](https://github.com/obin-ai/handbook/issues/68).
They land hard here: this agent writes code and environment that run later, on
someone else's machine, from bytes nobody re-reads. A value hand-placed here is
baked into a package and shipped.

- **A gap in the tooling is a bug in the tooling — never a reason to reach
  around it.** When a step `codefly`, `uv` or core does not perform is needed,
  the answer is a capability fixed in whichever of them owns it, named in the
  PR. Never a hand-assembled substitute — not as a "workaround", not "just this
  once", not "until the capability lands".
- **Never hack. Provide the best fix, even when it spans repos.** The fix living
  in `codefly-dev/core` or `codefly-dev/cli` is not a reason to work around it
  here — open the PR there and consume the reviewed result. When it genuinely
  cannot be fixed now, the deliverable is a precise issue against that owner
  plus an explicitly labelled stopgap, never an unlabelled one.
- **Classify every change that makes something work**, in the PR body: a *fix*
  at the place that owns the behaviour, or a *hack*. A hack does not become a
  fix by working, by being small, by being local, or by the real fix belonging
  elsewhere.
- **Never hardcode what the system resolves** — injected environment, derived
  ports, service addresses, credentials copied out of another component. Typing
  one encodes something true only on one machine for ten minutes, and it fails
  quietly: a runtime missing a credential can skip registration *silently*, so
  the thing boots, serves, and is simply absent. Here the same shape appears as
  a pinned interpreter path, a guessed `uv` location or an inlined digest —
  every one of them is resolved, measured or declared by code that already
  exists.
- **Diagnose, do not pattern-match.** "It started working when I set X" is not a
  diagnosis — set X back and confirm it breaks. Do not trust an error message
  before checking its claim: in the session behind this standard, a runtime
  reporting *"the provisioned secret does not match its digest"* actually meant
  a missing internal token, and the digest was correct all along.
- **Say what you did not verify.** Unverified is not the same as working. The Go
  suite is not the harness suite, and neither is a real invocation on a prepared
  package; if you could not exercise something, the PR says so.

## Build and test

Derived from [`.github/workflows/ci.yml`](.github/workflows/ci.yml), which runs
two jobs. Both are runnable locally, and both need `uv` on `PATH`: `prepare`
shells out to it and the agent advertises the local backend by probing for it.

```bash
go build ./...
go vet ./...
go test ./...                     # ~20s: the suite really locks, downloads
                                  # an interpreter, packages and invokes
cd pkg/harness && uv run --with pytest --python 3.12 pytest -q
```

No test skips itself when a prerequisite is missing — without `uv` the Go suite
fails rather than passing hollow. Keep it that way: a prepared tree that was
never prepared is not evidence.

`builder_grpc_test.go` is the end-to-end path. It compiles the binary, installs
it where `manager.Load` looks, and drives `Load`, `Create`,
`RunnableBuildInputs` and `Package` over real gRPC — exercise the lifecycle
through it rather than hand-running the binary, which is a plugin server and not
a CLI.

`docs/protocol.md` is the live contract. `docs/qualification.md` and the
milestone documents are dated records of older commits, explicitly superseded;
do not run commands out of them or trust their digests.

## Rules that bite

- **`.codefly/` is the agent's, the handler is the author's.** `Scaffold` never
  overwrites a handler — regenerating must not silently discard an
  implementation. Nothing an author edits may move under the generated
  directory.
- **The bounded schema profile is stated twice**, and the two must agree:
  `pkg/generate/types.go` emits the TypedDicts an author's type checker sees,
  and `codefly_runnable/schema.py` enforces the same profile at invocation time.
  A change to one is a change to both, with tests in both suites.
- **Package bytes are the identity.** The harness digest is sha256 over the
  harness `.py` sources; the generated contract is encoded with stable key
  order; the archive is deterministic across machines. Anything that makes the
  same declaration produce different bytes — a timestamp, a map iteration, a
  newly-embedded non-`.py` file — breaks the promise a launcher relies on.
- **Measure, never assume, what identifies a build.** The toolchain is read out
  of the interpreter that was prepared, not from the requested version; the
  harness digest is measured from the tree that was generated, not from the
  agent version. Keep both readings at the bytes.
- **Builder state is per-process.** The lock serializes calls but cannot tell
  two callers apart, and `Load` replaces what it finds, so one connection drives
  one Runnable at a time. Concurrent builds get one agent process each — do not
  add a session map to paper over this.
- **The agent version is pinned in more than one place.** `agent.codefly.yaml`
  is the source, and every test declaration pins the same agent; bumping one
  alone fails `Load` with `declaration pins a different Runnable agent`. See the
  release skill before touching it.

## Procedures

Step-by-step procedures live in `.claude/skills/`, loaded on demand rather than
carried here:

- `cut-release` — publishing a version, what the tag triggers, and the asset
  names core's downloader requires.

## Workflow

- Branch and PR; never commit to `main`. Conventional Commits for the title.
- Keep this file under ~150 lines (hard cap 200). Push depth into a nested
  `AGENTS.md` beside what it describes, into `.claude/skills/`, or into `docs/`.
- If a `CLAUDE.md` is ever added, it is a one-line `@AGENTS.md` pointer. One
  canonical source.
- Treat this file as code: the PR that changes a process updates it.
