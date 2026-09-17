---
name: cut-release
description: Publish a runnable-python version — bump the agent manifest and every declaration pinning it, tag, and let GoReleaser build. Use when asked to cut, publish or bump a release, when a change must reach the CLI as an installed agent, or when a downloaded agent's asset name or version looks wrong.
---

# Cutting a runnable-python release

A release is a tag. `.github/workflows/releaser.yml` fires on `v*`, re-runs both
suites, then runs GoReleaser; nothing is built by hand, and the tag is the only
trigger.

## The version lives in more than one file

`agent.codefly.yaml` is the source — `main.go` embeds it, and the agent
advertises that identity. But a declaration names the agent it wants, so every
test declaration pins the same version. Bump them together:

| File | Form |
| --- | --- |
| `agent.codefly.yaml` | `version: X.Y.Z` |
| `qualification_test.go` | `agent:` block of the declaration |
| `pkg/pack/pack_test.go` | same |
| `pkg/generate/generate_test.go` | same |
| `builder_grpc_test.go` | `codefly.dev/python:X.Y.Z` passed to `ParseAgent` |
| `README.md` | the status line naming the agent version |

Bumping the manifest alone is not a silent drift: `Load` rejects it with
`declaration pins a different Runnable agent`, and the gRPC builder test fails.
That is the check — run `go test ./...` after the bump, before tagging.

## Steps

1. Bump every file above, in one commit, on a branch. `go test ./...` and
   `cd pkg/harness && uv run --with pytest --python 3.12 pytest -q` both pass.
2. Merge the PR.
3. Tag the merged commit `vX.Y.Z` — the same `X.Y.Z` as the manifest — and push
   the tag. GoReleaser derives the asset version from the tag, so a tag that
   disagrees with the manifest publishes assets the CLI will not find.
4. Watch the `GoReleaser` workflow. Its `test` job gates `release`; a red suite
   means no assets, not a partial release.

## What the asset names have to be

Core's downloader asks for `runnable-{name}_{version}_{os}_{arch}.tar.gz`
holding a binary named `runnable-{name}`. `.goreleaser.yaml` satisfies this
through `project_name: runnable-python` plus the archive `name_template`. Do not
rename the project, the binary or the template to something tidier — that
contract is the reason an installed agent resolves at all.

Builds are `CGO_ENABLED=0` for darwin/linux × amd64/arm64: this agent generates
and packages Python and never inspects source with core's tree-sitter grammars,
so it cross-compiles. If something here starts needing cgo, that is a design
question, not a flag to flip.

## If a released agent misbehaves in the CLI

Check identity before behaviour: the version the CLI resolved, the asset it
downloaded, and the manifest inside it. A mismatch between manifest and tag
looks exactly like a stale agent.
