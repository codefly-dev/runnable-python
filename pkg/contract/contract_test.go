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
// twice: once in Go, against core, and once as a literal in the embedded Python
// harness. Nothing at build time connects the two, and runnable-go renders the
// same strings from the same source, so a core rename would otherwise reach the
// fleet as an owner nobody can call — which reads as an outage rather than as a
// mistake in a string.
func TestTheFramingIsOneContract(t *testing.T) {
	protocol := source(t, "codefly_runnable/protocol.py")

	for _, declaration := range []string{
		fmt.Sprintf("PROTOCOL = %q", resources.RunnableServedProtocolV1),
		fmt.Sprintf("ADDRESS_VARIABLE = %q", corerunnable.ListenAddressEnv),
		fmt.Sprintf("INVOKE_PROCEDURE = %q", corerunnable.ServedInvokeProcedure),
		fmt.Sprintf("LOOKUP_PROCEDURE = %q", corerunnable.ServedLookupProcedure),
		fmt.Sprintf("WORK_CONTEXT_HEADER = %q", corerunnable.WorkContextHeader),
		fmt.Sprintf("EFFECT_HEADER = %q", corerunnable.EffectHeader),
		fmt.Sprintf("DEADLINE_HEADER = %q", corerunnable.DeadlineHeader),
		fmt.Sprintf("FAILURE_CODE_HEADER = %q", corerunnable.FailureCodeHeader),
	} {
		if !strings.Contains(protocol, declaration) {
			t.Errorf("the harness does not declare %s", declaration)
		}
	}

	if generated := source(t, "codefly_runnable/contract.py"); !strings.Contains(
		generated, fmt.Sprintf("CONTRACT_SCHEMA = %q", contract.GeneratedSchema)) {
		t.Errorf("the harness reads a different generated contract schema")
	}
	if generated := source(t, "codefly_runnable/contract.py"); !strings.Contains(
		generated, fmt.Sprintf("RECEIPT_ATTRIBUTE = %q", contract.ReceiptAttribute)) {
		t.Errorf("the harness looks up a different receipt function than the scaffold writes")
	}
	if runner := source(t, "codefly_runnable/runner.py"); !strings.Contains(
		runner, "getattr(module, attribute") {
		t.Error("the harness no longer resolves the handler through the generated contract")
	}
}

// TestTheFailureCodeHeaderIsSetInOnePlace holds the property the whole taxonomy
// rests on. That header is the only thing that proves to a caller that no
// effect committed, so the handler's own declared failure sets it and nothing
// else does: a harness that also set it for a crash or a deadline would assert
// on the handler's behalf that its effect did not happen, and the caller would
// stop looking for a receipt that exists.
func TestTheFailureCodeHeaderIsSetInOnePlace(t *testing.T) {
	runner := source(t, "codefly_runnable/runner.py")
	if occurrences := strings.Count(runner, "FAILURE_CODE_HEADER"); occurrences != 2 {
		t.Fatalf("the failure-code header is named %d times: once in the import and once where it is set", occurrences)
	}
	if !strings.Contains(runner, "self.send_header(FAILURE_CODE_HEADER, answer.failure.code)") {
		t.Error("the failure-code header no longer carries the handler's own declared code")
	}
}

// TestTheProtocolIsCoresProtocol keeps this agent's own alias honest.
func TestTheProtocolIsCoresProtocol(t *testing.T) {
	if contract.Protocol != resources.RunnableServedProtocolV1 {
		t.Fatalf("protocol = %q, core declares %q", contract.Protocol, resources.RunnableServedProtocolV1)
	}
}

func TestTheHandlerAttributeIsWhatTheScaffoldDefines(t *testing.T) {
	if contract.HandlerAttribute != "handle" {
		t.Fatalf("handler attribute = %q", contract.HandlerAttribute)
	}
	if contract.ReceiptAttribute != "receipt_of" {
		t.Fatalf("receipt attribute = %q", contract.ReceiptAttribute)
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
