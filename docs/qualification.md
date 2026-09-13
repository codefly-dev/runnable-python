# Qualification milestone — 2026-09-12

**Historical baseline at `b4d5c12`.** Its request/result commands use superseded framing; see [milestone 3](milestone_3_builder_grpc.md) and [the current protocol](protocol.md). The review changed the harness and native
archive layout, so the digests and `.venv` command below identify the earlier
experiment. See [milestone 2](milestone_2_review_fixes.md) for the corrected
implementation and current checks. The earlier k3d runs remain debugging evidence.

What this agent has been proven to do, what it has not, and where the next
session resumes. [Issue #1](https://github.com/codefly-dev/runnable-python/issues/1)
is the acceptance list this measures against.

## Identities

| Component | Exact identity |
| --- | --- |
| `runnable-python` agent | `codefly:runnable` / `python` / `0.0.1`, publisher `codefly.dev` |
| `codefly-dev/core` | `v0.3.28-0.20260912222100-6a40c4bf28ac` (the merged Runnable slice) |
| Python interpreter | `python-3.12.14`, prepared by `uv 0.12.13` |
| Harness | `sha256:eeb8c9a90c1d160b68e3ae18aa189ecd48fd664903cace6acf67a7513448b11e` |
| k3d / k3s | k3d cluster `runnable-proof`, `v1.35.5+k3s1` (deleted, see Cleanup) |

The harness digest comes from the embedded sources, and the toolchain is read
out of the interpreter that was prepared — never from the requested version or
from a resolved-plan fingerprint.

## Proven

The example is a neutral `word-count` runnable generated into a workspace
outside this repository, core and the CLI. Its output depends on two different
valid inputs: the text, and the optional nullable `options.stop_words`.

### Native

```
go run . <workspace>     # declare → generate → prepare → package → invoke
```

Build evidence (`runnable-build.json`, abridged):

```json
{
  "schema": "codefly.runnable-build-evidence/v1",
  "agent": {"kind": "RUNNABLE", "name": "python", "version": "0.0.1", "publisher": "codefly.dev"},
  "identity": {"name": "word-count", "version": "0.1.0"},
  "build": {
    "handler": {"path": "handler.py", "digest": "sha256:dbfa4275e20b…"},
    "inputs": [{"path": "pyproject.toml", …}, {"path": "uv.lock", …}],
    "harnessDigest": "sha256:eeb8c9a90c1d…",
    "toolchain": "python-3.12.14",
    "configurationDigest": "sha256:f634dff5bf2f…"
  },
  "artifacts": [{
    "kind": "NATIVE", "platform": "darwin/arm64",
    "reference": "word-count-0.1.0.tar.gz", "digest": "sha256:3c96972df8f2…",
    "command": [".venv/bin/python", "main.py"]
  }]
}
```

Invocations through the installed package, unpacked under a path other than the
one it was built in:

| Input | Exit | Completion |
| --- | --- | --- |
| `{"text":"the quick brown fox"}` | 0 | `completed`, `{"count":4}` |
| `{"text":"the quick brown fox","options":{"stop_words":["the"]}}` | 0 | `completed`, `{"count":3}` |
| `{"text":"one two","options":{"stop_words":null}}` | 0 | `completed`, `{"count":2}` |
| `{"text":42}` | 64 | `invalid_input`, `input.text must be a string, got integer` |

### Linux image

The generated recipe built unmodified, and the image honored the same
operation contract with a different artifact identity:

```
docker build --platform linux/arm64 -t runnable-python-proof:0.1.0 .
docker run --rm -v …/inv:/inv \
  -e CODEFLY_RUNNABLE_REQUEST=/inv/request.json \
  -e CODEFLY_RUNNABLE_COMPLETION=/inv/completion.json runnable-python-proof:0.1.0
```

Image `sha256:081289b72f1297ba710509412dffed1240858cfad58297f225b57644c2317b9d`,
exit 0, `completed` with `{"count":3}`.

### Kubernetes Jobs in disposable k3d

```
k3d cluster create runnable-proof --no-lb --api-port 127.0.0.1:6553
k3d image import runnable-python-proof:0.1.0 -c runnable-proof
kubectl apply -f job.yaml        # request from a ConfigMap, completion on an emptyDir
```

| Job | Container exits | Completion |
| --- | --- | --- |
| `word-count-inv-k3d-1` | `invocation=0` | `completed`, `{"count":3}`, bound to `inv-k3d-1` |
| `word-count-inv-k3d-2` | `invocation=64` | `invalid_input`, bound to `inv-k3d-2` |

Both completions carried the original invocation, intent and effect
identifiers, and Kubernetes surfaced the harness's distinct exit code.

### Repository tests

`go test ./...` covers generation, packaging determinism, the agent/CLI
evidence and the native round trip through real processes.
`uv run --with pytest pytest` (in `pkg/harness`) covers the bounded profile and
every outcome of the framing — including a swallowed deadline, a swallowed
interruption, an exit-zero process that writes no completion, log truncation
and both payload bounds — each through a real process.

## Not proven, and why

These acceptance items are blocked on shared work in other repositories. None
of them was approximated here, and no stub stands in for one.

- **The real CLI-to-agent gRPC path** (`create`, `build`, the local launcher).
  [core#472](https://github.com/codefly-dev/core/issues/472) is still open:
  `Builder.LoadRequest` carries a `ServiceIdentity`, so an agent cannot receive
  a Runnable's identity and location without the CLI fabricating a service
  declaration, and `Builder.PackageResponse` has nowhere to carry the native
  command or the build evidence. This agent therefore advertises no BUILDER
  capability, and `pkg/pack` writes `runnable-build.json` — a versioned
  document holding exactly the core messages the seam will carry — instead of
  inventing a private RPC. [cli#638](https://github.com/codefly-dev/cli/issues/638)
  shipped discovery only for the same reason.
- **Orchestration** — registration/activation, TaskService, the native adapter,
  the Kubernetes adapter, recorded task results, recovery and reconnect.
  [module-runtime#63](https://github.com/obin-ai/module-runtime/issues/63) is
  an open spike. The Jobs above were submitted with `kubectl`, which issue #1
  states is debugging aid, not durable-path acceptance.
- **Durable-path acceptance**: typed I/O through TaskService → native adapter →
  launcher → harness; the same source through Orchestration's Kubernetes
  adapter; lost response and reconnect preserving task, release, invocation and
  deadline; registration Jobs observed separately from invocation Jobs;
  Kubernetes retries not multiplying with Orchestration attempts.
- **Dynamic installation, repeat registration, version coexistence and
  installed-versus-available observations**: these are CLI and Orchestration
  observations of a *released* agent. The release workflow is in place
  (`.github/workflows/releaser.yml`, `.goreleaser.yaml` producing
  `runnable-python_<version>_<os>_<arch>.tar.gz`), but no tag has been pushed,
  so no version is installable yet.
- **The protected installation handoff** for dependency, configuration and
  credential references. `pkg/pack` already keeps values out of everything it
  writes — the configuration digest covers names, coordinates and bounds only —
  but consuming resolved references at launch is the binding's half, and a
  binding is produced by the installer, not by this agent.

## Cleanup

The k3d cluster `runnable-proof` and the local image
`runnable-python-proof:0.1.0` were deleted; `k3d cluster list` and
`docker images` are clear of both. Nothing from this qualification is left
running.

## Resume point

1. Land [core#472](https://github.com/codefly-dev/core/issues/472). Its three
   gaps are load-with-runnable-identity, the build-evidence transfer, and the
   invocation framing. `docs/protocol.md` and `pkg/contract` are this agent's
   frozen proposal for the third, implemented and tested on both sides.
2. Implement `Builder` here against whatever core freezes: `Load` on a
   `RunnableIdentity`, `Create` calling `generate.Scaffold`/`generate.Generate`,
   `Build` returning the recipe from `pkg/recipe`, `Package` returning
   `pkg/prepare` + `pkg/pack` output. The implementation is already written and
   tested; only the RPC surface is missing.
3. Tag `v0.0.1` so `codefly agent install python:0.0.1 --kind=runnable`
   resolves a real release, then redo the observations above through the CLI.
4. Pick up the Orchestration adapters in
   [module-runtime#63](https://github.com/obin-ai/module-runtime/issues/63) and
   rerun the k3d Jobs through them, as invocation Jobs Orchestration owns.
