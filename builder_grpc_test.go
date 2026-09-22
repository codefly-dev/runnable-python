package main_test

import (
	"context"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/codefly-dev/core/agents/contract"
	"github.com/codefly-dev/core/agents/manager"
	basev0 "github.com/codefly-dev/core/generated/go/codefly/base/v0"
	agentv0 "github.com/codefly-dev/core/generated/go/codefly/services/agent/v0"
	builderv0 "github.com/codefly-dev/core/generated/go/codefly/services/builder/v0"
	"github.com/codefly-dev/core/resources"
	corerunnable "github.com/codefly-dev/core/runnable"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestBuilderPackagesThePreparedSnapshotOverGRPC(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer cancel()
	root := t.TempDir()
	t.Setenv(resources.CodeflyHomeEnv, filepath.Join(root, "home"))
	t.Setenv(manager.AgentSourceEnv, "local")
	agent, err := resources.ParseAgent(ctx, resources.RunnableAgent, "codefly.dev/python:0.0.2")
	require.NoError(t, err)
	binary, err := agent.Path(ctx)
	require.NoError(t, err)
	require.NoError(t, os.MkdirAll(filepath.Dir(binary), 0700))
	compile := exec.CommandContext(ctx, "go", "build", "-o", binary, ".")
	output, err := compile.CombinedOutput()
	require.NoError(t, err, string(output))
	connection, err := manager.Load(ctx, agent, manager.WithoutSandbox(), manager.WithoutPrincipal(), manager.WithLogWriter(io.Discard))
	require.NoError(t, err)
	defer connection.Close()
	client := builderv0.NewBuilderClient(connection.GRPCConn())
	info, err := agentv0.NewAgentClient(connection.GRPCConn()).GetAgentInformation(ctx, &agentv0.AgentInformationRequest{})
	require.NoError(t, err)
	require.NoError(t, contract.Check(info.GetContract()), "the live agent must advertise the protocol implemented by its server")
	require.Equal(t, []*agentv0.Capability{{Type: agentv0.Capability_BUILDER}}, info.GetCapabilities())

	workspaceDir := filepath.Join(root, "workspace")
	require.NoError(t, os.Mkdir(workspaceDir, 0700))
	write(t, filepath.Join(workspaceDir, "workspace.codefly.yaml"), "kind: workspace\nname: proof\nlayout: flat\nservices: []\n")
	workspace, err := resources.LoadWorkspaceFromDir(ctx, workspaceDir)
	require.NoError(t, err)
	module, err := workspace.LoadModuleFromName(ctx, "proof")
	require.NoError(t, err)
	r, err := module.NewRunnable(ctx, "word-count", agent, "handler.py")
	require.NoError(t, err)
	r.Contract.Input.Fields = []*resources.RunnableField{{Name: "text", Type: resources.RunnableFieldType("string")}}
	r.Contract.Output.Fields = []*resources.RunnableField{{Name: "count", Type: resources.RunnableFieldType("integer")}}
	require.NoError(t, r.Save(ctx))
	location, err := workspace.RunnableLocationOf(r)
	require.NoError(t, err)
	wire, err := location.Proto()
	require.NoError(t, err)
	loaded, err := client.Load(ctx, &builderv0.LoadRequest{Runnable: wire})
	require.NoError(t, err)
	require.Equal(t, builderv0.LoadStatus_READY, loaded.GetState().GetState())
	created, err := client.Create(ctx, &builderv0.CreateRequest{})
	require.NoError(t, err)
	require.Equal(t, builderv0.CreateStatus_CREATED, created.GetState().GetState())
	require.FileExists(t, filepath.Join(r.Dir(), "handler.py"))
	require.FileExists(t, filepath.Join(r.Dir(), "codefly_types.py"))
	write(t, filepath.Join(r.Dir(), "handler.py"), "def handle(context, input):\n    return {\"count\": len(input[\"text\"].split())}\n")

	rejectedOutput := filepath.Join(r.Dir(), "build", "prepared")
	_, err = client.RunnableBuildInputs(ctx, &builderv0.RunnableBuildInputsRequest{OutputDirectory: rejectedOutput})
	require.Equal(t, codes.InvalidArgument, status.Code(err))
	require.NoDirExists(t, filepath.Join(r.Dir(), "build"))
	sourceLink := filepath.Join(root, "source-link")
	require.NoError(t, os.Symlink(r.Dir(), sourceLink))
	_, err = client.RunnableBuildInputs(ctx, &builderv0.RunnableBuildInputsRequest{OutputDirectory: filepath.Join(sourceLink, "build")})
	require.Equal(t, codes.InvalidArgument, status.Code(err))
	require.NoDirExists(t, filepath.Join(r.Dir(), "build"))

	prepared, err := client.RunnableBuildInputs(ctx, &builderv0.RunnableBuildInputsRequest{OutputDirectory: filepath.Join(root, "prepared")})
	require.NoError(t, err)
	require.Equal(t, builderv0.RunnableBuildInputsStatus_SUCCESS, prepared.GetState().GetState())

	// Preparing again into the caller's populated directory is refused by name
	// rather than from inside preparation, and the snapshot already prepared
	// survives the refusal: the Package below still emits it.
	_, err = client.RunnableBuildInputs(ctx, &builderv0.RunnableBuildInputsRequest{OutputDirectory: filepath.Join(root, "prepared")})
	require.Equal(t, codes.AlreadyExists, status.Code(err))

	write(t, filepath.Join(r.Dir(), "handler.py"), "def handle(context, input):\n    return {\"count\": 999}\n")
	_, err = client.Package(ctx, &builderv0.PackageRequest{Targets: []*builderv0.PackageTarget{{Os: "other", Architecture: runtime.GOARCH}}, OutputDirectory: filepath.Join(root, "unsupported")})
	require.Equal(t, codes.Unimplemented, status.Code(err))
	require.NoDirExists(t, filepath.Join(root, "unsupported"))
	response, err := client.Package(ctx, &builderv0.PackageRequest{OutputDirectory: filepath.Join(root, "artifacts")})
	require.NoError(t, err)
	require.Equal(t, builderv0.PackageStatus_SUCCESS, response.GetState().GetState())
	r, err = resources.LoadRunnableFromDir(ctx, r.Dir())
	require.NoError(t, err)
	emitted := response.GetArtifacts()[0]
	pkg := preparedPackage(t, r, wire.GetIdentity(), prepared.GetBuild(), []*basev0.RunnableArtifact{{
		Kind: basev0.RunnableArtifact_NATIVE, Platform: runtime.GOOS + "/" + runtime.GOARCH,
		Reference: filepath.Base(emitted.GetPath()), Digest: "sha256:" + emitted.GetSha256(), Command: emitted.GetCommand(),
	}})
	require.NoError(t, corerunnable.VerifyPackage(pkg))
	require.True(t, proto.Equal(prepared.GetBuild(), pkg.GetBuild()))
	require.True(t, proto.Equal(wire.GetIdentity(), pkg.GetIdentity()))
	require.Len(t, pkg.GetArtifacts(), 1)
	require.NotEmpty(t, pkg.GetArtifacts()[0].GetCommand())
	installed := filepath.Join(root, "installed")
	unpack(t, response.GetArtifacts()[0].GetPath(), installed)
	result := invoke(t, installed, pkg.GetArtifacts()[0].GetCommand(), pkg, map[string]any{"text": "one two three"})
	require.Equal(t, 0, result.exit, result.stderr)
	require.Equal(t, 3, result.count(t),
		"the package must execute the prepared handler, so the refused retry discarded no snapshot")

	write(t, filepath.Join(root, "prepared", "runnable", "handler.py"), "def handle(context, input): return {\"count\": 888}\n")
	_, err = client.Package(ctx, &builderv0.PackageRequest{OutputDirectory: filepath.Join(root, "tampered")})
	require.Equal(t, codes.FailedPrecondition, status.Code(err))
	require.NoDirExists(t, filepath.Join(root, "tampered"), "reject changed snapshot before emitting any archive")

	wrong := proto.Clone(wire).(*basev0.RunnableLocation)
	wrong.Identity.Version = "9.0.0"
	_, err = client.Load(ctx, &builderv0.LoadRequest{Runnable: wrong})
	require.Equal(t, codes.InvalidArgument, status.Code(err))
	_, err = client.Package(ctx, &builderv0.PackageRequest{OutputDirectory: filepath.Join(root, "stale")})
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "a failed load must clear the previous snapshot")
}
