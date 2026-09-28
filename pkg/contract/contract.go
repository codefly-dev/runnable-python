// Package contract defines agent-private generated configuration for the Python harness.
// Public invocation framing belongs to codefly-dev/core.
package contract

import (
	"encoding/json"
	"fmt"

	"github.com/codefly-dev/core/resources"
)

// Protocol is the only invocation protocol this agent generates for.
const Protocol = resources.RunnableServedProtocolV1

// GeneratedSchema identifies agent-private generated configuration, not wire framing.
const GeneratedSchema = "codefly.runnable-generated-contract/v1"

// HandlerAttribute is the function every generated handler exposes.
const HandlerAttribute = "handle"

// ReceiptAttribute is the function a receipt-recovery handler exposes beside
// it. The harness refuses to serve without one: an owner that cannot answer
// what an effect committed leaves every uncertain outcome uncertain forever.
const ReceiptAttribute = "receipt_of"

// Generated is the contract document the agent writes beside the harness.
type Generated struct {
	Schema   string            `json:"schema"`
	Protocol string            `json:"protocol"`
	Handler  Handler           `json:"handler"`
	Input    Schema            `json:"input"`
	Output   Schema            `json:"output"`
	Recovery string            `json:"recovery"`
	Runnable map[string]string `json:"runnable"`

	MaxInputBytes  uint64 `json:"max-input-bytes"`
	MaxOutputBytes uint64 `json:"max-output-bytes"`
	// TimeoutNanoseconds is the declared timeout, which bounds one call. It is
	// in the document because the harness has to know it without the caller:
	// a call carrying no deadline header is bounded by the author's own
	// statement of how long the operation may run.
	TimeoutNanoseconds int64 `json:"timeout-nanoseconds"`
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
		Schema:             GeneratedSchema,
		Protocol:           Protocol,
		Handler:            Handler{Module: handlerModule, Attribute: HandlerAttribute},
		Input:              schemaOf(runnable.Contract.Input),
		Output:             schemaOf(runnable.Contract.Output),
		Recovery:           string(runnable.Execution.Recovery),
		MaxInputBytes:      runnable.Execution.MaxInputBytes(),
		MaxOutputBytes:     runnable.Execution.MaxOutputBytes(),
		TimeoutNanoseconds: runnable.Execution.GetTimeout().Nanoseconds(),
		// The whole release identity, not the half a declaration states: the
		// harness hands it to the handler, and a field missing from the
		// document is a field the handler cannot read. Standalone generation
		// has no resolved workspace, which is a blank field rather than an
		// absent one.
		Runnable: releaseOf(runnable),
	}
}

// releaseOf is the release identity as the declaration knows it. The Builder
// replaces it with the workspace-resolved one during Load.
func releaseOf(runnable *resources.Runnable) map[string]string {
	identity := runnable.Identity()
	return map[string]string{
		"name":      identity.Name,
		"module":    identity.Module,
		"workspace": identity.Workspace,
		"version":   runnable.Version,
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
