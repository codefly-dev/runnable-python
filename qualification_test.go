package main_test

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	basev0 "github.com/codefly-dev/core/generated/go/codefly/base/v0"
	"github.com/codefly-dev/core/resources"
	corerunnable "github.com/codefly-dev/core/runnable"
	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/codefly-dev/runnable-python/pkg/contract"
	"github.com/codefly-dev/runnable-python/pkg/generate"
	"github.com/codefly-dev/runnable-python/pkg/pack"
	"github.com/codefly-dev/runnable-python/pkg/prepare"
	"github.com/codefly-dev/runnable-python/pkg/recipe"
)

const declaration = `kind: runnable
name: word-count
description: Count the words of a text.
version: 0.1.0
agent:
  kind: codefly:runnable
  name: python
  version: 0.0.1
  publisher: codefly.dev
contract:
  protocol: codefly.runnable/v1
  input:
    fields:
      - name: text
        type: string
      - name: options
        type: object
        optional: true
        fields:
          - name: stop_words
            type: array
            nullable: true
            items:
              type: string
  output:
    fields:
      - name: count
        type: integer
entrypoint:
  handler: handler.py
  inputs: [pyproject.toml, uv.lock]
execution:
  facilities: [native, kubernetes]
  timeout: 2m
  cancellation: signal
  recovery: recompute
  payload:
    max-input-bytes: 65536
workspace-configuration-dependencies: [openai]
`

const implementation = `"""Count the words of a text."""

from codefly_runnable import Context

from codefly_types import Input, Output


def handle(context: Context, input: Input) -> Output:
    stop_words = set()
    options = input.get("options")
    if options is not None and options["stop_words"] is not None:
        stop_words = set(options["stop_words"])
    words = [word for word in input["text"].split() if word not in stop_words]
    return {"count": len(words)}
`

// TestANativePackageCompletesAnInvocation walks one release the way the CLI
// will: declare, generate, prepare, package, install elsewhere, invoke. The
// package is unpacked under a different path than it was built in, so a
// package that only ran from its build directory fails here.
func TestANativePackageCompletesAnInvocation(t *testing.T) {
	ctx := context.Background()
	workspace := t.TempDir()
	builderPython := filepath.Join(workspace, "builder-python")
	t.Setenv("UV_PYTHON_INSTALL_DIR", builderPython)
	t.Setenv("UV_PYTHON_BIN_DIR", filepath.Join(workspace, "builder-bin"))

	source := filepath.Join(workspace, "runnables", "word-count")

	runnable := declare(t, ctx, source)
	if err := generate.Scaffold(runnable, source); err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	if err := generate.Generate(runnable, source); err != nil {
		t.Fatalf("generate: %v", err)
	}
	// Verify a locked third-party dependency is carried into the installed package.
	project := filepath.Join(source, generate.ProjectFile)
	write(t, project, strings.Replace(read(t, project), "dependencies = []", "dependencies = [\"idna==3.10\"]", 1))
	write(t, filepath.Join(source, "handler.py"), "import idna\nassert idna.__version__ == \"3.10\"\n"+implementation)

	prepared, err := prepare.Prepare(ctx, runnable, source, filepath.Join(workspace, "build", "native"))
	if err != nil {
		t.Fatalf("prepare: %v", err)
	}
	if !strings.HasPrefix(prepared.Toolchain, "python-"+prepare.DefaultPythonVersion+".") {
		t.Fatalf("toolchain %q is not the declared interpreter", prepared.Toolchain)
	}

	// A source edit after preparation must not rewrite the archived build's evidence.
	write(t, filepath.Join(source, "handler.py"), "def handle(context, input):\n    return {\"count\": 999}\n")

	output := filepath.Join(workspace, "out")
	archive := filepath.Join(output, "word-count-0.1.0.tar.gz")
	evidence, err := pack.Native(runnable, runnable.Agent, prepared, archive)
	if err != nil {
		t.Fatalf("pack: %v", err)
	}
	if _, err := evidence.Write(output); err != nil {
		t.Fatalf("write evidence: %v", err)
	}

	artifact := artifacts(t, evidence)[0]
	pkg := packageFromEvidence(t, runnable, evidence)
	if artifact["kind"] != "NATIVE" {
		t.Fatalf("artifact kind is %v", artifact["kind"])
	}
	installed := filepath.Join(workspace, "installed", "word-count")
	unpack(t, archive, installed)
	// Remove access to both the prepared tree and the builder's interpreter.
	// The installed command must use only its own archive contents.
	if err := os.RemoveAll(prepared.Root); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(builderPython, builderPython+"-unavailable"); err != nil {
		t.Fatal(err)
	}
	archivedHandler, err := os.ReadFile(filepath.Join(installed, prepare.SourceDirectory, "handler.py"))
	if err != nil {
		t.Fatal(err)
	}
	var build struct {
		Handler struct {
			Digest string `json:"digest"`
		} `json:"handler"`
	}
	if err := json.Unmarshal(evidence.Build, &build); err != nil {
		t.Fatal(err)
	}
	if want := fmt.Sprintf("sha256:%x", sha256.Sum256(archivedHandler)); build.Handler.Digest != want {
		t.Fatalf("build evidence describes a different handler: got %s, archive %s", build.Handler.Digest, want)
	}

	empty := invoke(t, installed, command(t, artifact), pkg, map[string]any{"text": "one two three"})
	if empty.outcome() != "SUCCEEDED" {
		t.Fatalf("outcome %q: %v %s", empty.outcome(), empty.completion["error"], empty.stderr)
	}
	if got := empty.count(t); got != 3 {
		t.Fatalf("count = %d, want 3", got)
	}

	filtered := invoke(t, installed, command(t, artifact), pkg, map[string]any{
		"text":    "one two three",
		"options": map[string]any{"stop_words": []string{"two"}},
	})
	if got := filtered.count(t); got != 2 {
		t.Fatalf("filtered count = %d, want 2", got)
	}

	nulled := invoke(t, installed, command(t, artifact), pkg, map[string]any{
		"text":    "one two three four",
		"options": map[string]any{"stop_words": nil},
	})
	if got := nulled.count(t); got != 4 {
		t.Fatalf("nullable count = %d, want 4", got)
	}
}

