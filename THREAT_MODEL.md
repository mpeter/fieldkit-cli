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
  the repository and never emit them in diagnostics.
- Workspace content and generated reports are high sensitivity; keep them under
  configured user-controlled roots.
- Runtime databases and logs are high sensitivity; treat them as local data and
  exclude them from commits.
- Release artifacts and provenance are high sensitivity; bind them to one
  verified source and export manifest.

## 3. Entry points and trust boundaries

- CLI input and configuration are local operator input; validate paths and
  structured values before use.
- Integration responses and imported text are external, untrusted data; bound
  content before prompts or writes and validate expected structure.
- Network requests cross to an external provider; use a named timeout and
  surface authentication separately.
- Dashboard requests originate in a local browser; bind only to loopback and
  require its configured authorization control for guarded actions.

## 4. Threats

- T1: untrusted integration text can influence a prompt or generated file.
  Content guards, size bounds, and explicit review boundaries mitigate this.
- T2: a credential or customer record can reach source control, output, or an
  issue. Public-tree scanning, external credential storage, and redacted
  diagnostics mitigate this.
- T3: user-derived path data can escape an approved root. Resolve and validate
  paths before writes.
- T4: an optional integration can be invoked without prerequisites. Use lazy
  imports, actionable profile guidance, and distinct authentication exits.
- T5: a release artifact can differ from the reviewed export. Use deterministic
  export, checksums, provenance, and same-candidate verification.

| Threat group | Required control |
| --- | --- |
| T1–T5 | Apply the matching control above before enabling or releasing the affected capability. |

## 5. Deprioritized

- Multi-user authorization is out of scope because fieldkit has no shared
  hosted service or tenant boundary.
- Public network listener hardening is out of scope because the supported
  dashboard boundary is loopback-only.
- Physical workstation compromise is out of scope because a compromised
  operating-system account already controls local application data.

## 6. Open questions

- Which optional integrations should gain stricter response schemas as their
  public contracts mature?
- Which local credential formats need additional permission checks on every
  supported platform?

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
- Report suspected vulnerabilities using the repository security policy rather
  than a public issue.
