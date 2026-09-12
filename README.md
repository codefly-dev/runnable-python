# Codefly Runnable for Python

The Codefly language agent for typed, finite Python operations: generate a handler, prepare its dependencies, and produce identified native packages and container build recipes.

**Status: generation, harness, preparation and packaging are implemented and qualified natively, in a Linux image and in disposable k3d; no version is released yet, and the CLI-to-agent gRPC surface waits on [core #472](https://github.com/codefly-dev/core/issues/472).** [docs/qualification.md](docs/qualification.md) records exactly what was proven, what was not, and where to resume; [Implementation issue #1](https://github.com/codefly-dev/runnable-python/issues/1) tracks the milestone.

## What belongs here

Python handler templates, generated input/output types, the invocation harness, interpreter and locked dependency preparation, native packaging, and Dockerfiles/build contexts. Generated user handlers belong in their owner workspace.

| Package | What it owns |
|---|---|
| `pkg/contract` | the `codefly.runnable/v1` framing, in Go ([docs/protocol.md](docs/protocol.md)) |
| `pkg/harness` | the Python harness generated into every runnable: the same framing, and the bounded schema profile it enforces |
| `pkg/generate` | the handler scaffold, the typed bindings of a contract, the generated contract |
| `pkg/prepare` | uv-locked dependencies and the pinned interpreter |
| `pkg/pack` | the native package and the build evidence that identifies it |
| `pkg/recipe` | the Linux image recipe and build context the CLI's executor builds |

| Component | Responsibility |
|---|---|
| [Codefly core](https://github.com/codefly-dev/core) | Generic Runnable resource, shared protocol/types and package/binding verification |
| This repository | Python generation, harness, dependencies and build recipes |
| [Codefly CLI](https://github.com/codefly-dev/cli/issues/638) | Agent invocation over gRPC, commands, local process supervision and application image build/publish |
| [Orchestration](https://github.com/obin-ai/module-runtime/issues/63) | Installed releases, tasks, recorded invocations, progress and recovery |

The CLI receives language-specific build and launch facts through the shared contract. Python bootstrapping stays here. Agents emit image recipes; CLI executes and publishes application image builds.

## Contracts and first delivery

[Core PR #471](https://github.com/codefly-dev/core/pull/471) merged at `6a40c4bf28ac3dcebd534c32040349be96626605`. It introduces `runnable.codefly.yaml`, agent kind `codefly:runnable` (`Agent_RUNNABLE`), and the immutable `RunnablePackage` and `RunnableBinding` contracts. The agent name is `python`; distribution uses the `runnable-python` prefix.

[Core #472](https://github.com/codefly-dev/core/issues/472) is the shared handoff still to establish, raised out of [CLI #638](https://github.com/codefly-dev/cli/issues/638): Runnable loading over gRPC, transfer of the native command and build evidence, and precise invocation/completion framing. Until it lands this agent advertises no builder capability, `pkg/pack` writes its evidence as a versioned document the CLI reads, and [docs/protocol.md](docs/protocol.md) is this agent's implemented proposal for the framing.

The first proof generates a neutral Runnable in a separate workspace and exercises real typed I/O, first natively and then through actual invocation Jobs in disposable k3d. The agent's half is done and recorded in [docs/qualification.md](docs/qualification.md); the durable path through Codefly and Orchestration — and with it immutable version selection and reconnect behavior — waits on core #472 and [Orchestration #63](https://github.com/obin-ai/module-runtime/issues/63).

[Core #470](https://github.com/codefly-dev/core/issues/470) remains the parent delivery specification. The [Go agent repository](https://github.com/codefly-dev/runnable-go) exists separately; Go implementation follows the Python/native/k3d milestone. Domain-module adoption, engine changes, infra-base and canonical handbook updates remain later work.

See [LICENSE](LICENSE) for the repository's licensing terms, matching the existing Codefly service agents.
