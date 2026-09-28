package main_test

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
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

	"github.com/codefly-dev/runnable-python/pkg/generate"
	"github.com/codefly-dev/runnable-python/pkg/pack"
	"github.com/codefly-dev/runnable-python/pkg/prepare"
	"github.com/codefly-dev/runnable-python/pkg/recipe"
)

// The per-call facts a caller presents. The Work Context is carried verbatim
// and never parsed, here or in the harness.
const (
	invocationID = "inv-1"
	workContext  = "eyJ0eXAiOiJjb2RlZmx5LndvcmstY29udGV4dC92MSJ9.signed"
)

const declaration = `kind: runnable
name: word-count
description: Count the words of a text.
version: 0.1.0
agent:
  kind: codefly:runnable
  name: python
  version: 0.0.2
  publisher: codefly.dev
contract:
  protocol: codefly.runnable.served/v1
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
  facilities: [generated-service, kubernetes]
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
	if artifact["kind"] != "ARCHIVE" {
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

	harness := serve(t, installed, command(t, artifact), pkg)

	empty := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{"text": "one two three"})
	if got := count(t, empty); got != 3 {
		t.Fatalf("count = %d, want 3\n%s", got, harness.stderr.String())
	}
	if !corerunnable.ServedOutcomeIsCertain(empty.GetOutcome()) {
		t.Fatal("a validated answer must prove the effect committed")
	}

	// The package serves: it does not exit after one call. Every call below is
	// answered by the same process, which is the whole difference from the
	// placement this replaced.
	filtered := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{
		"text":    "one two three",
		"options": map[string]any{"stop_words": []string{"two"}},
	})
	if got := count(t, filtered); got != 2 {
		t.Fatalf("filtered count = %d, want 2", got)
	}

	nulled := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{
		"text":    "one two three four",
		"options": map[string]any{"stop_words": nil},
	})
	if got := count(t, nulled); got != 4 {
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
	harness := serve(t, prepared.Root, prepared.Command, packageFromEvidence(t, runnable, evidence))

	completion := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{"text": 3})

	if completion.GetOutcome() == basev0.RunnableServedOutcome_SERVED_SUCCEEDED {
		t.Fatalf("a payload the contract refuses was accepted: %s", completion.GetResult().GetOutput())
	}
	// A refused payload never reached the handler, so nothing committed — but
	// "proven not committed" is the operation asserting it in its own
	// vocabulary, and a refused request is not the operation speaking.
	if completion.GetFailureCode() != "" {
		t.Fatalf("a refused payload is not the operation's own failure: %q", completion.GetFailureCode())
	}
	if corerunnable.ServedOutcomeIsCertain(completion.GetOutcome()) {
		t.Fatalf("a refused payload leaves the outcome unproven, got %s", completion.GetOutcome())
	}

	// The harness answers again afterwards: a refusal ends a call, not the
	// process that serves them.
	second := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{"text": "one two"})
	if second.GetOutcome() != basev0.RunnableServedOutcome_SERVED_SUCCEEDED {
		t.Fatalf("the call after a refused one: %s %q\n--- stderr ---\n%s", second.GetOutcome(), second.GetMessage(), harness.stderr.String())
	}
}

// TestAnInstalledPackageRefusesACallWithNoIdentity proves the identity slot in
// the package a placement actually runs, not only in the harness suite. A call
// with no Work Context is refused rather than run under whatever identity the
// process happens to have, which is the state the required slot removes — and
// the handler's own declared failure is the one answer that proves no effect
// committed.
func TestAnInstalledPackageRefusesACallWithNoIdentity(t *testing.T) {
	ctx := context.Background()
	workspace := t.TempDir()
	source := filepath.Join(workspace, "runnables", "word-count")

	runnable := declare(t, ctx, source)
	require.NoError(t, generate.Scaffold(runnable, source))
	require.NoError(t, generate.Generate(runnable, source))
	write(t, filepath.Join(source, "handler.py"), `from codefly_runnable import Context, HandlerFailure


def handle(context: Context, input) -> dict:
    if input["text"] == "declined":
        raise HandlerFailure("card_declined", "the issuer declined")
    if input["text"] == "untyped":
        raise RuntimeError("something went wrong")
    return {"count": len(context.invocation.work_context.split())}
