package pack_test

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/codefly-dev/core/resources"

	"github.com/codefly-dev/runnable-python/pkg/pack"
	"github.com/codefly-dev/runnable-python/pkg/prepare"
)

const declaration = `kind: runnable
name: charge-card
version: 0.2.0
agent:
  kind: codefly:runnable
  name: python
  version: 0.0.1
  publisher: codefly.dev
contract:
  protocol: codefly.runnable/v1
  input:
    fields:
      - name: amount
        type: integer
  output:
    fields:
      - name: receipt
        type: string
entrypoint:
  handler: handler.py
  inputs: [uv.lock, pyproject.toml]
execution:
  facilities: [native]
  timeout: 30s
  cancellation: signal
  recovery: receipt
workspace-configuration-dependencies: [stripe]
`

func TestTheBuildPinsEveryInputItWasBuiltFrom(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir, declaration)
	write(t, filepath.Join(dir, "handler.py"), "def handle(context, input):\n    return {}\n")
	write(t, filepath.Join(dir, "pyproject.toml"), "[project]\n")
	write(t, filepath.Join(dir, "uv.lock"), "version = 1\n")

	build, err := pack.Build(runnable, dir, "python-3.12.14")
	if err != nil {
		t.Fatal(err)
	}

	if build.GetHandler().GetPath() != "handler.py" {
		t.Errorf("handler path = %q", build.GetHandler().GetPath())
	}
	if got, want := build.GetHandler().GetDigest(), digestOf("def handle(context, input):\n    return {}\n"); got != want {
		t.Errorf("handler digest = %q, want %q", got, want)
	}
	if len(build.GetInputs()) != 2 {
		t.Fatalf("inputs = %v", build.GetInputs())
	}
	if build.GetInputs()[0].GetPath() != "pyproject.toml" || build.GetInputs()[1].GetPath() != "uv.lock" {
		t.Errorf("declared inputs were not sorted: %v", build.GetInputs())
	}
	if !strings.HasPrefix(build.GetHarnessDigest(), "sha256:") {
		t.Errorf("harness digest = %q", build.GetHarnessDigest())
	}
	if build.GetToolchain() != "python-3.12.14" {
		t.Errorf("toolchain = %q", build.GetToolchain())
	}
	if !strings.HasPrefix(build.GetConfigurationDigest(), "sha256:") {
		t.Errorf("configuration digest = %q", build.GetConfigurationDigest())
	}
}

func TestTheConfigurationDigestFollowsWhatAnInvocationIsBoundTo(t *testing.T) {
	base := digestFor(t, declaration)

	if described := digestFor(t, strings.Replace(declaration,
		"name: charge-card", "name: charge-card\ndescription: Charge a card.", 1)); described != base {
		t.Error("a documentation change moved the configuration digest")
	}
	if extra := digestFor(t, strings.Replace(declaration,
		"workspace-configuration-dependencies: [stripe]",
		"workspace-configuration-dependencies: [stripe, openai]", 1)); extra == base {
		t.Error("declaring another workspace configuration did not move the configuration digest")
	}
	if relaxed := digestFor(t, strings.Replace(declaration, "timeout: 30s", "timeout: 5m", 1)); relaxed == base {
		t.Error("a different execution bound did not move the configuration digest")
	}
}

func TestTheSamePreparedTreeAlwaysPackagesToTheSameDigest(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir, declaration)
	write(t, filepath.Join(dir, "handler.py"), "def handle(context, input):\n    return {}\n")
	write(t, filepath.Join(dir, "pyproject.toml"), "[project]\n")
	write(t, filepath.Join(dir, "uv.lock"), "version = 1\n")

	prepared := &prepare.Prepared{
		Root:      preparedTree(t),
		Toolchain: "python-3.12.14",
		Command:   []string{filepath.Join(prepare.Environment, "bin", "python"), prepare.EntryFile},
	}
	first, err := pack.Native(runnable, runnable.Agent, dir, prepared, filepath.Join(t.TempDir(), "a.tar.gz"))
	if err != nil {
		t.Fatal(err)
	}
	second, err := pack.Native(runnable, runnable.Agent, dir, prepared, filepath.Join(t.TempDir(), "b.tar.gz"))
	if err != nil {
		t.Fatal(err)
	}

	if artifactOf(t, first)["digest"] != artifactOf(t, second)["digest"] {
		t.Error("two packages of the same prepared tree have different artifact digests")
	}
	artifact := artifactOf(t, first)
	if artifact["platform"] == "" {
		t.Error("the native artifact declares no platform")
	}
	command, _ := artifact["command"].([]any)
	if len(command) != 2 || !strings.HasSuffix(fmt.Sprint(command[0]), "python") {
		t.Errorf("the native artifact carries no launch command: %v", artifact["command"])
	}
}

func TestTheEvidenceRecordsTheAgentThatBuiltIt(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir, declaration)
	write(t, filepath.Join(dir, "handler.py"), "")
	write(t, filepath.Join(dir, "pyproject.toml"), "")
	write(t, filepath.Join(dir, "uv.lock"), "")
	prepared := &prepare.Prepared{Root: preparedTree(t), Toolchain: "python-3.12.14", Command: []string{"x"}}

	evidence, err := pack.Native(runnable, runnable.Agent, dir, prepared, filepath.Join(t.TempDir(), "a.tar.gz"))
	if err != nil {
		t.Fatal(err)
	}
	output := t.TempDir()
	path, err := evidence.Write(output)
	if err != nil {
		t.Fatal(err)
	}

	var document map[string]any
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(content, &document); err != nil {
		t.Fatal(err)
	}
	if document["schema"] != pack.EvidenceSchema {
		t.Errorf("schema = %v", document["schema"])
	}
	agent, _ := document["agent"].(map[string]any)
	if agent["name"] != "python" || agent["version"] != "0.0.1" {
		t.Errorf("agent = %v", agent)
	}
	identity, _ := document["identity"].(map[string]any)
	if identity["name"] != "charge-card" || identity["version"] != "0.2.0" {
		t.Errorf("identity = %v", identity)
	}
}

func digestFor(t *testing.T, text string) string {
	t.Helper()
	digest, err := pack.ConfigurationDigest(load(t, t.TempDir(), text))
	if err != nil {
		t.Fatal(err)
	}
	return digest
}

func artifactOf(t *testing.T, evidence *pack.Evidence) map[string]any {
	t.Helper()
	var artifacts []map[string]any
	if err := json.Unmarshal(evidence.Artifacts, &artifacts); err != nil {
		t.Fatal(err)
	}
	if len(artifacts) != 1 {
		t.Fatalf("artifacts = %v", artifacts)
	}
	return artifacts[0]
}

// preparedTree stands in for a prepared runnable: packaging measures bytes, so
// it does not need a real interpreter to be exercised.
func preparedTree(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, prepare.SourceDirectory), 0o755); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(root, prepare.EntryFile), "import sys\n")
	write(t, filepath.Join(root, prepare.SourceDirectory, "handler.py"), "def handle(context, input):\n    return {}\n")
	return root
}

func load(t *testing.T, dir string, text string) *resources.Runnable {
	t.Helper()
	write(t, filepath.Join(dir, resources.RunnableConfigurationName), text)
	runnable, err := resources.LoadRunnableFromDir(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	return runnable
}

func digestOf(content string) string {
	return fmt.Sprintf("sha256:%x", sha256.Sum256([]byte(content)))
}

func write(t *testing.T, path string, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
