// Package prepare turns a generated runnable into a prepared tree: a locked
// dependency set, an interpreter of the declared version and the entry point
// the launch command names.
package prepare

import (
	"context"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/codefly-dev/core/resources"

	"github.com/codefly-dev/runnable-python/pkg/generate"
)

// Layout of a prepared tree. The launch command is relative to its root, so
// these names are part of the native package contract.
const (
	SourceDirectory  = "runnable"
	Environment      = ".venv"
	EntryFile        = "main.py"
	RequirementsFile = "requirements.txt"

	// DefaultPythonVersion is used when the declaration's spec pins none.
	DefaultPythonVersion = "3.12"
)

// PythonVersionKey is the spec key pinning the interpreter of a runnable.
const PythonVersionKey = "python-version"

// Prepared describes a tree ready to be archived or copied into an image.
type Prepared struct {
	// Root is the prepared tree; the launch command is relative to it.
	Root string
	// Toolchain identifies the interpreter the package was prepared with,
	// read from that interpreter rather than from the requested version.
	Toolchain string
	// Command launches one invocation, relative to Root.
	Command []string
}

// Prepare materializes the prepared tree for a generated runnable under root.
func Prepare(ctx context.Context, runnable *resources.Runnable, source string, root string) (*Prepared, error) {
	uv, err := exec.LookPath("uv")
	if err != nil {
		return nil, fmt.Errorf("uv is required to prepare a Python runnable: %w", err)
	}
	if err := run(ctx, uv, source, "lock"); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, err
	}
	if err := copyTree(source, filepath.Join(root, SourceDirectory)); err != nil {
		return nil, fmt.Errorf("copy runnable source: %w", err)
	}
	requirements := filepath.Join(root, RequirementsFile)
	if err := run(ctx, uv, source,
		"export", "--frozen", "--no-emit-project", "--format", "requirements.txt", "-o", requirements); err != nil {
		return nil, err
	}

	environment := filepath.Join(root, Environment)
	version := PythonVersion(runnable)
	if err := run(ctx, uv, root, "venv", "--relocatable", "--python", version, environment); err != nil {
		return nil, err
	}
	// The command is relative to the package root; the build resolves it
	// against the tree it just prepared.
	interpreter := filepath.Join(Environment, "bin", "python")
	if err := run(ctx, uv, root,
		"pip", "install", "--python", filepath.Join(root, interpreter), "--require-hashes", "-r", requirements); err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(root, EntryFile), entryPoint(), 0o644); err != nil {
		return nil, err
	}

	toolchain, err := toolchainOf(ctx, filepath.Join(root, interpreter))
	if err != nil {
		return nil, err
	}
	return &Prepared{
		Root:      root,
		Toolchain: toolchain,
		Command:   []string{interpreter, EntryFile},
	}, nil
}

// PythonVersion returns the interpreter version the declaration pins.
func PythonVersion(runnable *resources.Runnable) string {
	if version, ok := runnable.Spec[PythonVersionKey].(string); ok && version != "" {
		return version
	}
	return DefaultPythonVersion
}

// toolchainOf reads the version out of the prepared interpreter: the package's
// identity is what was installed, never what was requested.
func toolchainOf(ctx context.Context, interpreter string) (string, error) {
	command := exec.CommandContext(ctx, interpreter, "-c", "import platform; print(platform.python_version())")
	output, err := command.Output()
	if err != nil {
		return "", fmt.Errorf("read interpreter version: %w", err)
	}
	return "python-" + strings.TrimSpace(string(output)), nil
}

func run(ctx context.Context, uv string, dir string, args ...string) error {
	command := exec.CommandContext(ctx, uv, args...)
	command.Dir = dir
	var diagnostics strings.Builder
	command.Stderr = &diagnostics
	if err := command.Run(); err != nil {
		return fmt.Errorf("uv %s: %w: %s", strings.Join(args, " "), err, strings.TrimSpace(diagnostics.String()))
	}
	return nil
}

// copyTree copies the generated runnable, leaving behind what a developer's
// environment left in it: those are not build inputs and would change the
// package bytes.
func copyTree(source string, target string) error {
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		name := entry.Name()
		if entry.IsDir() && (name == Environment || name == "__pycache__" || name == ".git") {
			return filepath.SkipDir
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		destination := filepath.Join(target, relative)
		if entry.IsDir() {
			return os.MkdirAll(destination, 0o755)
		}
		if !entry.Type().IsRegular() {
			return fmt.Errorf("%s is not a regular file", relative)
		}
		return copyFile(path, destination, entry)
	})
}

func copyFile(path string, destination string, entry fs.DirEntry) error {
	info, err := entry.Info()
	if err != nil {
		return err
	}
	from, err := os.Open(path)
	if err != nil {
		return err
	}
	defer from.Close()
	to, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, info.Mode().Perm())
	if err != nil {
		return err
	}
	defer to.Close()
	_, err = io.Copy(to, from)
	return err
}

func entryPoint() []byte {
	return []byte(fmt.Sprintf(`"""Entry point of the native package. Generated; do not edit."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / %q / %q))

from codefly_runnable.runner import main  # noqa: E402

sys.exit(main())
`, SourceDirectory, generate.GeneratedDirectory))
}
