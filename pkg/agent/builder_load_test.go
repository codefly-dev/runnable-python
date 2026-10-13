package agent

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	builderv0 "github.com/codefly-dev/core/generated/go/codefly/services/builder/v0"
	"github.com/codefly-dev/core/resources"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// TestLoadRefusesWhatTheHarnessCannotHonour holds both refusals at the first
// call that sees a declaration. Core admits each value, and a package built
// for either would claim what its harness never does when called: a submit
// package would answer a bare output where an acceptance is owed, and a signal
// package would promise an interrupted outcome no Python thread can report.
func TestLoadRefusesWhatTheHarnessCannotHonour(t *testing.T) {
	for _, test := range []struct {
		name    string
		declare func(*resources.RunnableExecution)
		refused string
	}{
		{"call completion and no cancellation", func(*resources.RunnableExecution) {}, ""},
		{"submit completion", func(e *resources.RunnableExecution) {
			e.Recovery, e.Completion = resources.RunnableRecoveryReceipt, resources.RunnableCompletionSubmit
		}, `completion "submit"`},
		{"signal cancellation", func(e *resources.RunnableExecution) {
			e.Cancellation = resources.RunnableCancellationSignal
		}, `cancellation "signal"`},
	} {
		t.Run(test.name, func(t *testing.T) {
			ctx := context.Background()
			builder, request := declared(t, test.declare)

			loaded, err := builder.Load(ctx, request)

			if test.refused == "" {
				if err != nil {
					t.Fatalf("a declaration the harness honours was refused: %v", err)
				}
				if loaded.GetState().GetState() != builderv0.LoadStatus_READY {
					t.Fatalf("state = %v", loaded.GetState().GetState())
				}
				return
			}
			if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), test.refused) {
				t.Fatalf("Load = %v, want InvalidArgument naming %s", err, test.refused)
			}
			if builder.runnable != nil {
				t.Fatal("a refused declaration stayed loaded")
			}
		})
	}
}

func declared(t *testing.T, declare func(*resources.RunnableExecution)) (*Builder, *builderv0.LoadRequest) {
	t.Helper()
	ctx := context.Background()
	manifest, err := resources.LoadFromPath[resources.Agent](ctx, filepath.Join("..", "..", "agent.codefly.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "workspace.codefly.yaml"),
		[]byte("kind: workspace\nname: proof\nlayout: flat\nservices: []\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	workspace, err := resources.LoadWorkspaceFromDir(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	module, err := workspace.LoadModuleFromName(ctx, "proof")
	if err != nil {
		t.Fatal(err)
	}
	r, err := module.NewRunnable(ctx, "word-count", manifest, "handler.py")
	if err != nil {
		t.Fatal(err)
	}
	declare(r.Execution)
	if err := r.Save(ctx); err != nil {
		t.Fatal(err)
	}
	location, err := workspace.RunnableLocationOf(r)
	if err != nil {
		t.Fatal(err)
	}
	wire, err := location.Proto()
	if err != nil {
		t.Fatal(err)
	}
	return NewBuilder(manifest), &builderv0.LoadRequest{Runnable: wire}
}
