// Package pack produces the native package of a prepared runnable and the
// build evidence that identifies it.
//
// The evidence is what a caller needs to assemble a core RunnablePackage
// without inferring anything from the language or the template: every field of
// RunnableBuild and the NATIVE artifact, measured from the bytes that were
// built. codefly-dev/core#472 is freezing how this crosses the agent/CLI seam;
// until it does, the agent writes this document and the CLI reads it.
package pack

import (
	"archive/tar"
	"compress/gzip"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"time"

	basev0 "github.com/codefly-dev/core/generated/go/codefly/base/v0"
	"github.com/codefly-dev/core/resources"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/codefly-dev/runnable-python/pkg/generate"
	"github.com/codefly-dev/runnable-python/pkg/harness"
	"github.com/codefly-dev/runnable-python/pkg/prepare"
)

// EvidenceSchema versions the document the agent writes for the CLI.
const EvidenceSchema = "codefly.runnable-build-evidence/v1"

// EvidenceFile is the document's name inside the build output directory.
const EvidenceFile = "runnable-build.json"

// Evidence is the agent's half of one built release.
type Evidence struct {
	Schema    string          `json:"schema"`
	Identity  json.RawMessage `json:"identity"`
	Agent     json.RawMessage `json:"agent"`
	Build     json.RawMessage `json:"build"`
	Artifacts json.RawMessage `json:"artifacts"`
}

// Native archives a prepared tree and returns the evidence describing it.
// The archive is deterministic: the same prepared bytes produce the same
// archive digest, whichever machine built them.
func Native(
	runnable *resources.Runnable,
	agent *resources.Agent,
	source string,
	prepared *prepare.Prepared,
	archive string,
) (*Evidence, error) {
	if err := writeArchive(prepared.Root, archive); err != nil {
		return nil, err
	}
	digest, err := digestFile(archive)
	if err != nil {
		return nil, err
	}
	build, err := Build(runnable, source, prepared.Toolchain)
	if err != nil {
		return nil, err
	}
	artifact := &basev0.RunnableArtifact{
		Kind:      basev0.RunnableArtifact_NATIVE,
		Platform:  runtime.GOOS + "/" + runtime.GOARCH,
		Reference: filepath.Base(archive),
		Digest:    digest,
		Command:   prepared.Command,
	}
	return evidenceOf(runnable, agent, build, []*basev0.RunnableArtifact{artifact})
}

// Build measures every input that changes the package bytes: the handler, the
// declared build inputs, the generated harness, the interpreter that was
// prepared, and the configuration the invocation is bound to.
func Build(runnable *resources.Runnable, source string, toolchain string) (*basev0.RunnableBuild, error) {
	handler, err := digestInput(source, runnable.Entrypoint.Handler)
	if err != nil {
		return nil, err
	}
	inputs := make([]*basev0.RunnableInputDigest, 0, len(runnable.Entrypoint.Inputs))
	for _, input := range runnable.Entrypoint.Inputs {
		digest, err := digestInput(source, input)
		if err != nil {
			return nil, err
		}
		inputs = append(inputs, digest)
	}
	sort.Slice(inputs, func(i, j int) bool { return inputs[i].Path < inputs[j].Path })

	harnessDigest, err := harness.Digest()
	if err != nil {
		return nil, err
	}
	configuration, err := ConfigurationDigest(runnable)
	if err != nil {
		return nil, err
	}
	return &basev0.RunnableBuild{
		Handler:             handler,
		Inputs:              inputs,
		HarnessDigest:       harnessDigest,
		Toolchain:           toolchain,
		ConfigurationDigest: configuration,
	}, nil
}

// ConfigurationDigest pins the effective configuration of an invocation: the
// names of the workspace configurations it is injected with and the service
// dependencies it may reach. Only names and coordinates are hashed — a
// credential value never reaches a descriptor, a log or a payload.
func ConfigurationDigest(runnable *resources.Runnable) (string, error) {
	type dependency struct {
		Name      string   `json:"name"`
		Module    string   `json:"module"`
		Kind      string   `json:"kind"`
		Endpoints []string `json:"endpoints"`
	}
	effective := struct {
		Configurations []string       `json:"workspace-configurations"`
		Dependencies   []dependency   `json:"service-dependencies"`
		Execution      map[string]any `json:"execution"`
		Spec           map[string]any `json:"spec,omitempty"`
		Protocol       string         `json:"protocol"`
	}{
		Configurations: append([]string{}, runnable.WorkspaceConfigurationDependencies...),
		Execution: map[string]any{
			"timeout":          runnable.Execution.Timeout,
			"cancellation":     string(runnable.Execution.Cancellation),
			"recovery":         string(runnable.Execution.Recovery),
			"concurrency":      runnable.Execution.Concurrency,
			"max-input-bytes":  runnable.Execution.MaxInputBytes(),
			"max-output-bytes": runnable.Execution.MaxOutputBytes(),
		},
		Spec:     runnable.Spec,
		Protocol: runnable.Contract.Protocol,
	}
	sort.Strings(effective.Configurations)
	for _, declared := range runnable.ServiceDependencies {
		endpoints := make([]string, 0, len(declared.Endpoints))
		for _, endpoint := range declared.Endpoints {
			endpoints = append(endpoints, endpoint.Name)
		}
		sort.Strings(endpoints)
		effective.Dependencies = append(effective.Dependencies, dependency{
			Name:      declared.Name,
			Module:    declared.Module,
			Kind:      string(declared.Kind),
			Endpoints: endpoints,
		})
	}
	sort.Slice(effective.Dependencies, func(i, j int) bool {
		left, right := effective.Dependencies[i], effective.Dependencies[j]
		if left.Module != right.Module {
			return left.Module < right.Module
		}
		return left.Name < right.Name
	})
	encoded, err := json.Marshal(effective)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("sha256:%x", sha256.Sum256(encoded)), nil
}

