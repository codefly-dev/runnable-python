package generate_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/codefly-dev/core/resources"

	"github.com/codefly-dev/runnable-python/pkg/generate"
)

const declaration = `kind: runnable
name: word-count
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
          - name: limits
            type: array
            items:
              type: object
              fields:
                - name: max_words
                  type: integer
                - name: strict
                  type: boolean
  output:
    fields:
      - name: count
        type: integer
entrypoint:
  handler: handler.py
execution:
  facilities: [native]
  timeout: 1m
  cancellation: none
  recovery: recompute
  payload:
    max-input-bytes: 4096
    max-output-bytes: 2048
`

// Exercise the generated Python module and its runtime typing metadata.
func TestTypesRenderTheBoundedProfile(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir)
	if err := generate.Generate(runnable, dir); err != nil {
		t.Fatal(err)
	}
	command := exec.Command("uv", "run", "--no-project", "--python", "3.12", "python", "-c", `
from typing import get_type_hints, get_args, NotRequired
import codefly_types as t
assert t.Input.__required_keys__ == {"text"}
assert t.Input.__optional_keys__ == {"options"}
assert get_type_hints(t.Input)["text"] is str
assert get_type_hints(t.InputOptions)["stop_words"] == list[str] | None
assert get_type_hints(t.InputOptionsLimitsItem) == {"max_words": int, "strict": bool}
assert get_type_hints(t.Output) == {"count": int}
`)
	command.Dir = dir
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("generated Python types: %v\n%s", err, output)
	}
}

func TestAnEmptyContractStillGeneratesBothTypes(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, resources.RunnableConfigurationName),
		strings.Replace(declaration, declaration[strings.Index(declaration, "  input:"):strings.Index(declaration, "entrypoint:")],
			"  input:\n    fields: []\n  output:\n    fields: []\n", 1))
	runnable, err := resources.LoadRunnableFromDir(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}

	types := string(generate.Types(runnable))

	if !strings.Contains(types, "Input = TypedDict(\"Input\", {})") {
		t.Errorf("an explicitly empty input schema did not generate a type:\n%s", types)
	}
	if strings.Contains(types, "NotRequired") {
		t.Error("an empty contract imported NotRequired")
	}
}

func TestTheGeneratedContractCarriesTheDeclaredBounds(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir)

	if err := generate.Generate(runnable, dir); err != nil {
		t.Fatal(err)
	}

	var document map[string]any
	content, err := os.ReadFile(filepath.Join(dir, generate.GeneratedDirectory, generate.ContractFile))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(content, &document); err != nil {
		t.Fatal(err)
	}
	if document["max-input-bytes"] != 4096.0 || document["max-output-bytes"] != 2048.0 {
		t.Errorf("declared payload bounds were not generated: %v", document)
	}
	if document["recovery"] != "recompute" {
		t.Errorf("recovery = %v", document["recovery"])
	}
	handler, _ := document["handler"].(map[string]any)
	if handler["module"] != "handler" || handler["attribute"] != "handle" {
		t.Errorf("handler = %v", handler)
	}
	if _, err := os.Stat(filepath.Join(dir, generate.GeneratedDirectory, "codefly_runnable", "runner.py")); err != nil {
		t.Errorf("the harness was not generated: %v", err)
	}
}

func TestRegeneratingProducesTheSameBytes(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir)

	if err := generate.Generate(runnable, dir); err != nil {
		t.Fatal(err)
	}
	first := read(t, filepath.Join(dir, generate.GeneratedDirectory, generate.ContractFile))
	if err := generate.Generate(runnable, dir); err != nil {
		t.Fatal(err)
	}

	if second := read(t, filepath.Join(dir, generate.GeneratedDirectory, generate.ContractFile)); second != first {
		t.Error("regenerating an unchanged declaration changed the contract bytes")
	}
}

func TestScaffoldNeverOverwritesAnImplementation(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir)
	if err := generate.Scaffold(runnable, dir); err != nil {
		t.Fatal(err)
	}
	implementation := "def handle(context, input):\n    return {\"count\": 1}\n"
	write(t, filepath.Join(dir, "handler.py"), implementation)

	if err := generate.Scaffold(runnable, dir); err != nil {
		t.Fatal(err)
	}

	if got := read(t, filepath.Join(dir, "handler.py")); got != implementation {
		t.Errorf("the author's handler was replaced by:\n%s", got)
	}
}

