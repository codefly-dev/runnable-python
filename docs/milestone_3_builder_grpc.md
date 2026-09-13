# Milestone 3 — native Builder over gRPC

The Python agent can now receive a real `RunnableLocation`, scaffold author code,
prepare its locked dependencies and bundled interpreter, and return a native
archive with a launch command through the shared Builder protocol. The CLI can
assemble the package without importing Python implementation code.

This checkpoint consumes core PR #474 at
`b3470f0096cd818d6a90c67dc31e9ad09824d822`. The integration branch is
`feat/runnable-builder-grpc`, based on merged PR #2 (`0a879b0`). No release tag is
published by this work. Core #474 must settle before coordinated fleet release.

## Implementation

- [Builder](../pkg/agent/builder.go): `Load` checks the workspace-resolved release,
  location and pinned agent; `Create` scaffolds; `RunnableBuildInputs` prepares a
  snapshot and returns measured `RunnableBuild`; `Package` returns native archive
  metadata and `PackageArtifact.command`.
- [Generation](../pkg/generate/generate.go): embeds the resolved release and its
  declared bounds beside the harness. Python tooling remains in this repository.
- [Harness protocol](protocol.md): core's three environment variables and proto3
  JSON documents, base64 input/output, explicit `HandlerFailure`, atomic result.
- [gRPC regression](../builder_grpc_test.go): builds and starts the real agent,
  verifies Builder capability, creates and packages over gRPC, edits live source,
  relocates and invokes the prepared archive. It also checks invalid target,
  source/symlink overlap, snapshot tampering, failed-Load state clearing, and
  that preparing again into an occupied output directory is refused without
  discarding the snapshot already prepared.
- [Native qualification](../qualification_test.go): core encodes the invocation
  and validates/classifies the real Python result; locked third-party dependencies
  and the bundled interpreter survive removal of the preparation environment.

There are no service-shaped Runnable declarations, language-specific CLI imports,
mock agents or duplicate core protocol definitions. Agent-private evidence remains
an internal packaging representation, not a CLI file API.

## Validation

```sh
GOWORK=off go test ./...
GOWORK=off go vet ./...
GOWORK=off go test -race ./...
uv run --with pytest --python 3.12 pytest -q pkg/harness/tests
```

The native Go and gRPC tests pass on macOS/arm64. The harness has 61 passing real
process/schema cases. The CLI companion integration creates through the actual
CLI, packages through this agent, removes source/prepared files, and proves:

| Input | Observed result |
| --- | --- |
| `{"text":"one two three"}` | `SUCCEEDED`, `{"count":3}` |
| `{"text":"four five"}` | `SUCCEEDED`, `{"count":2}` |
| `{"text":42}` | exit 64, no result; core classifies `CRASHED`, uncertain |

An unexpected exception is never converted into a certain typed failure.
Input/output schema errors and interruption leave no result; the launcher owns
completion classification. Standalone test invocation is a debugging proof,
not a production supervisor or durable task.

## Limits and next action

Native packaging supports the host OS/architecture only. Builder image build,
SBOM, replacement release subjects and internal library preparation return
unsupported. Docker execution is not advertised. No cluster, installation,
active binding, Task or external domain effect is created by these tests.
Temporary agent processes are closed by test cleanup.

Finish core/CLI/agent CI together, then implement the CLI's generic native
supervisor and Orchestration's installed-binding invocation. Qualify deadline,
cancellation, recovery and reconnect behavior before repeating through k3d.
Reconcile cross-host clock policy first: this native harness preserves the
original absolute deadline; core #474 currently describes clock-offset budgeting.
Go agent execution, domain adoption, infrastructure and handbook changes remain
later checkpoints. Earlier milestone records are historical, not current framing.
