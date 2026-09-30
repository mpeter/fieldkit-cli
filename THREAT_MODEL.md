# Threat model

fieldkit is a single-user, local-first command-line application. This document
helps operators decide which integrations to enable and helps contributors keep
the supported trust boundary intact.

## 1. System context

fieldkit stores workspace and runtime state locally. Optional commands connect
to services selected and configured by the operator. The optional dashboard is
restricted to loopback use; fieldkit is not a hosted or multi-tenant service.

## 2. Assets

- Integration credentials and tokens are high sensitivity; keep them outside
  the repository and do not deliberately include them in diagnostics. Unexpected
  exception text and tracebacks can still disclose sensitive values, so inspect
  diagnostics before sharing them.
- Workspace content and generated reports are high sensitivity; keep them under
  configured user-controlled roots.
- Runtime databases and logs are high sensitivity; treat them as local data and
  exclude them from commits.
- Prepared ingest checkpoints retain rendered notes, action items, and task
  classifications until completion. Treat checkpoints and backups as sensitive
  content. Ownership comments in notes, pursuits, and tasks contain integrity
  hashes, not encryption or authentication.
- Release artifacts and provenance are high sensitivity; bind them to one
  verified source and export manifest.

## 3. Entry points and trust boundaries

- CLI input and configuration are local operator input; validate paths and
  structured values before use.
- Integration responses and imported text are external, untrusted data; bound
  content before prompts or writes and validate expected structure.
- Network requests cross to an external provider; use a named timeout and
  surface authentication separately.
- Dashboard requests can come from any local HTTP client, not only a browser.
  The server binds only to loopback. Without a token, it rejects foreign Host
  headers and write requests; with a token, API and event requests require the
  bearer credential. A loopback listener is not a multi-user authorization
  boundary.
- Ingest revalidates saved checkpoints and existing Markdown before replay.
  Configured workspace and runtime roots may be symlink aliases resolved to
  canonical roots; replay rejects descendant redirects. File locks coordinate
  cooperating fieldkit writers, not arbitrary processes editing the same files.

## 4. Threats

- T1: untrusted integration text can influence a prompt or generated file.
  Content guards, size bounds, and explicit review boundaries mitigate this.
- T2: a credential or customer record can reach source control, output, or an
  issue. Public-tree scanning, external credential storage, and redacted
  diagnostics in selected domain paths mitigate this. Redaction is not universal:
  authentication diagnostics can include exception text, and unexpected failures
  retain full tracebacks and exception chains. Inspect and sanitize diagnostics
  before sharing them.
- T3: user-derived path data can escape an approved root. Resolve and validate
  paths before writes. Ingest replay accepts canonicalized root aliases but
  rejects child redirects, with publication-time rechecks. Hashed replay lock
  names remain beneath the canonical runtime root.
- T4: an optional integration can be invoked without prerequisites. Use lazy
  imports, actionable profile guidance, and distinct authentication exits.
- T5: a release artifact can differ from the reviewed export. Use deterministic
  export, checksums, provenance, and same-candidate verification.
- T6: interruption can leave only some ingest file effects published. Corrupt
  checkpoints or edited, copied, or malformed ownership comments could suppress,
  duplicate, or redirect later effects. Replay uses bounded strict checkpoint
  decoding, rejects duplicate JSON keys, and derives a canonical intent digest.
  Source and task-position identities bind decision fingerprints; structural
  Markdown validation rejects conflicting, duplicate, or unmarked ownership.
  Target-specific bounded locks, stable snapshots, and atomic create or replace
  protect individual writes. Database completion occurs only after all effects
  succeed; recovery is replay, not rollback or a cross-filesystem transaction.

| Threat group | Required control |
| --- | --- |
| T1–T6 | Apply the matching control above before enabling or releasing the affected capability. |

## 5. Deprioritized

- Multi-user authorization is out of scope because fieldkit has no shared
  hosted service or tenant boundary.
- Public network listener hardening is out of scope because the supported
  dashboard boundary is loopback-only.
- Physical workstation compromise is out of scope because a compromised
  operating-system account already controls local application data.
- A hostile process running as the same OS user can forge unkeyed ownership
  comments or race ancestor-directory renames. Protection against that principal
  is out of scope; replay assumes stable configured directory namespaces.
- Process-interruption recovery does not promise power-loss durability.

## 6. Control development

The [roadmap](ROADMAP.md#release-safety-and-future-delivery) records control
development and verification status. This threat model describes the trust
boundaries, not evidence that those controls have passed.

## 7. Provenance

Python dependencies are locked, workflows use pinned actions, and release
artifacts are checked against recorded digests. The public export policy
classifies every tracked path before publication.

## 8. Recommended mitigations

- Use a dedicated least-privilege credential for each integration where the
  provider supports it.
- Keep customer data and credentials out of terminals, issues, fixtures, and
  commits.
- Review an integration's guide before enabling it with production data.
- Preserve prepared checkpoints and ownership comments during recovery. Reconcile
  conflicts using the [pipeline recovery guide](docs/guides/pipeline-workflow.md)
  instead of deleting markers or lock files. Keep configured directory namespaces
  stable while writes are active.
- Report suspected vulnerabilities using the repository security policy rather
  than a public issue.
