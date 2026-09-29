# OpenShell coding workers

This opt-in development path runs an immutable Git HEAD snapshot in a disposable
OpenShell sandbox. It does not replace the installed Fieldkit CLI or change host
agent defaults. Review downloaded changes before applying them to your checkout.

The shared [Dockerfile](../.devcontainer/Dockerfile) supplies Python 3.11, the
locked Fieldkit development dependencies, Codex 0.158.0, and Claude Code 2.1.284.
The [devcontainer configuration](../.devcontainer/devcontainer.json) uses the
same recipe for interactive development. Devcontainers describe the environment;
OpenShell reads its own filesystem, network, and provider policies. Starting a
devcontainer alone does not activate OpenShell enforcement.

## Prepare the runtime

The tested host is Linux amd64 with rootless Podman and OpenShell 0.1.2. An
authenticated local OpenShell gateway must already be running. No GPU is needed
for these hosted-model workers. Build from this public checkout only:

```console
podman build -f .devcontainer/Dockerfile -t localhost/fieldkit-workbench:0.1 .
openshell workspace create fieldkit-cli
openshell --workspace fieldkit-cli profile import .openshell/codex-profile.yaml
```

Workspace creation is a one-time operation. The image is about 1.5 GB; allow
additional space for build layers and each temporary source image. The current
recipe selects Linux x64 agent binaries. Other architectures require a recipe
change and their own validation.

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

For Codex, an existing host subscription login in `~/.codex/auth.json` is required:

```console
uv run python scripts/openshell_codex.py -- exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --model gpt-6-luna -c 'model_reasoning_effort="low"' "Inspect the source and propose a small improvement."
```

The explicit Codex flag delegates isolation to the outer OpenShell boundary.
The adapter exports only the current access token through an environment lookup
to a unique workspace provider. It never copies the host auth file, refresh
token, or real identity token into the worker. Codex receives an opaque bearer
reference plus nonsecret account routing metadata. A synthetic unsigned identity
token satisfies this pinned Codex version's local auth-file parser; it is not an
identity assertion and is never used to authorize the model request. The proxy
resolves the real bearer only for `chatgpt.com:443` from the Codex binary.

The adapter validates the exported provider profile before reading the host
login. It does not refresh or modify the host login. An expired access token
fails the run; refresh through your normal host login workflow before retrying.
Model workers are capped at 600 seconds.

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

Results go to a new ignored `.openshell/runs/<id>/` directory, or a project-relative
`--output` directory that must not exist. `input/` retains the baseline;
`result/` contains the downloaded workspace after link and file-type validation.
`stdout.log`, `stderr.log`, and `receipt.json` record the outcome. Treat all worker
output as untrusted. No result is automatically merged or executed on the host.

The launcher deletes its sandbox and temporary snapshot image after downloading
results. The adapter removes its provider afterward. SIGINT and SIGTERM defer
during bounded cleanup; forced process termination and host crashes can still
leave resources behind. A cleanup error is a failure, with the resource ID in the
error or receipt. Deleting a provider removes its runtime copy; it does not revoke
the underlying host OAuth access token.

The provider preflight searches the first native listing page and fails closed
if its selected provider is absent. Workspaces with more than 100 providers need
pagination support before adopting this launcher.

Operational Fieldkit workspaces consume the same image with a separate read-only
input policy and Vertex-only credential adapter. Their data and configuration are
owned by that workspace, and never belong in this public build context. Tool
governance, remote deployment, evaluation, and MCP business-system access remain
separate integrations.
