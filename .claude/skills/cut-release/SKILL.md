---
name: cut-release
description: Publish a runnable-python version — bump the agent manifest and every declaration pinning it, tag, and let GoReleaser build. Use when asked to cut, publish or bump a release, when a change must reach the CLI as an installed agent, or when a downloaded agent's asset name or version looks wrong.
---

# Cutting a runnable-python release

Use `codefly publish` with Runnable release support. It qualifies the bumped
candidate, lands the release PR, and tags the merged commit. Never hand-tag or
replace published assets. `.github/workflows/releaser.yml` also reruns both
suites and builds the four GoReleaser platforms on a new semantic tag.

`agent.codefly.yaml` is the release identity embedded by `main.go`. The real
gRPC test reads that manifest when selecting its newly built candidate, so a
release bump cannot leave the test requesting an older executable. The pure
generation and packaging fixtures use independent example release identities;
they do not have to change when this agent releases.

## Steps

1. Run `go test ./...` and, from `pkg/harness`,
   `uv run --with pytest --python 3.12 pytest -q`. Merge the reviewed source PR
   only after all checks pass.
2. In a clean checkout of green main, run `codefly publish --dry-run`, then
   `codefly publish`. Its `runnable-package` conformance must create and package
   through the built agent; never waive it with `--skip-conformance`.
3. Watch `GoReleaser` and verify every archive against its checksums. Qualify
   the downloaded native binary through the real consumer lifecycle before
   updating a consumer selection.

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
