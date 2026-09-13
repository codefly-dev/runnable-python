package contract_test

import (
	"fmt"
	"io/fs"
	"strings"
	"testing"

	"github.com/codefly-dev/core/resources"
	corerunnable "github.com/codefly-dev/core/runnable"

	"github.com/codefly-dev/runnable-python/pkg/contract"
	"github.com/codefly-dev/runnable-python/pkg/harness"
)

// TestTheFramingIsOneContract guards the constants this repository spells
// twice: once in Go, against core, and once as a literal in the embedded
// Python harness. Nothing at build time connects the two, so a core rename
// would otherwise reach the fleet as every invocation exiting 69.
func TestTheFramingIsOneContract(t *testing.T) {
	protocol := source(t, "codefly_runnable/protocol.py")

	for _, declaration := range []string{
		// Core owns the protocol name and the three framing variables.
		fmt.Sprintf("PROTOCOL = %q", resources.RunnableProtocolV1),
		fmt.Sprintf("PROTOCOL_VARIABLE = %q", corerunnable.EnvProtocol),
		fmt.Sprintf("REQUEST_PATH_VARIABLE = %q", corerunnable.EnvInvocationPath),
		fmt.Sprintf("COMPLETION_PATH_VARIABLE = %q", corerunnable.EnvResultPath),
		// The harness's fallback log bound must be core's declared default.
		fmt.Sprintf("DEFAULT_MAX_LOG_BYTES = %d * 1024 * 1024", resources.DefaultRunnableLogBytes/(1024*1024)),
		fmt.Sprintf("EXIT_PROTOCOL = %d", contract.ExitProtocol),
	} {
		if !strings.Contains(protocol, declaration) {
			t.Errorf("the harness does not declare %s", declaration)
		}
	}

	// This agent's Go constants are what a caller compares an observed exit
	// against; the harness is what produces it.
	for name, code := range map[string]int{
		"COMPLETED":      contract.ExitCompleted,
		"INVALID_INPUT":  contract.ExitInvalidInput,
		"INVALID_OUTPUT": contract.ExitInvalidOutput,
		"FAILED":         contract.ExitFailed,
		"TIMEOUT":        contract.ExitTimeout,
		"INTERRUPTED":    contract.ExitInterrupted,
	} {
		if mapping := fmt.Sprintf("%s: %d", name, code); !strings.Contains(protocol, mapping) {
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

// TestTheProtocolIsCoresProtocol keeps this agent's own alias honest.
func TestTheProtocolIsCoresProtocol(t *testing.T) {
	if contract.Protocol != resources.RunnableProtocolV1 {
		t.Fatalf("protocol = %q, core declares %q", contract.Protocol, resources.RunnableProtocolV1)
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
