# Milestone 2 — correct Python packages and execution outcomes

PR #2 implements Python generation, preparation, packaging and the invocation
harness. The shared gRPC lifecycle and durable native/k3d qualification remain in
issue #1 and core #472. No release is published by this milestone.

## Corrected behavior

| Problem | Result |
|---|---|
| Native virtualenv linked to the builder's interpreter | The archive contains a managed CPython distribution, standard library and shared libraries; internal symlinks are relative and confined |
| Live source edits changed build evidence | Handler, declared-input and harness digests are measured from the prepared snapshot that is archived |
| Missing descendants bypassed symlink confinement | Scaffolding resolves the nearest existing ancestor and refuses paths escaping the Runnable directory |
| Linux Go module graph was incomplete | Module metadata includes the platform dependencies required by the Linux build |
| Request framing consumed the input payload budget | Compact UTF-8 payload and framing have separate bounds; the request file read is bounded |
| Cleanup errors hid timeout/interruption | The first recorded signal retains precedence, with subsequent failures included in diagnostics |
| Author imports ran outside the timer | Expired requests never import author code; imports and handler execution share deadline/signal supervision |
| Python keywords produced syntax errors | Functional TypedDict declarations preserve wire names, including keywords and leading double underscores |
| Nested type names overwrote each other | A shared allocator assigns distinct Python type names |
| Native/subprocess writes bypassed log limits | File-descriptor redirection bounds all writes during harness execution, with one truncation marker per stream |

The native command is `.python/bin/python -I main.py`. Dependencies are installed
from hashed requirements into `.packages`; isolated interpreter mode prevents
ambient Python configuration from overriding the packaged runtime. Preparation
requires an empty destination so removed source files cannot survive a rebuild.
The Linux image recipe continues to create its own environment and excludes the
host interpreter and installed native dependency directory from its context.

## Verification

The native integration test generates a real neutral operation, prepares and
archives it, then installs the archive elsewhere. It changes the live handler
after preparation and checks the evidence against the actual archived handler.
It removes the prepared tree and makes the private builder interpreter
installation unavailable before invoking the installed package with different
inputs. The original typed results still complete successfully.
The handler imports the locked third-party package `idna==3.10` from the installed
archive, proving dependency installation travels with the interpreter.

Generated Python is imported by a real interpreter, including keyword fields,
colliding nested names and optional-field metadata. Harness process tests cover
the payload boundary, expired imports, import timeout, cleanup failures after
cancellation, and native/subprocess log writes. The harness suite has 48 cases.
Go tests, vet and the Linux build are run separately from the Python harness suite.

The earlier image/k3d observations in `qualification.md` identify the pre-review
experiment. They do not qualify this revision through Codefly and Orchestration.

## Shared handoff still to establish

`codefly.runnable/v1`, the request/completion framing, the 64 KiB envelope bound
and the build-evidence JSON in this repository remain proposals for core #472.
The CLI does not consume the evidence file yet. The agent exposes metadata over
gRPC but advertises no Builder capability until the shared interface exists.

The launcher must enforce the absolute deadline and cancellation on the whole
process group, bound its log transport, and handle missing/ambiguous completions.
The harness's cooperative timer cannot terminate an uninterruptible native call
or control descendants after the harness exits. Those responsibilities belong
in the shared handoff and CLI/Orchestration supervision.

Next: ratify core #472, implement generation/build through gRPC, then qualify real
typed invocation through Codefly and Orchestration natively and with
Orchestration-created invocation Jobs in disposable k3d.
