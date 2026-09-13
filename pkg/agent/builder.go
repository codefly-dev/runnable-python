package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"

	basev0 "github.com/codefly-dev/core/generated/go/codefly/base/v0"
	builderv0 "github.com/codefly-dev/core/generated/go/codefly/services/builder/v0"
	"github.com/codefly-dev/core/resources"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/codefly-dev/runnable-python/pkg/generate"
	"github.com/codefly-dev/runnable-python/pkg/pack"
	"github.com/codefly-dev/runnable-python/pkg/prepare"
)

// Builder owns one loaded declaration and its prepared snapshot. Calls are
// serialized so packaging cannot accidentally use another load's build state.
type Builder struct {
	builderv0.UnimplementedBuilderServer
	mu       sync.Mutex
	agent    *resources.Agent
	runnable *resources.Runnable
	identity *basev0.RunnableIdentity
	prepared *prepare.Prepared
	build    *basev0.RunnableBuild
}

func NewBuilder(agent *resources.Agent) *Builder { return &Builder{agent: agent} }

func (b *Builder) Load(ctx context.Context, request *builderv0.LoadRequest) (*builderv0.LoadResponse, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.runnable, b.identity, b.prepared, b.build = nil, nil, nil, nil
	if request.GetIdentity() != nil || request.GetRunnable() == nil {
		return nil, status.Error(codes.InvalidArgument, "a Runnable location is required; service identity is unsupported")
	}
	location := request.GetRunnable()
	if err := resources.Validate(location); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid Runnable location: %v", err)
	}
	workspace, err := resources.LoadWorkspaceFromDir(ctx, location.GetWorkspacePath())
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "load workspace: %v", err)
	}
	id := location.GetIdentity()
	r, err := workspace.LoadRunnableFromUnique(ctx, id.GetModule()+"/"+id.GetName())
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "load Runnable: %v", err)
	}
	actual, err := workspace.RunnableLocationOf(r)
	if err != nil {
		return nil, err
	}
	actualWire, err := actual.Proto()
	if err != nil {
		return nil, err
	}
	if !proto.Equal(actualWire.GetIdentity(), id) {
		return nil, status.Error(codes.InvalidArgument, "Runnable release identity does not match the workspace declaration")
	}
	actualDir, err := filepath.EvalSymlinks(r.Dir())
	if err != nil {
		return nil, err
	}
	requestedDir, err := filepath.EvalSymlinks(resources.RunnableLocationFromProto(location).Dir())
	if err != nil || actualDir != requestedDir {
		return nil, status.Error(codes.InvalidArgument, "Runnable location does not match the workspace declaration")
	}
	if r.Agent.Identifier() != b.agent.Identifier() || !r.Agent.IsRunnable() {
		return nil, status.Error(codes.InvalidArgument, "declaration pins a different Runnable agent")
	}
	if _, err := generate.HandlerModule(r.Entrypoint.Handler); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "%v", err)
	}
	b.runnable, b.identity = r, proto.Clone(id).(*basev0.RunnableIdentity)
	return &builderv0.LoadResponse{State: &builderv0.LoadStatus{State: builderv0.LoadStatus_READY}}, nil
}

func (b *Builder) Create(ctx context.Context, _ *builderv0.CreateRequest) (*builderv0.CreateResponse, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.runnable == nil {
		return nil, status.Error(codes.FailedPrecondition, "load a Runnable before creation")
	}
	if b.prepared != nil {
		return nil, status.Error(codes.FailedPrecondition, "reload before changing a prepared Runnable")
	}
	r := b.runnable
	if err := generate.Scaffold(r, r.Dir()); err != nil {
		return nil, err
	}
	for _, input := range []string{generate.ProjectFile, generate.LockFile} {
		if !slices.Contains(r.Entrypoint.Inputs, input) {
			r.Entrypoint.Inputs = append(r.Entrypoint.Inputs, input)
		}
	}
	if err := r.Save(ctx); err != nil {
		return nil, err
	}
	if err := generate.GenerateForRelease(r, r.Dir(), b.identity); err != nil {
		return nil, err
	}
	return &builderv0.CreateResponse{State: &builderv0.CreateStatus{State: builderv0.CreateStatus_CREATED}}, nil
}

func (b *Builder) RunnableBuildInputs(ctx context.Context, request *builderv0.RunnableBuildInputsRequest) (*builderv0.RunnableBuildInputsResponse, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.runnable == nil {
		return nil, status.Error(codes.FailedPrecondition, "load a Runnable before building")
	}
	b.prepared, b.build = nil, nil
	r := b.runnable
	if len(r.LibraryDependencies) != 0 {
		return nil, status.Error(codes.Unimplemented, "internal library dependency preparation is not implemented")
	}
	for _, input := range []string{generate.ProjectFile, generate.LockFile} {
		if !slices.Contains(r.Entrypoint.Inputs, input) {
			return nil, status.Errorf(codes.InvalidArgument, "entrypoint.inputs must declare %s", input)
		}
	}
	root, err := outputDirectory(request.GetOutputDirectory(), r.Dir())
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "%v", err)
	}
	if err := generate.GenerateForRelease(r, r.Dir(), b.identity); err != nil {
		return nil, err
	}
	prepared, err := prepare.Prepare(ctx, r, r.Dir(), root)
	if err != nil {
		return nil, err
	}
	build, err := pack.Build(r, filepath.Join(prepared.Root, prepare.SourceDirectory), prepared.Toolchain)
	if err != nil {
		return nil, err
	}
	b.prepared, b.build = prepared, build
	return &builderv0.RunnableBuildInputsResponse{
		State: &builderv0.RunnableBuildInputsStatus{State: builderv0.RunnableBuildInputsStatus_SUCCESS},
		Build: proto.Clone(build).(*basev0.RunnableBuild),
	}, nil
}

