# OpenShell task workers

This opt-in development path runs an immutable Git HEAD snapshot in a disposable
OpenShell sandbox. It does not replace the installed `fieldkit-cli` or change host
agent defaults. Review downloaded changes before applying them to your checkout.

The shared [Dockerfile](../.devcontainer/Dockerfile) supplies Python 3.11, the
locked `fieldkit-cli` development dependencies, and standard command-line tools.
It contains no coding-agent CLI.
The [devcontainer configuration](../.devcontainer/devcontainer.json) uses the
same recipe for interactive development. Devcontainers describe the environment;
OpenShell reads its own filesystem, network, and provider policies. Starting a
devcontainer alone does not activate OpenShell enforcement.

## Prepare the runtime

The tested host is Linux amd64 with rootless Podman and OpenShell 0.1.2. An
authenticated local OpenShell gateway must already be running. The bundled validation task needs no GPU. Build from this public checkout only:

```console
podman build -f .devcontainer/Dockerfile -t localhost/fieldkit-workbench:0.1 .
openshell workspace create --name fieldkit-cli
```

Workspace creation is a one-time operation. Allow additional space for build
layers and each temporary source image.
Other architectures require their own validation.

The devcontainer also targets rootless Podman: its `runArgs` map the host owner
to container UID/GID 1001 so the contributor mount remains writable. A Docker
backend needs an equivalent user-mapping configuration before use.

## Run a worker

Preview the selected snapshot and policy without starting a worker:

```console
uv run python scripts/openshell_workspace.py --dry-run -- python -m fieldkit version
```

Run a local validation command with no network provider:

```console
uv run python scripts/openshell_workspace.py -- python -m fieldkit version
```

The launcher runs an explicitly supplied command with a named timeout; the default
is 600 seconds. Choose a bounded task such as a validation command or a
project-owned script. No baseline coding agent is supplied or installed separately
by this path.

A model task requires a project-owned implementation and an explicitly selected
existing OpenShell provider via `--provider`. Its manifest can require a provider
and declare its type; its policy must grant the corresponding network access.
The default manifest requires no provider and the default policy denies network
access. The launcher does not read host agent logins, create providers, or infer a
model or task. Provider setup and credential handling belong to the project that
owns the task.

## Inputs, outputs, and failures

Git HEAD supplies the coding baseline. Dirty and untracked files are excluded;
`--input relative/file` explicitly adds a regular noncredential file. Commit a
source change first if it should be part of the baseline. Symlinks, private-key
files, native auth stores, and credential paths are rejected. This path filter
does not detect secrets embedded in ordinary source files: review your committed
source and selected inputs before launching.

Each run builds a uniquely tagged local image containing the chosen snapshot.
The image gives the worker its inputs before the policy starts. No live checkout,
host home directory, browser session, MCP credentials, or Docker socket is mounted.
The coding workspace is writable inside the sandbox; installed tools are read-only.
Network access is denied unless a provider explicitly grants it. Manual approval
mode means a denied request cannot silently broaden the policy.

Results go to a new owner-only ignored `.openshell/runs/<id>/` directory, or a project-relative
`--output` directory that must not exist. `input/` retains the baseline;
`result/` contains the downloaded workspace after link and file-type validation.
`stdout.log`, `stderr.log`, and `receipt.json` record the outcome. Treat all worker
output as untrusted. No result is automatically merged or executed on the host.

The launcher deletes its sandbox and temporary snapshot image after downloading
results. Explicitly selected providers remain owned by their configuring project.
SIGINT and SIGTERM defer
during bounded cleanup; forced process termination and host crashes can still
leave resources behind. A cleanup error is a failure, with the resource ID in the
error or receipt.

The provider preflight searches the first native listing page and fails closed
if its selected provider is absent. Workspaces with more than 100 providers need
pagination support before adopting this launcher.

Operational workspaces can consume the same tools image with their own explicit
task, read-only input policy, and approved provider. Their data and configuration
are owned by that workspace and never belong in this public build context. Model
adapters, agent frameworks, tool governance, remote deployment, evaluation, and
MCP business-system access are separate project integrations.
