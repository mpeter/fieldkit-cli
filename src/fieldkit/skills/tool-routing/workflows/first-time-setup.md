# First-time authentication

Authentication changes local credential state and may open a browser. Explain
the account, service, requested scopes, credential location, and verification
step before starting setup. Never print or export secrets as routine diagnosis.

## fieldkit integrations

Inspect the selected fieldkit leaf's `--help` and public integration guide. Use
only its documented authentication command and credential locations. After
setup, run the narrowest credential-safe health or read operation for that
integration before attempting a write.

## Google Workspace CLI

`gws` is separately installed. Start with `gws auth status`. When the operator
chooses to log in, limit requested scopes with
`gws auth login --services SERVICE` where the installed CLI supports that flag.
Use `gws auth setup` only when OAuth client configuration itself is absent and
the operator intends to configure a Google Cloud project and client.

After login, confirm the authenticated account and run a bounded read for the
required service. Authentication success does not authorize a later mutation.

## GitHub and other optional clients

Use `gh auth status` before authenticated GitHub operations. For any other
optional client, consult its installed help and credential-safe status command.
If no public setup documentation is available, stop and ask the operator rather
than inferring a token source or copying credentials between tools.

Do not store secrets in a repository, shell history, issue, pull request,
diagnostic bundle, or generated evidence. A configured tool remains unavailable
for release proof until the intended identity and a bounded operation are
verified.