func (b *Builder) Package(_ context.Context, request *builderv0.PackageRequest) (*builderv0.PackageResponse, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.prepared == nil {
		return nil, status.Error(codes.FailedPrecondition, "prepare the loaded Runnable with RunnableBuildInputs before packaging")
	}
	if request.GetIncludeSbom() || request.GetSubject() != nil {
		return nil, status.Error(codes.Unimplemented, "Runnable packaging does not support SBOM or a replacement release subject")
	}
	if len(request.GetTargets()) > 1 {
		return nil, status.Error(codes.Unimplemented, "native packaging supports the current host target only")
	}
	for _, target := range request.GetTargets() {
		if target.GetOs() != runtime.GOOS || target.GetArchitecture() != runtime.GOARCH {
			return nil, status.Error(codes.Unimplemented, "native packaging supports the current host target only")
		}
	}
	root, err := outputDirectory(request.GetOutputDirectory(), b.runnable.Dir())
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "%v", err)
	}
	if _, err := outputDirectory(root, b.prepared.Root); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "%v", err)
	}
	name := request.GetArtifactName()
	if name == "" {
		name = b.runnable.Name + "-" + b.runnable.Version
	}
	if name == "." || name == ".." || strings.ContainsAny(name, "/\\") {
		return nil, status.Error(codes.InvalidArgument, "artifact_name must be a filename")
	}
	archive := filepath.Join(root, name+".tar.gz")
	if _, err := os.Lstat(archive); !os.IsNotExist(err) {
		return nil, status.Error(codes.AlreadyExists, "artifact output already exists or cannot be inspected")
	}
	currentBuild, err := pack.Build(b.runnable, filepath.Join(b.prepared.Root, prepare.SourceDirectory), b.prepared.Toolchain)
	if err != nil {
		return nil, err
	}
	if !proto.Equal(currentBuild, b.build) {
		return nil, status.Error(codes.FailedPrecondition, "prepared build inputs changed before packaging")
	}
	evidence, err := pack.Native(b.runnable, b.agent, b.prepared, archive)
	if err != nil {
		return nil, err
	}
	build := &basev0.RunnableBuild{}
	if err := protojson.Unmarshal(evidence.Build, build); err != nil {
		return nil, err
	}
	if !proto.Equal(build, b.build) {
		return nil, status.Error(codes.FailedPrecondition, "prepared build inputs changed before packaging")
	}
	// The archive was just emitted from this snapshot. Its launch command and
	// metadata travel in the shared response, never in a language-specific CLI file.
	var artifacts []*basev0.RunnableArtifact
	if err := decodeArtifacts(evidence.Artifacts, &artifacts); err != nil {
		return nil, err
	}
	artifact := artifacts[0]
	return &builderv0.PackageResponse{
		State: &builderv0.PackageStatus{State: builderv0.PackageStatus_SUCCESS},
		Artifacts: []*builderv0.PackageArtifact{{Kind: builderv0.PackageArtifact_ARCHIVE, Path: archive,
			Target: &builderv0.PackageTarget{Os: runtime.GOOS, Architecture: runtime.GOARCH},
			Sha256: strings.TrimPrefix(artifact.GetDigest(), "sha256:"), MediaType: "application/gzip", Command: artifact.GetCommand()}},
	}, nil
}

func decodeArtifacts(data []byte, artifacts *[]*basev0.RunnableArtifact) error {
	var documents []json.RawMessage
	if err := json.Unmarshal(data, &documents); err != nil {
		return err
	}
	for _, document := range documents {
		artifact := &basev0.RunnableArtifact{}
		if err := protojson.Unmarshal(document, artifact); err != nil {
			return err
		}
		*artifacts = append(*artifacts, artifact)
	}
	return nil
}

// outputDirectory never allows preparing inside the source, or writing through
// a symlink into it. This prevents a build from recursively copying its output.
func outputDirectory(directory, source string) (string, error) {
	if !filepath.IsAbs(directory) {
		return "", fmt.Errorf("output_directory must be absolute")
	}
	// Resolve the nearest existing ancestor before creating anything. Rejected
	// outputs must not leave directories inside the source or prepared snapshot.
	ancestor := filepath.Clean(directory)
	var missing []string
	var root string
	var err error
	for {
		root, err = filepath.EvalSymlinks(ancestor)
		if err == nil {
			break
		}
		if !os.IsNotExist(err) {
			return "", err
		}
		if _, statErr := os.Lstat(ancestor); statErr == nil {
			return "", fmt.Errorf("output contains a dangling symlink")
		}
		missing = append(missing, filepath.Base(ancestor))
		parent := filepath.Dir(ancestor)
		if parent == ancestor {
			return "", err
		}
		ancestor = parent
	}
	for i := len(missing) - 1; i >= 0; i-- {
		root = filepath.Join(root, missing[i])
	}
	source, err = filepath.EvalSymlinks(source)
	if err != nil {
		return "", err
	}
	if root == source || strings.HasPrefix(root, strings.TrimSuffix(source, string(filepath.Separator))+string(filepath.Separator)) || strings.HasPrefix(source, strings.TrimSuffix(root, string(filepath.Separator))+string(filepath.Separator)) {
		return "", fmt.Errorf("output_directory must be separate from the Runnable source")
	}
	return root, nil
}
