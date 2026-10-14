package contract_test

import (
	"fmt"
	"io/fs"
	"regexp"
	"strconv"
	"strings"
	"testing"

	validate "buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go/buf/validate"
	basev0 "github.com/codefly-dev/core/generated/go/codefly/base/v0"
	"github.com/codefly-dev/core/resources"
	corerunnable "github.com/codefly-dev/core/runnable"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

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

// TestTheHeaderBoundsAreCoresBounds holds the harness's limits on the per-call
// headers to the rules core validates the same values with. They live only in
// core's proto, so they are read from its descriptors rather than remembered:
// a harness admitting more than core would accept a call no caller core admits
// could have sent, and one admitting less would refuse calls core says are
// valid.
func TestTheHeaderBoundsAreCoresBounds(t *testing.T) {
	protocol := source(t, "codefly_runnable/protocol.py")
	for _, bound := range []struct {
		literal string
		message proto.Message
		field   protoreflect.Name
	}{
		{"MAX_WORK_CONTEXT_LENGTH", &basev0.RunnableInvocationIdentity{}, "work_context"},
		{"MAX_EFFECT_ID_LENGTH", &basev0.RunnableInvocation{}, "effect_id"},
	} {
		field := bound.message.ProtoReflect().Descriptor().Fields().ByName(bound.field)
		rules, _ := proto.GetExtension(field.Options(), validate.E_Field).(*validate.FieldRules)
		limit := rules.GetString().GetMaxLen()
		if limit == 0 {
			t.Fatalf("core declares no max_len on %s", field.FullName())
		}
		declared := regexp.MustCompile(`(?m)^`+bound.literal+` = ([0-9]+)$`).FindAllStringSubmatch(protocol, -1)
		if len(declared) != 1 {
			t.Fatalf("the harness declares %s %d times, not once", bound.literal, len(declared))
		}
		if value, err := strconv.ParseUint(declared[0][1], 10, 64); err != nil || value != limit {
			t.Errorf("the harness declares %s = %s; core bounds %s at %d", bound.literal, declared[0][1], field.FullName(), limit)
		}
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
	if !strings.Contains(runner, "self.send_header(FAILURE_CODE_HEADER, code)") ||
		strings.Count(runner, "code = _code_of(answer.failure)") != 1 {
		t.Error("the failure-code header no longer carries the handler's own declared code, read once and checked")
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
