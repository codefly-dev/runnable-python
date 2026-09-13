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
	g := &typeGenerator{names: make(map[*resources.RunnableField]string), used: map[string]bool{InputType: true, OutputType: true}}

	for _, schema := range []struct {
		name   string
		fields []*resources.RunnableField
	}{
		{InputType, runnable.Contract.Input.Fields},
		{OutputType, runnable.Contract.Output.Fields},
	} {
		emitted, notRequired := g.renderObject(schema.name, schema.fields)
		classes = append(classes, emitted...)
		needsNotRequired = needsNotRequired || notRequired
	}

	imports := "TypedDict"
	if needsNotRequired {
		imports = "NotRequired, TypedDict"
	}

	var out strings.Builder
	out.WriteString("\"\"\"Typed bindings generated from runnable.codefly.yaml. Do not edit.\"\"\"\n\n")
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
func (g *typeGenerator) renderObject(name string, fields []*resources.RunnableField) ([]string, bool) {
	var classes []string
	var notRequired bool

	for _, field := range fields {
		nested, nestedNotRequired := g.renderNested(name, field)
		classes = append(classes, nested...)
		notRequired = notRequired || nestedNotRequired
	}

	var body strings.Builder
	fmt.Fprintf(&body, "%s = TypedDict(%q, {", name, name)
	if len(fields) == 0 {
		body.WriteString("})")
		return append(classes, body.String()), notRequired
	}
	for _, field := range fields {
		annotation := g.annotationOf(field)
		if field.Optional {
			annotation = fmt.Sprintf("NotRequired[%s]", annotation)
			notRequired = true
		}
		fmt.Fprintf(&body, "\n    %q: %s,", field.Name, annotation)
	}
	body.WriteString("\n})")
	return append(classes, body.String()), notRequired
}

func (g *typeGenerator) renderNested(parent string, field *resources.RunnableField) ([]string, bool) {
	switch field.Type {
	case resources.RunnableFieldObject:
		name := g.allocate(nestedName(parent, field.Name))
		g.names[field] = name
		return g.renderObject(name, field.Fields)
	case resources.RunnableFieldArray:
		return g.renderNested(nestedName(parent, field.Name), field.Items)
	default:
		return nil, false
	}
}

func (g *typeGenerator) annotationOf(field *resources.RunnableField) string {
	var annotation string
	switch field.Type {
	case resources.RunnableFieldString:
		annotation = "str"
	case resources.RunnableFieldInteger:
		annotation = "int"
	case resources.RunnableFieldBoolean:
		annotation = "bool"
	case resources.RunnableFieldObject:
		annotation = g.names[field]
	case resources.RunnableFieldArray:
		annotation = fmt.Sprintf("list[%s]", g.annotationOf(field.Items))
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

// Names remain readable, while a shared allocator prevents distinct wire paths
// from overwriting one another's classes after capitalization.
type typeGenerator struct {
	names map[*resources.RunnableField]string
	used  map[string]bool
}

func (g *typeGenerator) allocate(hint string) string {
	name := hint
	for suffix := 2; g.used[name]; suffix++ {
		name = fmt.Sprintf("%s%d", hint, suffix)
	}
	g.used[name] = true
	return name
}
