package generate

import (
	"fmt"
	"strings"

	"github.com/codefly-dev/core/resources"
)

// TypesFile is the generated module a handler imports its types from. It is
// not named types.py: that would shadow the standard library module.
const TypesFile = "codefly_types.py"

// InputType and OutputType are the generated names of the two payload types.
const (
	InputType  = "Input"
	OutputType = "Output"
)

// Types renders the typed bindings of a contract. Every object of the bounded
// profile becomes a TypedDict, so a handler's input and output are checked by
// the author's own type checker before the harness ever rejects a payload.
func Types(runnable *resources.Runnable) []byte {
	var classes []string
	var needsNotRequired bool

	for _, schema := range []struct {
		name   string
		fields []*resources.RunnableField
	}{
		{InputType, runnable.Contract.Input.Fields},
		{OutputType, runnable.Contract.Output.Fields},
	} {
		emitted, notRequired := renderObject(schema.name, schema.fields)
		classes = append(classes, emitted...)
		needsNotRequired = needsNotRequired || notRequired
	}

	imports := "TypedDict"
	if needsNotRequired {
		imports = "NotRequired, TypedDict"
	}

	var out strings.Builder
	out.WriteString("\"\"\"Typed bindings generated from runnable.codefly.yaml. Do not edit.\"\"\"\n\n")
	out.WriteString("from __future__ import annotations\n\n")
	fmt.Fprintf(&out, "from typing import %s", imports)
	for _, class := range classes {
		out.WriteString("\n\n\n")
		out.WriteString(class)
	}
	out.WriteString("\n")
	return []byte(out.String())
}

// renderObject emits the nested classes of an object before the object itself,
// so the module reads top to bottom without forward references.
func renderObject(name string, fields []*resources.RunnableField) ([]string, bool) {
	var classes []string
	var notRequired bool

	for _, field := range fields {
		nested, nestedNotRequired := renderNested(name, field)
		classes = append(classes, nested...)
		notRequired = notRequired || nestedNotRequired
	}

	var body strings.Builder
	fmt.Fprintf(&body, "class %s(TypedDict):", name)
	if len(fields) == 0 {
		body.WriteString("\n    pass")
		return append(classes, body.String()), notRequired
	}
	for _, field := range fields {
		annotation := annotationOf(name, field)
		if field.Optional {
			annotation = fmt.Sprintf("NotRequired[%s]", annotation)
			notRequired = true
		}
		fmt.Fprintf(&body, "\n    %s: %s", field.Name, annotation)
	}
	return append(classes, body.String()), notRequired
}

func renderNested(parent string, field *resources.RunnableField) ([]string, bool) {
	switch field.Type {
	case resources.RunnableFieldObject:
		return renderObject(nestedName(parent, field.Name), field.Fields)
	case resources.RunnableFieldArray:
		return renderNested(nestedName(parent, field.Name), field.Items)
	default:
		return nil, false
	}
}

func annotationOf(parent string, field *resources.RunnableField) string {
	var annotation string
	switch field.Type {
	case resources.RunnableFieldString:
		annotation = "str"
	case resources.RunnableFieldInteger:
		annotation = "int"
	case resources.RunnableFieldBoolean:
		annotation = "bool"
	case resources.RunnableFieldObject:
		annotation = nestedName(parent, field.Name)
	case resources.RunnableFieldArray:
		annotation = fmt.Sprintf("list[%s]", annotationOf(nestedName(parent, field.Name), field.Items))
	}
	if field.Nullable {
		annotation += " | None"
	}
	return annotation
}

// nestedName names an anonymous shape after the path that reaches it: an
// array's element type keeps its parent's name with "Item" appended.
func nestedName(parent string, field string) string {
	if field == "" {
		return parent + "Item"
	}
	return parent + camel(field)
}

func camel(name string) string {
	var out strings.Builder
	for _, part := range strings.Split(name, "_") {
		if part == "" {
			continue
		}
		out.WriteString(strings.ToUpper(part[:1]))
		out.WriteString(part[1:])
	}
	return out.String()
}
