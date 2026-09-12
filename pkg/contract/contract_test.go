package contract_test

import (
	"fmt"
	"io/fs"
	"strings"
	"testing"

	"github.com/codefly-dev/runnable-python/pkg/contract"
	"github.com/codefly-dev/runnable-python/pkg/harness"
)

// TestTheFramingIsOneContract guards the seam: the Go constants a launcher
// reads and the Python constants the harness enforces are two implementations
// of docs/protocol.md, and they drift silently unless something compares them.
func TestTheFramingIsOneContract(t *testing.T) {
	protocol := source(t, "codefly_runnable/protocol.py")

	for _, declaration := range []string{
		fmt.Sprintf("PROTOCOL = %q", contract.Protocol),
		fmt.Sprintf("REQUEST_SCHEMA = %q", contract.RequestSchema),
		fmt.Sprintf("COMPLETION_SCHEMA = %q", contract.CompletionSchema),
		fmt.Sprintf("REQUEST_PATH_VARIABLE = %q", contract.RequestPathVariable),
		fmt.Sprintf("COMPLETION_PATH_VARIABLE = %q", contract.CompletionPathVariable),
		fmt.Sprintf("COMPLETED = %q", contract.OutcomeCompleted),
		fmt.Sprintf("INVALID_INPUT = %q", contract.OutcomeInvalidInput),
		fmt.Sprintf("INVALID_OUTPUT = %q", contract.OutcomeInvalidOutput),
		fmt.Sprintf("FAILED = %q", contract.OutcomeFailed),
		fmt.Sprintf("TIMEOUT = %q", contract.OutcomeTimeout),
		fmt.Sprintf("INTERRUPTED = %q", contract.OutcomeInterrupted),
		fmt.Sprintf("EXIT_PROTOCOL = %d", contract.ExitProtocol),
		fmt.Sprintf("DEFAULT_MAX_LOG_BYTES = %d * 1024", contract.DefaultMaxLogBytes/1024),
	} {
		if !strings.Contains(protocol, declaration) {
			t.Errorf("the harness does not declare %s", declaration)
		}
	}

	for outcome, code := range map[string]int{
		contract.OutcomeCompleted:     contract.ExitCompleted,
		contract.OutcomeInvalidInput:  contract.ExitInvalidInput,
		contract.OutcomeInvalidOutput: contract.ExitInvalidOutput,
		contract.OutcomeFailed:        contract.ExitFailed,
		contract.OutcomeTimeout:       contract.ExitTimeout,
		contract.OutcomeInterrupted:   contract.ExitInterrupted,
	} {
		mapping := fmt.Sprintf("%s: %d,", strings.ToUpper(outcome), code)
		if !strings.Contains(protocol, mapping) {
			t.Errorf("the harness does not map %s", mapping)
		}
	}

	if generated := source(t, "codefly_runnable/contract.py"); !strings.Contains(
		generated, fmt.Sprintf("CONTRACT_SCHEMA = %q", contract.GeneratedSchema)) {
		t.Errorf("the harness reads a different generated contract schema")
	}
	if runner := source(t, "codefly_runnable/runner.py"); !strings.Contains(
		runner, "getattr(module, contract.handler_attribute") {
		t.Error("the harness no longer resolves the handler through the generated contract")
	}
}

func TestTheHandlerAttributeIsWhatTheScaffoldDefines(t *testing.T) {
	if contract.HandlerAttribute != "handle" {
		t.Fatalf("handler attribute = %q", contract.HandlerAttribute)
	}
}

func source(t *testing.T, path string) string {
	t.Helper()
	content, err := fs.ReadFile(harness.Files(), path)
	if err != nil {
		t.Fatal(err)
	}
	return string(content)
}
