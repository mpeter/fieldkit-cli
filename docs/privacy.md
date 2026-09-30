# Local data and privacy

fieldkit is a single-user local application. It has no hosted fieldkit service and no shared
multi-tenant database. Your operating-system account is the application boundary.

## What stays local

The workspace, generated Markdown, configuration, caches, logs, and runtime databases are stored in
the configured workspace, data, and user-configuration roots. Disposable harness worktrees use
`FIELDKIT_HARNESS_ROOT`, `$XDG_CACHE_HOME/fieldkit`, or `~/.cache/fieldkit`. None of these locations
is bundled with the Python package or source repository. Keep configured roots outside your checkout.

Treat these files as sensitive. Depending on the integrations you enable, they may contain customer
names, opportunity data, email content, meeting notes, prompts, and model responses. Do not commit
them to a public repository or attach them to an issue without redaction.

Transcript ingest retains the rendered note, action items, and task classifications
in local `pipeline.db` checkpoints until all required writes complete. Reprocessing
an existing note separately retains its exact replacement text, destination, file
mode, original content digest, and artifact identity until the replacement is
verified and its database version is updated. Interrupted work keeps this content
for replay; deleting a checkpoint can destroy its saved decisions.

Successful completion removes the corresponding checkpoint, but this is not a
secure-erasure guarantee. Continue treating the database, its SQLite sidecar files,
and backups as sensitive. Note, pursuit, and task ownership comments contain
identity and content digests, not encryption. Keep them with their files so
retries can recognize edited output. Follow the [pipeline recovery
guide](guides/pipeline-workflow.md) to resolve interrupted work without discarding
its recovery records.

The verified SQLite snapshot reader does not open the original database through
SQLite. It copies a verified, quiescent database while holding a shared lock that permits
other readers but excludes updates. The copy is limited to 8 GiB, created as a
mode-`0600` file inside a mode-`0700` system temporary directory, opened as an
immutable read-only database, and unlinked before the command receives the open
connection. Active writers are reported as retryable; rollback journals or a
snapshot that cannot be verified are rejected as invalid data. A source with
multiple hard links or a WAL-mode database header is also rejected: an alias can
place live WAL files under another basename that fieldkit cannot safely prove
quiescent. Use the owning application's supported backup or export to produce a
single-link, rollback-journal working copy before reading it with fieldkit.

These are guarantees of that reader, not of every command labeled read-only.
Optional automation spend accounting opens its external session database directly
in SQLite read-only mode; it does not provide the same verified-snapshot contract.

Handled failures, timeouts, controller loss, and termination remove partial
copies. An abrupt process death after a complete copy is written but before it is
unlinked can still leave that bounded private file for the operating system or
operator's temporary-file cleanup. This containment is not a secure-erasure
guarantee.

## What can leave the machine

Commands using external APIs, provider clients, or authentication helpers can send requests
beyond the workstation. The table summarizes common destinations; authentication may also contact provider login and token endpoints.

| Capability | Typical data destination |
| --- | --- |
| Salesforce | The Salesforce organization you configure |
| Google | Gmail, Drive, Docs, or other enabled Google APIs |
| LLM | The configured supported model provider |
| MCP-backed tools | The configured MCP endpoint and its downstream services; Backstory authorization registers the People.ai MCP endpoint through MCPJungle |
| GitHub-backed issue commands | The GitHub repository configured for fieldkit issues; issue titles, bodies, labels, and status changes |
| Organization-provided services | The service endpoint configured by your operator; for example, a ShadowBot prompt, thread identifier, and returned response |

The base installation and minimal first-success workflow require none of these integrations after
the package is installed.

## Credentials

Store credentials outside the source tree. fieldkit uses provider-supported tokens, local credential
stores, or application-default credentials according to the selected integration. Never paste a
credential, session cookie, customer record, full email, or unredacted diagnostic bundle into a
public issue.

The `auth sf --sid-file` and `auth shadowbot --refresh-token-file` options accept
only non-empty UTF-8 regular files with owner-only permissions (for example, mode `0600`).
Their limit is 8,192 bytes, including surrounding whitespace; symlinks and pipes
are rejected. Input-reader errors omit the supplied path and file contents.

Use the repository security policy for suspected vulnerabilities. Use the support path for setup
questions that do not contain sensitive data.