// TestAnInstalledPackageRefusesAnInvalidPayload proves the harness enforces
// the contract in the package the launcher actually runs, not only in the
// repository's own tests.
func TestAnInstalledPackageRefusesAnInvalidPayload(t *testing.T) {
	ctx := context.Background()
	workspace := t.TempDir()
	source := filepath.Join(workspace, "runnables", "word-count")

	runnable := declare(t, ctx, source)
	if err := generate.Scaffold(runnable, source); err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	if err := generate.Generate(runnable, source); err != nil {
		t.Fatalf("generate: %v", err)
	}
	write(t, filepath.Join(source, "handler.py"), implementation)

	prepared, err := prepare.Prepare(ctx, runnable, source, filepath.Join(workspace, "build", "native"))
	if err != nil {
		t.Fatalf("prepare: %v", err)
	}

	evidence, err := pack.Native(runnable, runnable.Agent, prepared, filepath.Join(workspace, "artifact.tar.gz"))
	require.NoError(t, err)
	result := invoke(t, prepared.Root, prepared.Command, packageFromEvidence(t, runnable, evidence), map[string]any{"text": 3})
	if result.exit != contract.ExitInvalidInput {
		t.Fatalf("exit = %d, want %d (%s)", result.exit, contract.ExitInvalidInput, result.stderr)
	}
	if result.outcome() != "CRASHED" {
		t.Fatalf("outcome = %q", result.outcome())
	}
	if _, recorded := result.completion["output"]; recorded {
		t.Fatal("a refused invocation recorded an output")
	}
}

