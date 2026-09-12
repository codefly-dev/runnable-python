# Codefly Runnable for Python

The Codefly language agent for typed, finite Python operations: generate a handler, prepare its dependencies, and produce identified native packages and container build recipes.

**Status: repository initialized; agent implementation and releases are not available yet.** [Implementation issue #1](https://github.com/codefly-dev/runnable-python/issues/1) tracks the first native and Kubernetes qualification.

## What belongs here

Python handler templates, generated input/output types, the invocation harness, interpreter and locked dependency preparation, native packaging, and Dockerfiles/build contexts. Generated user handlers belong in their owner workspace.

| Component | Responsibility |
|---|---|
| [Codefly core](https://github.com/codefly-dev/core) | Generic Runnable resource, shared protocol/types and package/binding verification |
| This repository | Python generation, harness, dependencies and build recipes |
| [Codefly CLI](https://github.com/codefly-dev/cli/issues/638) | Agent invocation over gRPC, commands, local process supervision and application image build/publish |
| [Orchestration](https://github.com/obin-ai/module-runtime/issues/63) | Installed releases, tasks, recorded invocations, progress and recovery |

The CLI receives language-specific build and launch facts through the shared contract. Python bootstrapping stays here. Agents emit image recipes; CLI executes and publishes application image builds.

## Contracts and first delivery

[Core PR #471](https://github.com/codefly-dev/core/pull/471) merged at `6a40c4bf28ac3dcebd534c32040349be96626605`. It introduces `runnable.codefly.yaml`, agent kind `codefly:runnable` (`Agent_RUNNABLE`), and the immutable `RunnablePackage` and `RunnableBinding` contracts. The agent name is `python`; distribution uses the `runnable-python` prefix.

[CLI #638](https://github.com/codefly-dev/cli/issues/638) records the shared handoff still to establish: Runnable loading over gRPC, transfer of the native command and build evidence, and precise invocation/completion framing. These are implementation dependencies, not already working behavior.

The first proof generates a neutral Runnable in a separate workspace and exercises real typed I/O through Codefly and Orchestration, first natively and then through actual invocation Jobs in disposable k3d. It must also prove failure handling, immutable version selection, reconnect behavior and scoped cleanup. A direct handler command is useful for debugging but does not complete this proof.

[Core #470](https://github.com/codefly-dev/core/issues/470) remains the parent delivery specification. The [Go agent repository](https://github.com/codefly-dev/runnable-go) exists separately; Go implementation follows the Python/native/k3d milestone. Domain-module adoption, engine changes, infra-base and canonical handbook updates remain later work.

See [LICENSE](LICENSE) for the repository's licensing terms, matching the existing Codefly service agents.
