# Codefly Runnable for Python

The Codefly language agent for typed, finite Python operations: generate a handler, prepare its dependencies, and produce identified native packages and container build recipes.

**Status: native authoring and packaging are implemented through the real Builder gRPC lifecycle on core v0.3.29.** Agent 0.0.2 includes the shared invocation/result framing and signal-cancellation contract; published binaries appear on the [releases page](https://github.com/codefly-dev/runnable-python/releases). [Milestone 3](docs/milestone_3_builder_grpc.md) records the Builder checkpoint; [issue #1](https://github.com/codefly-dev/runnable-python/issues/1) still tracks durable native and Kubernetes execution.

The review corrections and current validation are recorded in [milestone 2](docs/milestone_2_review_fixes.md). Earlier image/k3d observations are historical and do not qualify the Codefly/Orchestration path.

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

Core owns `RunnableLocation`, `RunnableBuild`, `PackageArtifact.command` and the invocation/result protocol. This agent advertises Builder, implements `Load`, `Create`, `RunnableBuildInputs` and native `Package`, and supplies Python generation, uv preparation and the launch command. The CLI assembles and verifies the shared package descriptor; it does not read the agent-private `runnable-build.json` evidence file.

The Builder supports the current host's native target. Image recipe helpers remain available for qualification, but image building is not exposed by this Builder yet. Durable installation, activation, invocation, recovery and Kubernetes qualification remain [Orchestration #63](https://github.com/obin-ai/module-runtime/issues/63) and [CLI #638](https://github.com/codefly-dev/cli/issues/638).

[Core #470](https://github.com/codefly-dev/core/issues/470) remains the parent delivery specification. The [Go agent repository](https://github.com/codefly-dev/runnable-go) exists separately; Go implementation follows the Python/native/k3d milestone. Domain-module adoption, engine changes, infra-base and canonical handbook updates remain later work.

See [LICENSE](LICENSE) for the repository's licensing terms, matching the existing Codefly service agents.