// TestTheImageRecipeDescribesALinuxBuild checks the recipe the CLI's image
// executor consumes; building the image is the CLI's half.
func TestTheImageRecipeDescribesALinuxBuild(t *testing.T) {
	ctx := context.Background()
	workspace := t.TempDir()
	source := filepath.Join(workspace, "runnables", "word-count")

	runnable := declare(t, ctx, source)
	if err := generate.Scaffold(runnable, source); err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	if err := generate.Generate(runnable, source); err != nil {
		t.Fatalf("generate: %v", err)
	}
	prepared, err := prepare.Prepare(ctx, runnable, source, filepath.Join(workspace, "build", "native"))
	if err != nil {
		t.Fatalf("prepare: %v", err)
	}

	image := filepath.Join(workspace, "build", "image")
	written, err := recipe.Write(runnable, prepared, image)
	if err != nil {
		t.Fatalf("recipe: %v", err)
	}
	if written.Schema != recipe.Schema {
		t.Fatalf("schema = %q", written.Schema)
	}
	for _, expected := range []string{
		filepath.Join(image, recipe.DockerfileName),
		filepath.Join(image, recipe.RecipeFile),
		filepath.Join(image, prepare.RequirementsFile),
		filepath.Join(image, prepare.EntryFile),
		filepath.Join(image, prepare.SourceDirectory, "handler.py"),
		filepath.Join(image, prepare.SourceDirectory, generate.GeneratedDirectory, generate.ContractFile),
	} {
		if _, err := os.Stat(expected); err != nil {
			t.Errorf("image context is missing %s", filepath.Base(expected))
		}
	}
	if _, err := os.Stat(filepath.Join(image, prepare.Environment)); !os.IsNotExist(err) {
		t.Error("the image context carries the build host's interpreter")
	}
	dockerfile := read(t, filepath.Join(image, recipe.DockerfileName))
	if !strings.Contains(dockerfile, "--require-hashes") {
		t.Error("the image resolves dependencies instead of installing the locked set")
	}
}

func declare(t *testing.T, ctx context.Context, dir string) *resources.Runnable {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(dir, resources.RunnableConfigurationName), declaration)
	runnable, err := resources.LoadRunnableFromDir(ctx, dir)
	if err != nil {
		t.Fatalf("load declaration: %v", err)
	}
	return runnable
}

type invocation struct {
	exit       int
	stdout     string
	stderr     string
	completion map[string]any
	classified *basev0.RunnableCompletion
}

func (i invocation) outcome() string {
	return i.classified.GetOutcome().String()
}

func (i invocation) count(t *testing.T) int {
	t.Helper()
	output, ok := i.completion["output"].(map[string]any)
	if !ok {
		t.Fatalf("completion carries no output: %v", i.completion)
	}
	count, ok := output["count"].(float64)
	if !ok {
		t.Fatalf("output.count is %v", output["count"])
	}
	return int(count)
}

// invoke runs one invocation the way a launcher does: a request document in, a
// completion document out, logs on the process streams.
func invoke(t *testing.T, root string, argv []string, pkg *basev0.RunnablePackage, payload map[string]any) invocation {
	t.Helper()
	requestPath := filepath.Join(t.TempDir(), "request.json")
	resultPath := filepath.Join(t.TempDir(), "result.json")
	input, err := json.Marshal(payload)
	require.NoError(t, err)
	started := time.Now()
	inv, err := corerunnable.PrepareInvocation(&basev0.RunnableInvocation{
		Protocol: corerunnable.ProtocolV1, Runnable: pkg.GetIdentity(), InvocationId: "inv-1", IntentId: "intent-1",
		IssuedAt: timestamppb.New(started), Deadline: timestamppb.New(started.Add(time.Minute)), Input: input,
	}, pkg)
	require.NoError(t, err)
	document, err := corerunnable.EncodeInvocation(inv)
	require.NoError(t, err)
	write(t, requestPath, string(document))
	ctx, cancel := context.WithDeadline(t.Context(), inv.GetDeadline().AsTime())
	defer cancel()
	process := exec.CommandContext(ctx, filepath.Join(root, argv[0]), argv[1:]...)
	process.Dir = root
	process.Env = os.Environ()
	for key, value := range corerunnable.InvocationEnvironment(inv, requestPath, resultPath) {
		process.Env = append(process.Env, key+"="+value)
	}
	var stdout, stderr strings.Builder
	process.Stdout, process.Stderr = &stdout, &stderr
	runErr := process.Run()
	require.NotNil(t, process.ProcessState, "start process: %v", runErr)
	result := invocation{exit: process.ProcessState.ExitCode(), stdout: stdout.String(), stderr: stderr.String()}
	recorded, err := os.ReadFile(resultPath)
	if err != nil {
		require.True(t, os.IsNotExist(err), "%v", err)
	}
	observed := corerunnable.Observation{Result: recorded, ExitCode: int32(result.exit), StartedAt: started, EndedAt: time.Now()}
	result.classified, err = corerunnable.Complete(inv, pkg, observed)
	require.NoError(t, err)
	if recorded != nil {
		parsed, err := corerunnable.ParseResult(recorded, inv, pkg)
		require.NoError(t, err, string(recorded))
		result.completion = map[string]any{}
		if parsed.GetStatus() == basev0.RunnableResult_SUCCEEDED {
			var output map[string]any
			require.NoError(t, json.Unmarshal(parsed.GetOutput(), &output))
			result.completion["output"] = output
		}
	}
	return result
}

