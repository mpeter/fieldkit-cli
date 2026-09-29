### Add opt-in OpenShell development workers

Developers can run committed source snapshots in disposable OpenShell workspaces
and review the returned artifacts. A shared devcontainer image supplies the
locked tools; project policies enforce filesystem and model-provider access.
The Codex adapter uses an existing subscription access token without copying
refresh credentials or changing host agent defaults. This preview path is
currently validated on Linux amd64 with rootless Podman and OpenShell 0.1.2.
