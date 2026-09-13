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
	SourceDirectory      = "runnable"
	Environment          = ".venv"
	InterpreterDirectory = ".python"
	PackagesDirectory    = ".packages"
	EntryFile            = "main.py"
	RequirementsFile     = "requirements.txt"

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
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	if len(entries) != 0 {
		return nil, fmt.Errorf("prepare destination must be empty: %s", root)
	}
	snapshot := filepath.Join(root, SourceDirectory)
	if err := copyTree(source, snapshot); err != nil {
		return nil, fmt.Errorf("copy runnable source: %w", err)
	}
	requirements := filepath.Join(root, RequirementsFile)
	if err := run(ctx, uv, snapshot,
		"export", "--frozen", "--no-emit-project", "--format", "requirements.txt", "-o", requirements); err != nil {
		return nil, err
	}

	interpreter, err := bundleInterpreter(ctx, uv, root, PythonVersion(runnable))
	if err != nil {
		return nil, err
	}
	if err := run(ctx, uv, root,
		"pip", "install", "--python", filepath.Join(root, interpreter),
		"--target", filepath.Join(root, PackagesDirectory), "--require-hashes", "-r", requirements); err != nil {
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
		Command:   []string{interpreter, "-I", EntryFile},
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
	command := exec.CommandContext(ctx, interpreter, "-I", "-c", "import platform; print(platform.python_version())")
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
import site
from pathlib import Path

site.addsitedir(str(Path(__file__).resolve().parent / %q))
sys.path.insert(0, str(Path(__file__).resolve().parent / %q / %q))

from codefly_runnable.runner import main  # noqa: E402

sys.exit(main())
`, PackagesDirectory, SourceDirectory, generate.GeneratedDirectory))
}

// A managed CPython distribution includes its standard library and shared
// libraries. A virtualenv alone points back to the builder's interpreter.
func bundleInterpreter(ctx context.Context, uv, root, version string) (string, error) {
	if err := run(ctx, uv, root, "python", "install", "--no-bin", version); err != nil {
		return "", err
	}
	find := exec.CommandContext(ctx, uv, "python", "find", "--no-project", "--managed-python", "--resolve-links", version)
	find.Dir = root
	output, err := find.Output()
	if err != nil {
		return "", fmt.Errorf("find managed interpreter: %w", err)
	}
	executable := strings.TrimSpace(string(output))
	inspect := exec.CommandContext(ctx, executable, "-I", "-c", "import sys; print(sys.base_prefix)")
	output, err = inspect.Output()
	if err != nil {
		return "", fmt.Errorf("inspect managed interpreter: %w", err)
	}
	prefix, err := filepath.EvalSymlinks(strings.TrimSpace(string(output)))
	if err != nil {
		return "", err
	}
	relative, err := filepath.Rel(prefix, executable)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("interpreter is outside its runtime: %s", executable)
	}
	target := filepath.Join(root, InterpreterDirectory)
	if err := copyRuntime(prefix, target); err != nil {
		return "", err
	}
	launcher := filepath.Join(target, "bin", "python")
	if launcher != filepath.Join(target, relative) {
		if err := os.Remove(launcher); err != nil && !os.IsNotExist(err) {
			return "", err
		}
		link, err := filepath.Rel(filepath.Dir(launcher), filepath.Join(target, relative))
		if err != nil {
			return "", err
		}
		if err := os.Symlink(link, launcher); err != nil {
			return "", err
		}
	}
	return filepath.Join(InterpreterDirectory, "bin", "python"), nil
}

func copyRuntime(source, target string) error {
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() && entry.Name() == "__pycache__" {
			return filepath.SkipDir
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		destination := filepath.Join(target, relative)
		if entry.IsDir() {
			return os.MkdirAll(destination, 0755)
		}
		if entry.Type()&os.ModeSymlink != 0 {
			resolved, err := filepath.EvalSymlinks(path)
			if err != nil {
				return err
			}
			inside, err := filepath.Rel(source, resolved)
			if err != nil || inside == ".." || strings.HasPrefix(inside, ".."+string(filepath.Separator)) {
				return fmt.Errorf("runtime symlink escapes its distribution: %s", path)
			}
			link, err := filepath.Rel(filepath.Dir(destination), filepath.Join(target, inside))
			if err != nil {
				return err
			}
			return os.Symlink(link, destination)
		}
		if !entry.Type().IsRegular() {
			return fmt.Errorf("unsupported runtime entry: %s", path)
		}
		return copyFile(path, destination, entry)
	})
}
