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

// Builder owns one loaded declaration and its prepared snapshot.
//
// That state is per-process, not per-caller: the lock serializes calls but
// cannot tell two callers apart, and Load replaces the state it finds. One
// connection therefore drives one Runnable at a time — interleaving Load and
// Package for two Runnables over the same agent packages whichever was loaded
// last. A caller that builds Runnables concurrently needs one agent process
// each, which is what manager.Load already gives it.
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
		return nil, status.Errorf(codes.Internal, "locate the Runnable in its workspace: %v", err)
	}
	actualWire, err := actual.Proto()
	if err != nil {
		return nil, status.Errorf(codes.Internal, "encode the Runnable location: %v", err)
	}
	if !proto.Equal(actualWire.GetIdentity(), id) {
		return nil, status.Error(codes.InvalidArgument, "Runnable release identity does not match the workspace declaration")
	}
	actualDir, err := filepath.EvalSymlinks(r.Dir())
	if err != nil {
		return nil, status.Errorf(codes.Internal, "resolve the declared Runnable directory: %v", err)
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
		return nil, status.Errorf(codes.Internal, "scaffold the Runnable: %v", err)
	}
	for _, input := range []string{generate.ProjectFile, generate.LockFile} {
		if !slices.Contains(r.Entrypoint.Inputs, input) {
			r.Entrypoint.Inputs = append(r.Entrypoint.Inputs, input)
		}
	}
	if err := r.Save(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "save the declaration: %v", err)
	}
	if err := generate.GenerateForRelease(r, r.Dir(), b.identity); err != nil {
		return nil, status.Errorf(codes.Internal, "generate the harness: %v", err)
	}
	return &builderv0.CreateResponse{State: &builderv0.CreateStatus{State: builderv0.CreateStatus_CREATED}}, nil
}

func (b *Builder) RunnableBuildInputs(ctx context.Context, request *builderv0.RunnableBuildInputsRequest) (*builderv0.RunnableBuildInputsResponse, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.runnable == nil {
		return nil, status.Error(codes.FailedPrecondition, "load a Runnable before building")
	}
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
	// Preparation populates the destination and refuses a directory that is
	// already populated, so an occupied one is rejected here rather than
	// surfacing from inside preparation as an untyped error.
	if err := requireEmpty(root); err != nil {
		return nil, err
	}
	if err := generate.GenerateForRelease(r, r.Dir(), b.identity); err != nil {
		return nil, status.Errorf(codes.Internal, "generate the harness: %v", err)
	}
	prepared, err := prepare.Prepare(ctx, r, r.Dir(), root)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "prepare the Runnable: %v", err)
	}
	build, err := pack.Build(r, filepath.Join(prepared.Root, prepare.SourceDirectory), prepared.Toolchain)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "measure the build inputs: %v", err)
	}
	// Only now, with a complete replacement in hand. A failed call leaves the
	// previous snapshot loaded: it still describes a whole tree on disk, and
	// Package re-measures it before using it, so it stays packageable.
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
		return nil, status.Errorf(codes.Internal, "re-measure the prepared snapshot: %v", err)
	}
	if !proto.Equal(currentBuild, b.build) {
		return nil, status.Error(codes.FailedPrecondition, "prepared build inputs changed before packaging")
	}
	evidence, err := pack.Native(b.runnable, b.agent, b.prepared, archive)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "emit the native archive: %v", err)
	}
	build := &basev0.RunnableBuild{}
	if err := protojson.Unmarshal(evidence.Build, build); err != nil {
		return nil, status.Errorf(codes.Internal, "decode the emitted build: %v", err)
	}
	if !proto.Equal(build, b.build) {
		return nil, status.Error(codes.FailedPrecondition, "prepared build inputs changed before packaging")
	}
	// The archive was just emitted from this snapshot. Its launch command and
	// metadata travel in the shared response, never in a language-specific CLI file.
	var artifacts []*basev0.RunnableArtifact
	if err := decodeArtifacts(evidence.Artifacts, &artifacts); err != nil {
		return nil, status.Errorf(codes.Internal, "decode the emitted artifacts: %v", err)
	}
	if len(artifacts) != 1 {
		return nil, status.Errorf(codes.Internal, "native packaging emitted %d artifacts, expected exactly one", len(artifacts))
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
//
// generate.Resolve does the resolution so this guard and generate.Confine
// cannot drift apart: nothing is created until the resolved path is accepted,
// so a rejected output leaves no directory inside the source or the snapshot.
func outputDirectory(directory, source string) (string, error) {
	if !filepath.IsAbs(directory) {
		return "", fmt.Errorf("output_directory must be absolute")
	}
	root, err := generate.Resolve(directory)
	if err != nil {
		return "", err
	}
	source, err = filepath.EvalSymlinks(source)
	if err != nil {
		return "", err
	}
	if root == source || generate.Within(root, source) || generate.Within(source, root) {
		return "", fmt.Errorf("output_directory must be separate from the Runnable source")
	}
	return root, nil
}

// requireEmpty refuses a destination that already holds something. Preparation
// populates the whole directory, so overwriting one is how a caller loses an
// earlier build, and the caller owns this path.
func requireEmpty(directory string) error {
	entries, err := os.ReadDir(directory)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return status.Errorf(codes.InvalidArgument, "inspect output_directory: %v", err)
	}
	if len(entries) != 0 {
		return status.Errorf(codes.AlreadyExists, "output_directory %s is not empty", directory)
	}
	return nil
}