`)

	prepared, err := prepare.Prepare(ctx, runnable, source, filepath.Join(workspace, "build", "native"))
	require.NoError(t, err)
	evidence, err := pack.Native(runnable, runnable.Agent, prepared, filepath.Join(workspace, "artifact.tar.gz"))
	require.NoError(t, err)
	harness := serve(t, prepared.Root, prepared.Command, packageFromEvidence(t, runnable, evidence))

	// The handler sees the caller's capability verbatim: forwarding it is the
	// only thing a handler may do with one, and the harness neither parses nor
	// reconstructs it.
	carried := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{"text": "a"})
	require.Equal(t, len(strings.Fields(workContext)), count(t, carried))

	none := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{"text": "a"},
		map[string]string{corerunnable.WorkContextHeader: ""})
	require.NotEqual(t, basev0.RunnableServedOutcome_SERVED_SUCCEEDED, none.GetOutcome())
	require.False(t, corerunnable.ServedOutcomeIsCertain(none.GetOutcome()),
		"a refused call is not the operation asserting anything")
	require.Contains(t, harness.stderr.String(), "Work Context")

	declined := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{"text": "declined"})
	require.Equal(t, basev0.RunnableServedOutcome_SERVED_OWNER_FAILED, declined.GetOutcome())
	require.Equal(t, "card_declined", declined.GetFailureCode())
	require.True(t, corerunnable.ServedOutcomeIsCertain(declined.GetOutcome()),
		"the operation's own typed code proves no effect committed")

	untyped := harness.call(t, corerunnable.ServedInvokeProcedure, map[string]any{"text": "untyped"})
	require.Empty(t, untyped.GetFailureCode(), "an untyped error asserts nothing on the handler's behalf")
	require.False(t, corerunnable.ServedOutcomeIsCertain(untyped.GetOutcome()))
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

// served is a generated package running the way a placement runs it: the
// allocated address in the environment, and nothing else.
type served struct {
	address string
	process *exec.Cmd
	stderr  *strings.Builder
	pkg     *basev0.RunnablePackage
}

// serve starts the installed package and waits until it answers. The port is
// chosen by the operating system and handed over the way a placement hands one
// over: nothing here picks a number.
func serve(t *testing.T, root string, argv []string, pkg *basev0.RunnablePackage) served {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	address := listener.Addr().String()
	require.NoError(t, listener.Close())

	stderr := &strings.Builder{}
	process := exec.Command(filepath.Join(root, argv[0]), argv[1:]...)
	process.Dir = root
	process.Env = append(os.Environ(), corerunnable.ListenAddressEnv+"="+address)
	process.Stderr = stderr
	require.NoError(t, process.Start())
	t.Cleanup(func() {
		_ = process.Process.Signal(os.Interrupt)
		done := make(chan struct{})
		go func() { _, _ = process.Process.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(45 * time.Second):
			_ = process.Process.Kill()
		}
	})

	for range 600 {
		if process.ProcessState != nil {
			break
		}
		connection, err := net.DialTimeout("tcp", address, 50*time.Millisecond)
		if err == nil {
			require.NoError(t, connection.Close())
			return served{address: address, process: process, stderr: stderr, pkg: pkg}
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("the package never answered on %s\n--- stderr ---\n%s", address, stderr.String())
	return served{}
}

// call sends one call the way the durable-work runtime's invoker sends one —
// core's header spellings, core's procedure, the bounded input document as the
// whole body — and classifies the answer with core's own ClassifyServed. Every
// constant is read from core, so a harness that drifted from the contract fails
// here rather than in a live composition.
func (s served) call(t *testing.T, procedure string, payload map[string]any, headers ...map[string]string) *basev0.RunnableServedCompletion {
	t.Helper()
	body, err := json.Marshal(payload)
	require.NoError(t, err)
	inv := s.invocation(t, body)

	request, err := http.NewRequest(http.MethodPost, "http://"+s.address+procedure, bytes.NewReader(body))
	require.NoError(t, err)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set(corerunnable.WorkContextHeader, workContext)
	request.Header.Set(corerunnable.EffectHeader, inv.GetEffectId())
	request.Header.Set(corerunnable.DeadlineHeader, inv.GetDeadline().AsTime().Format(corerunnable.DeadlineFormat))
	for _, overrides := range headers {
		for name, value := range overrides {
			if value == "" {
				request.Header.Del(name)
				continue
			}
			request.Header.Set(name, value)
		}
	}

	observed := corerunnable.Call{CalledAt: time.Now().UTC(), AuthorityResolved: true}
	response, err := (&http.Client{Timeout: 2 * time.Minute}).Do(request)
	observed.AnsweredAt = time.Now().UTC()
	if err != nil {
		observed.Trouble = err
	} else {
		defer func() { require.NoError(t, response.Body.Close()) }()
		observed.Answered = true
		observed.FailureCode = response.Header.Get(corerunnable.FailureCodeHeader)
		answered, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		require.NoError(t, err)
		observed.Response = s.resultOf(t, response.StatusCode, answered)
		if response.StatusCode != http.StatusOK && observed.FailureCode == "" {
			observed.Trouble = fmt.Errorf("owner answered %d: %s", response.StatusCode, answered)
		}
	}
	completion, err := corerunnable.ClassifyServed(inv, s.pkg, observed)
	require.NoError(t, err)
	return completion
}

// resultOf turns the owner's answer into the RunnableResult core classifies. A
// 200 is the bounded output document; anything else answered no output at all,
// and a result invented for it would be a completion the owner never gave.
func (s served) resultOf(t *testing.T, status int, body []byte) []byte {
	t.Helper()
	if status != http.StatusOK {
		return nil
	}
	document, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(&basev0.RunnableResult{
		Protocol:     corerunnable.ServedProtocolV1,
		InvocationId: invocationID,
		Status:       basev0.RunnableResult_SUCCEEDED,
		Output:       body,
	})
	require.NoError(t, err)
	return document
}

func (s served) invocation(t *testing.T, input []byte) *basev0.RunnableInvocation {
	t.Helper()
	issued := time.Now().UTC()
	inv, err := corerunnable.PrepareInvocation(&basev0.RunnableInvocation{
		Protocol:     corerunnable.ServedProtocolV1,
		Runnable:     s.pkg.GetIdentity(),
		InvocationId: invocationID,
		IntentId:     "intent-1",
		EffectId:     "effect-1",
		IssuedAt:     timestamppb.New(issued),
		Deadline:     timestamppb.New(issued.Add(time.Minute)),
		Input:        input,
		Identity: &basev0.RunnableInvocationIdentity{
			Carrier: &basev0.RunnableInvocationIdentity_WorkContext{WorkContext: workContext},
		},
	}, s.pkg)
	require.NoError(t, err)
	return inv
}

// count reads the operation's own output out of a classified success.
func count(t *testing.T, completion *basev0.RunnableServedCompletion) int {
	t.Helper()
	require.Equal(t, basev0.RunnableServedOutcome_SERVED_SUCCEEDED, completion.GetOutcome(), completion.GetMessage())
	var output struct {
		Count int `json:"count"`
	}
	require.NoError(t, json.Unmarshal(completion.GetResult().GetOutput(), &output))
	return output.Count
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
