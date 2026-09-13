// Package agent is the gRPC surface of the Python Runnable agent.
package agent

import (
	"context"
	"os/exec"

	"github.com/codefly-dev/core/agents/services"
	agentv0 "github.com/codefly-dev/core/generated/go/codefly/services/agent/v0"
	"github.com/codefly-dev/core/resources"
	runners "github.com/codefly-dev/core/runners/base"

	"github.com/codefly-dev/runnable-python/pkg/prepare"
)

// Agent advertises the Runnable Builder lifecycle. The invocation process is
// supervised by the caller, so this agent advertises no Runtime capability.
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
	info := services.Advertisement{
		CapabilityOnly: true,
		Backends: runners.BackendSupport{
			Local:  func() bool { _, err := exec.LookPath("uv"); return err == nil },
			Docker: false,
		},
		Toolchains: []agentv0.Toolchain_Type{agentv0.Toolchain_PYTHON},
		Languages:  []agentv0.Language_Type{agentv0.Language_PYTHON},
		ReadMe: "Codefly Runnable agent for Python: typed handler scaffolding, the " +
			resources.RunnableProtocolV1 + " harness, uv-locked dependency and interpreter " +
			"preparation, native packages through Builder gRPC. Runnables pin " +
			"their interpreter with spec." + prepare.PythonVersionKey + " (default " +
			prepare.DefaultPythonVersion + ").",
	}.Build()
	info.Capabilities = append(info.Capabilities, &agentv0.Capability{Type: agentv0.Capability_BUILDER})
	return info, nil
}

// ListCommands returns no commands: everything this agent does happens through
// the runnable lifecycle, not through plugin commands.
func (a *Agent) ListCommands(_ context.Context, _ *agentv0.ListCommandsRequest) (*agentv0.ListCommandsResponse, error) {
	return &agentv0.ListCommandsResponse{}, nil
}
