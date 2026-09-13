// Binary runnable-python is the Codefly Runnable agent for Python.
//
// The implementation lives under ./pkg:
//
//	github.com/codefly-dev/runnable-python/pkg/contract  — codefly.runnable/v1 framing
//	github.com/codefly-dev/runnable-python/pkg/harness   — the Python harness it generates
//	github.com/codefly-dev/runnable-python/pkg/generate  — handler, typed bindings, contract
//	github.com/codefly-dev/runnable-python/pkg/prepare   — locked dependencies and interpreter
//	github.com/codefly-dev/runnable-python/pkg/pack      — native package and build evidence
//	github.com/codefly-dev/runnable-python/pkg/recipe    — Linux image recipe and context
package main

import (
	"embed"

	"github.com/codefly-dev/core/agents"
	"github.com/codefly-dev/core/resources"
	"github.com/codefly-dev/core/shared"

	runnableagent "github.com/codefly-dev/runnable-python/pkg/agent"
)

//go:embed agent.codefly.yaml
var infoFS embed.FS

var manifest = shared.Must(resources.LoadFromFs[resources.Agent](shared.Embed(infoFS)))

func main() {
	agents.Serve(agents.PluginRegistration{
		Agent:   runnableagent.New(manifest.Of(resources.RunnableAgent)),
		Builder: runnableagent.NewBuilder(manifest.Of(resources.RunnableAgent)),
	})
}