func TestHandlerModule(t *testing.T) {
	for handler, expected := range map[string]string{
		"handler.py":          "handler",
		"src/word_count.py":   "src.word_count",
		"_private/handler.py": "_private.handler",
	} {
		module, err := generate.HandlerModule(handler)
		if err != nil {
			t.Errorf("%s: %v", handler, err)
			continue
		}
		if module != expected {
			t.Errorf("%s resolved to %q, want %q", handler, module, expected)
		}
	}
	for _, handler := range []string{"handler", "handler.txt", "my handler.py", "2handler.py", "a-b.py"} {
		if module, err := generate.HandlerModule(handler); err == nil {
			t.Errorf("%s resolved to %q instead of being refused", handler, module)
		}
	}
}

func TestConfineRefusesATargetOutsideTheRunnable(t *testing.T) {
	outside := t.TempDir()
	write(t, filepath.Join(outside, "secret.py"), "")
	dir := t.TempDir()
	if err := os.Symlink(filepath.Join(outside, "secret.py"), filepath.Join(dir, "handler.py")); err != nil {
		t.Fatal(err)
	}

	if _, err := generate.Confine(dir, "handler.py"); err == nil {
		t.Error("a symlink out of the runnable directory was accepted")
	}
	if _, err := generate.Confine(dir, "../escape.py"); err == nil {
		t.Error("a path above the runnable directory was accepted")
	}
	if _, err := generate.Confine(dir, "new.py"); err != nil {
		t.Errorf("a file that does not exist yet was refused: %v", err)
	}
}

func load(t *testing.T, dir string) *resources.Runnable {
	t.Helper()
	write(t, filepath.Join(dir, resources.RunnableConfigurationName), declaration)
	runnable, err := resources.LoadRunnableFromDir(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	return runnable
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

func TestTypesKeepKeywordAndCollidingWireNames(t *testing.T) {
	dir := t.TempDir()
	runnable := load(t, dir)
	runnable.Contract.Input.Fields = []*resources.RunnableField{
		{Name: "class", Type: resources.RunnableFieldString},
		{Name: "__private", Type: resources.RunnableFieldInteger},
		{Name: "a_b", Type: resources.RunnableFieldObject, Fields: []*resources.RunnableField{{Name: "text", Type: resources.RunnableFieldString}}},
		{Name: "aB", Type: resources.RunnableFieldObject, Fields: []*resources.RunnableField{{Name: "count", Type: resources.RunnableFieldInteger}}},
	}
	if err := runnable.Contract.Validate(); err != nil {
		t.Fatal(err)
	}
	if err := generate.Generate(runnable, dir); err != nil {
		t.Fatal(err)
	}
	command := exec.Command("uv", "run", "--no-project", "--python", "3.12", "python", "-c", `
from typing import get_type_hints
import codefly_types as t
fields = get_type_hints(t.Input)
assert fields["class"] is str
assert fields["__private"] is int
assert get_type_hints(fields["a_b"]) == {"text": str}
assert get_type_hints(fields["aB"]) == {"count": int}
`)
	command.Dir = dir
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("generated wire names: %v\n%s", err, output)
	}
}

func TestScaffoldConfinesMissingDescendantsOfSymlinks(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	runnable := load(t, dir)
	if err := os.Symlink(outside, filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}
	runnable.Entrypoint.Handler = "link/new/sub/handler.py"
	if err := generate.Scaffold(runnable, dir); err == nil {
		t.Fatal("scaffold accepted a path outside the runnable")
	}
	if _, err := os.Stat(filepath.Join(outside, "new")); !os.IsNotExist(err) {
		t.Fatalf("scaffold wrote outside its directory: %v", err)
	}
	runnable.Entrypoint.Handler = "new/sub/handler.py"
	if err := generate.Scaffold(runnable, dir); err != nil {
		t.Fatalf("confined new directories should work: %v", err)
	}
}
