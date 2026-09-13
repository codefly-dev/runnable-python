// Package contract holds the seam between the launcher and the generated
// Python harness: the framing of codefly.runnable/v1 and the generated
// contract document the harness reads at startup.
//
// docs/protocol.md is the normative description; this package and
// harness/codefly_runnable are its two implementations.
package contract

import (
	"encoding/json"
	"fmt"

	"github.com/codefly-dev/core/resources"
)

// Protocol is the only invocation protocol this agent generates for.
const Protocol = resources.RunnableProtocolV1

// Document schemas of the framing.
const (
	RequestSchema    = "codefly.runnable.request/v1"
	CompletionSchema = "codefly.runnable.completion/v1"

	// GeneratedSchema is the contract the agent writes beside the harness at
	// generation time: what the harness enforces that a request does not carry.
	GeneratedSchema = "codefly.runnable-generated-contract/v1"
)

// Environment variables through which a launcher hands the harness the two
// document paths. Logs stay on stdout and stderr, which never carry data.
const (
	RequestPathVariable    = "CODEFLY_RUNNABLE_REQUEST"
	CompletionPathVariable = "CODEFLY_RUNNABLE_COMPLETION"
)

// Outcomes of one invocation. They are distinct on purpose: a launcher that
// cannot tell a timeout from a failure cannot decide whether an effect may be
// recomputed.
const (
	OutcomeCompleted     = "completed"
	OutcomeInvalidInput  = "invalid_input"
	OutcomeInvalidOutput = "invalid_output"
	OutcomeFailed        = "failed"
	OutcomeTimeout       = "timeout"
	OutcomeInterrupted   = "interrupted"
)

// Exit codes repeat the outcome so a missing completion document is never read
// as success.
const (
	ExitCompleted     = 0
	ExitInvalidInput  = 64
	ExitInvalidOutput = 65
	ExitFailed        = 66
	ExitTimeout       = 67
	ExitInterrupted   = 68
	ExitProtocol      = 69
)

// DefaultMaxLogBytes bounds each log stream of one invocation. The declaration
// bounds input and output; core leaves the log bound to the harness.
const DefaultMaxLogBytes uint64 = 256 * 1024

// MaxEnvelopeBytes bounds framing separately from the declared input payload.
const MaxEnvelopeBytes uint64 = 64 * 1024

// HandlerAttribute is the function every generated handler exposes.
const HandlerAttribute = "handle"

// Generated is the contract document the agent writes beside the harness.
type Generated struct {
	Schema   string  `json:"schema"`
	Protocol string  `json:"protocol"`
	Handler  Handler `json:"handler"`
	Input    Schema  `json:"input"`
	Output   Schema  `json:"output"`
	Recovery string  `json:"recovery"`

	MaxInputBytes  uint64 `json:"max-input-bytes"`
	MaxOutputBytes uint64 `json:"max-output-bytes"`
	MaxLogBytes    uint64 `json:"max-log-bytes"`
}

// Handler locates the author entrypoint inside the generated package.
type Handler struct {
	Module    string `json:"module"`
	Attribute string `json:"attribute"`
}

// Schema is the bounded shape of one payload.
type Schema struct {
	Fields []Field `json:"fields"`
}

// Field is one field of the bounded profile.
type Field struct {
	Name     string  `json:"name,omitempty"`
	Type     string  `json:"type"`
	Optional bool    `json:"optional,omitempty"`
	Nullable bool    `json:"nullable,omitempty"`
	Fields   []Field `json:"fields,omitempty"`
	Items    *Field  `json:"items,omitempty"`
}

// Generate builds the contract document for a runnable declaration. The
// handler module is the declared entrypoint without its extension, which
// Runnable.Validate has already confined to the runnable directory.
func Generate(runnable *resources.Runnable, handlerModule string) *Generated {
	return &Generated{
		Schema:         GeneratedSchema,
		Protocol:       Protocol,
		Handler:        Handler{Module: handlerModule, Attribute: HandlerAttribute},
		Input:          schemaOf(runnable.Contract.Input),
		Output:         schemaOf(runnable.Contract.Output),
		Recovery:       string(runnable.Execution.Recovery),
		MaxInputBytes:  runnable.Execution.MaxInputBytes(),
		MaxOutputBytes: runnable.Execution.MaxOutputBytes(),
		MaxLogBytes:    DefaultMaxLogBytes,
	}
}

// Encode renders the contract document with sorted keys, so regenerating an
// unchanged declaration produces the same bytes and the same package digest.
func (g *Generated) Encode() ([]byte, error) {
	encoded, err := json.MarshalIndent(g, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("encode generated contract: %w", err)
	}
	return append(encoded, '\n'), nil
}

func schemaOf(schema *resources.RunnableSchema) Schema {
	out := Schema{Fields: []Field{}}
	for _, field := range schema.Fields {
		out.Fields = append(out.Fields, fieldOf(field))
	}
	return out
}

func fieldOf(field *resources.RunnableField) Field {
	out := Field{
		Name:     field.Name,
		Type:     string(field.Type),
		Optional: field.Optional,
		Nullable: field.Nullable,
	}
	for _, nested := range field.Fields {
		out.Fields = append(out.Fields, fieldOf(nested))
	}
	if field.Items != nil {
		items := fieldOf(field.Items)
		out.Items = &items
	}
	return out
}