func evidenceOf(
	runnable *resources.Runnable,
	agent *resources.Agent,
	build *basev0.RunnableBuild,
	artifacts []*basev0.RunnableArtifact,
) (*Evidence, error) {
	identity, err := marshal(&basev0.RunnableIdentity{
		Name:      runnable.Name,
		Module:    runnable.Module(),
		Workspace: runnable.Identity().Workspace,
		Version:   runnable.Version,
	})
	if err != nil {
		return nil, err
	}
	agentProto, err := agent.Proto()
	if err != nil {
		return nil, err
	}
	agentDocument, err := marshal(agentProto)
	if err != nil {
		return nil, err
	}
	buildDocument, err := marshal(build)
	if err != nil {
		return nil, err
	}
	encoded := make([]json.RawMessage, 0, len(artifacts))
	for _, artifact := range artifacts {
		document, err := marshal(artifact)
		if err != nil {
			return nil, err
		}
		encoded = append(encoded, document)
	}
	list, err := json.Marshal(encoded)
	if err != nil {
		return nil, err
	}
	return &Evidence{
		Schema:    EvidenceSchema,
		Identity:  identity,
		Agent:     agentDocument,
		Build:     buildDocument,
		Artifacts: list,
	}, nil
}

// Write renders the evidence next to the artifacts it describes.
func (e *Evidence) Write(directory string) (string, error) {
	encoded, err := json.MarshalIndent(e, "", "  ")
	if err != nil {
		return "", err
	}
	path := filepath.Join(directory, EvidenceFile)
	return path, os.WriteFile(path, append(encoded, '\n'), 0o644)
}

// marshal renders a core message as proto3 JSON with sorted keys, the form
// core digests a descriptor in.
func marshal(message proto.Message) (json.RawMessage, error) {
	encoded, err := protojson.Marshal(message)
	if err != nil {
		return nil, err
	}
	var canonical any
	if err := json.Unmarshal(encoded, &canonical); err != nil {
		return nil, err
	}
	return json.Marshal(canonical)
}

func digestInput(source string, relative string) (*basev0.RunnableInputDigest, error) {
	path, err := generate.Confine(source, relative)
	if err != nil {
		return nil, err
	}
	digest, err := digestFile(path)
	if err != nil {
		return nil, err
	}
	return &basev0.RunnableInputDigest{Path: filepath.ToSlash(relative), Digest: digest}, nil
}

func digestFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return fmt.Sprintf("sha256:%x", digest.Sum(nil)), nil
}

// writeArchive tars the prepared tree with fixed metadata so two builds of the
// same bytes have the same artifact digest.
func writeArchive(root string, archive string) error {
	if err := os.MkdirAll(filepath.Dir(archive), 0o755); err != nil {
		return err
	}
	file, err := os.Create(archive)
	if err != nil {
		return err
	}
	defer file.Close()
	compressed, err := gzip.NewWriterLevel(file, gzip.BestCompression)
	if err != nil {
		return err
	}
	writer := tar.NewWriter(compressed)

	var paths []string
	err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path == root {
			return nil
		}
		paths = append(paths, path)
		return nil
	})
	if err != nil {
		return err
	}
	sort.Strings(paths)

	for _, path := range paths {
		if err := appendEntry(writer, root, path); err != nil {
			return err
		}
	}
	if err := writer.Close(); err != nil {
		return err
	}
	if err := compressed.Close(); err != nil {
		return err
	}
	return nil
}

func appendEntry(writer *tar.Writer, root string, path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	link := ""
	if info.Mode()&os.ModeSymlink != 0 {
		if link, err = os.Readlink(path); err != nil {
			return err
		}
	}
	header, err := tar.FileInfoHeader(info, link)
	if err != nil {
		return err
	}
	relative, err := filepath.Rel(root, path)
	if err != nil {
		return err
	}
	header.Name = filepath.ToSlash(relative)
	header.ModTime = time.Unix(0, 0)
	header.AccessTime = time.Unix(0, 0)
	header.ChangeTime = time.Unix(0, 0)
	header.Uid, header.Gid = 0, 0
	header.Uname, header.Gname = "", ""
	if err := writer.WriteHeader(header); err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return nil
	}
	content, err := os.Open(path)
	if err != nil {
		return err
	}
	defer content.Close()
	_, err = io.Copy(writer, content)
	return err
}
