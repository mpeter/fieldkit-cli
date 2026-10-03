---
status: Proposed
applies_to: fieldkit-cli
---

# Use a shared development image with opt-in OpenShell workers

Date: 2026-09-29

## Context

Coding and operational agents need the same `fieldkit-cli` tools but different data
and provider boundaries. A development container supplies an environment without
the enforced egress and credential controls needed for autonomous workers.

## Decision

Build one OCI image from the public CLI's pinned development recipe. Devcontainers
and OpenShell consume that image independently. Keep runtime policies, selected
inputs, explicit provider selection, and result handling project-owned. The image
supplies development tools, with no coding-agent CLI or model framework. Workers
run explicit bounded commands. Coding tasks use
Git HEAD snapshots; operational workspaces use explicitly selected read-only
files and their approved model provider.

Workers are opt-in. They return untrusted artifacts for review, never apply host
changes automatically, and remove their own sandbox and source image.
The developer-preview integration is local tooling, not a replacement for the
`fieldkit-cli` product or a production agent control plane.

## Alternatives

Using devcontainers alone leaves egress and bearer credentials outside an
OpenShell policy. Making OpenShell mandatory for every interactive session would
also change existing browser and MCP workflows before those paths are integrated
and verified. Separate image recipes per workspace would duplicate dependency
ownership and allow the environments to drift.

## Consequences

The environment has one owner, while each project retains its security boundary.
Linux amd64 is the tested scope. Model tasks and credential adapters remain
project-owned; this public integration supplies neither a baseline coding agent
nor a model framework. Local snapshot images cost build time and disk space.
Selected providers remain owned by the configuring project. Forced termination
can leave resources requiring
manual cleanup. The repository's `.openshell/README.md` worker guide documents operation
and the distinction between a devcontainer and the governed runtime.
