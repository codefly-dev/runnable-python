// Package generate scaffolds a Python runnable from its declaration: the
// author's handler, the typed bindings of its contract, and the generated
// directory holding the harness and the contract it enforces.
package generate

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/codefly-dev/core/resources"

	"github.com/codefly-dev/runnable-python/pkg/contract"
	"github.com/codefly-dev/runnable-python/pkg/harness"
)

// Layout of what the agent owns inside a runnable directory.
const (
	// GeneratedDirectory holds everything the agent regenerates; nothing an
	// author edits lives here.
	GeneratedDirectory = ".codefly"
	ContractFile       = "contract.json"
	ProjectFile        = "pyproject.toml"
	LockFile           = "uv.lock"

	// PythonRequirement is the interpreter floor of generated code: the typed
	// bindings use typing.NotRequired.
	PythonRequirement = ">=3.11"
)

var identifier = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// Scaffold writes the author-owned files of a new runnable. It never
// overwrites: a handler is the author's, and regenerating a runnable must not
// silently discard an implementation.
func Scaffold(runnable *resources.Runnable, dir string) error {
	handler, err := Confine(dir, runnable.Entrypoint.Handler)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(handler), 0o755); err != nil {
		return err
	}
	if err := writeIfAbsent(handler, handlerTemplate(runnable)); err != nil {
		return err
	}
	return writeIfAbsent(filepath.Join(dir, ProjectFile), projectTemplate(runnable))
}

// Generate writes the agent-owned files: the typed bindings, the contract the
// harness enforces and the harness itself. Regenerating an unchanged
// declaration produces the same bytes.
func Generate(runnable *resources.Runnable, dir string) error {
	module, err := HandlerModule(runnable.Entrypoint.Handler)
	if err != nil {
		return err
	}
	generated := filepath.Join(dir, GeneratedDirectory)
	if err := os.RemoveAll(generated); err != nil {
		return err
	}
	if err := os.MkdirAll(generated, 0o755); err != nil {
		return err
	}
	if err := harness.Write(generated); err != nil {
		return fmt.Errorf("write harness: %w", err)
	}
	document, err := contract.Generate(runnable, module).Encode()
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(generated, ContractFile), document, 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, TypesFile), Types(runnable), 0o644)
}

// HandlerModule is the importable name of the declared entrypoint. The
// declaration allows any runnable-relative path; only an importable Python
// module can be a handler.
func HandlerModule(handler string) (string, error) {
	trimmed := strings.TrimSuffix(handler, ".py")
	if trimmed == handler {
		return "", fmt.Errorf("handler %q is not a Python module: expected a .py file", handler)
	}
	segments := strings.Split(filepath.ToSlash(trimmed), "/")
	for _, segment := range segments {
		if !identifier.MatchString(segment) {
			return "", fmt.Errorf("handler %q is not importable: %q is not an identifier", handler, segment)
		}
	}
	return strings.Join(segments, "."), nil
}

// Confine resolves a declared runnable-relative path inside dir and refuses a
// target outside it, symlinks included: core validates the declared spelling,
// but whoever opens the file is what decides what content is trusted.
func Confine(dir string, relative string) (string, error) {
	root, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return "", fmt.Errorf("resolve runnable directory: %w", err)
	}
	target := filepath.Join(root, relative)
	resolved, err := filepath.EvalSymlinks(target)
	if err != nil {
		// A path that does not exist yet is confined by its own spelling; its
		// parent is what a symlink could redirect.
		if !os.IsNotExist(err) {
			return "", err
		}
		parent, parentErr := filepath.EvalSymlinks(filepath.Dir(target))
		if parentErr != nil {
			if !os.IsNotExist(parentErr) {
				return "", parentErr
			}
			parent = filepath.Dir(target)
		}
		resolved = filepath.Join(parent, filepath.Base(target))
	}
	if resolved != root && !strings.HasPrefix(resolved, root+string(filepath.Separator)) {
		return "", fmt.Errorf("path %q resolves outside the runnable directory", relative)
	}
	return resolved, nil
}

func writeIfAbsent(path string, content []byte) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	} else if !os.IsNotExist(err) {
		return err
	}
	return os.WriteFile(path, content, 0o644)
}

func handlerTemplate(runnable *resources.Runnable) []byte {
	description := runnable.Description
	if description == "" {
		description = fmt.Sprintf("the %s operation", runnable.Name)
	}
	return []byte(fmt.Sprintf(`"""%s"""

from codefly_runnable import Context

from codefly_types import Input, Output


def handle(context: Context, input: Input) -> %s:
    """Run one invocation.

    The input has already been validated against the contract, and the output
    is validated before the invocation completes.
    """
    raise NotImplementedError("implement %s")
`, description, OutputType, runnable.Name))
}

func projectTemplate(runnable *resources.Runnable) []byte {
	return []byte(fmt.Sprintf(`[project]
name = %q
version = %q
description = %q
requires-python = %q
dependencies = []

[tool.uv]
package = false
`, runnable.Name, runnable.Version, runnable.Description, PythonRequirement))
}
