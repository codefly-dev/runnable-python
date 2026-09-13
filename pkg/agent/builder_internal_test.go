package agent

import (
	"os"
	"path/filepath"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// TestOutputDirectoryKeepsTheOutputOutOfTheSource covers the guard that stops
// a build from copying its own output back into itself. It is reached over
// gRPC only through a full preparation, so the cases live here.
func TestOutputDirectoryKeepsTheOutputOutOfTheSource(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "runnable")
	if err := os.Mkdir(source, 0o755); err != nil {
		t.Fatal(err)
	}
	// A symlink whose target is inside the source: the spelling says nothing
	// about where writing through it lands.
	into := filepath.Join(root, "into-source")
	if err := os.Symlink(source, into); err != nil {
		t.Fatal(err)
	}
	dangling := filepath.Join(root, "dangling")
	if err := os.Symlink(filepath.Join(root, "absent"), dangling); err != nil {
		t.Fatal(err)
	}
	// A sibling sharing the source's name as a prefix is not inside it.
	sibling := filepath.Join(root, "runnable-output")

	for _, test := range []struct {
		name      string
		directory string
		refused   bool
	}{
		{"a relative path", "build", true},
		{"the empty path", "", true},
		{"the source itself", source, true},
		{"a directory inside the source", filepath.Join(source, "build"), true},
		{"a missing directory inside the source", filepath.Join(source, "a", "b"), true},
		{"a directory containing the source", root, true},
		{"a symlink into the source", filepath.Join(into, "build"), true},
		{"a dangling symlink", filepath.Join(dangling, "build"), true},
		{"a sibling of the source", filepath.Join(root, "build"), false},
		{"a sibling prefixed by the source name", sibling, false},
		{"a missing sibling of the source", filepath.Join(root, "a", "b"), false},
	} {
		t.Run(test.name, func(t *testing.T) {
			resolved, err := outputDirectory(test.directory, source)
			if test.refused {
				if err == nil {
					t.Fatalf("accepted %s, resolved to %s", test.directory, resolved)
				}
				return
			}
			if err != nil {
				t.Fatalf("refused %s: %v", test.directory, err)
			}
			if !filepath.IsAbs(resolved) {
				t.Fatalf("resolved to a relative path %q", resolved)
			}
		})
	}
}

// TestOutputDirectoryCreatesNothing proves a refused output leaves no
// directory behind: the check runs on the resolved path before anything is
// written, so a rejected request cannot seed the source with empty directories.
func TestOutputDirectoryCreatesNothing(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "runnable")
	if err := os.Mkdir(source, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := outputDirectory(filepath.Join(source, "build", "native"), source); err == nil {
		t.Fatal("accepted an output inside the source")
	}
	if _, err := os.Stat(filepath.Join(source, "build")); !os.IsNotExist(err) {
		t.Fatalf("a refused output left a directory inside the source: %v", err)
	}
	if _, err := outputDirectory(filepath.Join(root, "accepted", "native"), source); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "accepted")); !os.IsNotExist(err) {
		t.Fatalf("an accepted output was created before it was asked for: %v", err)
	}
}

// TestRequireEmptyRefusesAPopulatedDestination guards the precondition that
// preparation enforces from the inside. Reporting it here is what keeps a
// retry from destroying the snapshot a caller already has.
func TestRequireEmptyRefusesAPopulatedDestination(t *testing.T) {
	root := t.TempDir()
	if err := requireEmpty(filepath.Join(root, "absent")); err != nil {
		t.Fatalf("a destination that does not exist yet is empty: %v", err)
	}
	empty := filepath.Join(root, "empty")
	if err := os.Mkdir(empty, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := requireEmpty(empty); err != nil {
		t.Fatalf("an empty destination was refused: %v", err)
	}
	if err := os.WriteFile(filepath.Join(empty, "prepared"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	err := requireEmpty(empty)
	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("status = %s, want %s (%v)", status.Code(err), codes.AlreadyExists, err)
	}
}