func packageFromEvidence(t *testing.T, r *resources.Runnable, evidence *pack.Evidence) *basev0.RunnablePackage {
	t.Helper()
	build := &basev0.RunnableBuild{}
	require.NoError(t, protojson.Unmarshal(evidence.Build, build))
	var documents []json.RawMessage
	require.NoError(t, json.Unmarshal(evidence.Artifacts, &documents))
	var artifacts []*basev0.RunnableArtifact
	for _, document := range documents {
		artifact := &basev0.RunnableArtifact{}
		require.NoError(t, protojson.Unmarshal(document, artifact))
		artifacts = append(artifacts, artifact)
	}
	return preparedPackage(t, r, &basev0.RunnableIdentity{Name: r.Name, Module: "text", Workspace: "proof", Version: r.Version}, build, artifacts)
}

func preparedPackage(t *testing.T, r *resources.Runnable, identity *basev0.RunnableIdentity, build *basev0.RunnableBuild, artifacts []*basev0.RunnableArtifact) *basev0.RunnablePackage {
	t.Helper()
	declaration, err := r.Proto(t.Context())
	require.NoError(t, err)
	pkg, err := corerunnable.PreparePackage(&basev0.RunnablePackage{
		Schema: corerunnable.PackageSchemaV1, Identity: identity, Agent: declaration.GetAgent(),
		Contract: declaration.GetContract(), Execution: declaration.GetExecution(), Build: build, Artifacts: artifacts,
		ServiceDependencies: declaration.GetServiceDependencies(), WorkspaceConfigurationDependencies: declaration.GetWorkspaceConfigurationDependencies(),
	})
	require.NoError(t, err)
	return pkg
}

func artifacts(t *testing.T, evidence *pack.Evidence) []map[string]any {
	t.Helper()
	var recorded []map[string]any
	if err := json.Unmarshal(evidence.Artifacts, &recorded); err != nil {
		t.Fatalf("artifacts: %v", err)
	}
	if len(recorded) == 0 {
		t.Fatal("the evidence records no artifact")
	}
	return recorded
}

func command(t *testing.T, artifact map[string]any) []string {
	t.Helper()
	raw, ok := artifact["command"].([]any)
	if !ok || len(raw) == 0 {
		t.Fatalf("the native artifact carries no launch command: %v", artifact)
	}
	argv := make([]string, 0, len(raw))
	for _, part := range raw {
		argv = append(argv, fmt.Sprint(part))
	}
	return argv
}

func unpack(t *testing.T, archive string, target string) {
	t.Helper()
	file, err := os.Open(archive)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	decompressed, err := gzip.NewReader(file)
	if err != nil {
		t.Fatal(err)
	}
	reader := tar.NewReader(decompressed)
	for {
		header, err := reader.Next()
		if err == io.EOF {
			return
		}
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(target, header.Name)
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(path, 0o755); err != nil {
				t.Fatal(err)
			}
		case tar.TypeSymlink:
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(header.Linkname, path); err != nil {
				t.Fatal(err)
			}
		default:
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			content, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, header.FileInfo().Mode())
			if err != nil {
				t.Fatal(err)
			}
			if _, err := io.Copy(content, reader); err != nil {
				t.Fatal(err)
			}
			content.Close()
		}
	}
}

func write(t *testing.T, path string, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func read(t *testing.T, path string) string {
	t.Helper()
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(content)
}
