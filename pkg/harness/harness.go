// Package harness ships the Python invocation harness the agent generates into
// every runnable. The Python sources beside this file are the implementation;
// docs/protocol.md is the contract they honor.
package harness

import (
	"crypto/sha256"
	"embed"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
)

//go:embed all:codefly_runnable
var files embed.FS

// Package is the importable name of the harness inside a generated runnable.
const Package = "codefly_runnable"

// sources reports whether an embedded entry belongs to the harness. Only the
// modules do: a build host that left bytecode beside them must not change the
// harness digest or reach a generated package.
func sources(path string, entry fs.DirEntry) bool {
	if entry.IsDir() {
		return entry.Name() != "__pycache__"
	}
	return filepath.Ext(path) == ".py"
}

// Files exposes the embedded harness sources.
func Files() fs.FS {
	return files
}

// Write materializes the harness under dir, creating dir/codefly_runnable.
func Write(dir string) error {
	return fs.WalkDir(files, Package, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !sources(path, entry) {
			if entry.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		target := filepath.Join(dir, path)
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		content, err := files.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, content, 0o644)
	})
}

// Digest identifies the exact harness generated into a package, as
// "sha256:<hex>" over every source path and its content. Two builds with the
// same digest ran the same harness bytes; a launcher never has to trust that
// an agent version implies them.
func Digest() (string, error) {
	var paths []string
	err := fs.WalkDir(files, Package, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !sources(path, entry) {
			if entry.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if !entry.IsDir() {
			paths = append(paths, path)
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(paths)
	digest := sha256.New()
	for _, path := range paths {
		content, err := files.ReadFile(path)
		if err != nil {
			return "", err
		}
		fmt.Fprintf(digest, "%s %x\n", path, sha256.Sum256(content))
	}
	return fmt.Sprintf("sha256:%x", digest.Sum(nil)), nil
}
