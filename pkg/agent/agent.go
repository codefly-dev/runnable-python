// Package agent is the gRPC surface of the Python Runnable agent.
package agent

import (
	"context"

	"github.com/codefly-dev/core/agents/services"
	agentv0 "github.com/codefly-dev/core/generated/go/codefly/services/agent/v0"
	"github.com/codefly-dev/core/resources"
	runners "github.com/codefly-dev/core/runners/base"
	pythonrunner "github.com/codefly-dev/core/runners/python"

	"github.com/codefly-dev/runnable-python/pkg/prepare"
)

// Agent answers what this runnable agent is and what it can do. The
// generation, preparation and packaging surface is not advertised: the
// agent/CLI handoff for loading a runnable and returning its build evidence is
// being frozen in codefly-dev/core#472, and an agent that advertises a
// capability the CLI cannot drive is worse than one that advertises none.
type Agent struct {
	agentv0.UnimplementedAgentServer

	agent *resources.Agent
}

// New builds the agent bound to its manifest.
func New(agent *resources.Agent) *Agent {
	return &Agent{agent: agent}
}

// Manifest returns the agent's own identity, which every build records.
func (a *Agent) Manifest() *resources.Agent {
	return a.agent
}

// GetAgentInformation advertises the Python runnable agent.
func (a *Agent) GetAgentInformation(_ context.Context, _ *agentv0.AgentInformationRequest) (*agentv0.AgentInformation, error) {
	return services.Advertisement{
		CapabilityOnly: true,
		Backends: runners.BackendSupport{
			Local:  pythonrunner.HasUVRuntime,
			Docker: true,
		},
		Toolchains: []agentv0.Toolchain_Type{agentv0.Toolchain_PYTHON},
		Languages:  []agentv0.Language_Type{agentv0.Language_PYTHON},
		ReadMe: "Codefly Runnable agent for Python: typed handler scaffolding, the " +
			resources.RunnableProtocolV1 + " harness, uv-locked dependency and interpreter " +
			"preparation, identified native packages and Linux image recipes. Runnables pin " +
			"their interpreter with spec." + prepare.PythonVersionKey + " (default " +
			prepare.DefaultPythonVersion + ").",
	}.Build(), nil
}

// ListCommands returns no commands: everything this agent does happens through
// the runnable lifecycle, not through plugin commands.
func (a *Agent) ListCommands(_ context.Context, _ *agentv0.ListCommandsRequest) (*agentv0.ListCommandsResponse, error) {
	return &agentv0.ListCommandsResponse{}, nil
}
