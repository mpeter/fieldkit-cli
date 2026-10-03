# Changelog

This file records changes in fieldkit's public release history. Development
history from before the public 1.0 baseline remains in the original private
repository and is intentionally not presented as a supported release history.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
fieldkit uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) for
public releases.

## [Unreleased]

No public changes yet.

## [1.0.2]

First published release. Versions 1.0.0 and 1.0.1 were prepared but never
published to PyPI; the 1.0.0 notes below describe the baseline this release
ships, and 1.0.1 was skipped because its TestPyPI version was consumed by a
rehearsal.

### Verify release environments before tagging

`scripts/verify_repository_settings.py` now checks the `testpypi` and `pypi`
deployment environments against `.github/repository-settings.json`. Both must
deploy only from `v*.*.*` tags, only `pypi` may require a reviewer, and any
undeclared environment is reported. The allowed-actions list no longer includes
`actions/create-github-app-token`, which no workflow uses.

---

### Update urllib3 to 2.8.0 for HTTP security fixes

Installations that include optional integrations, which reach urllib3 through
`requests` or `botocore`, now lock urllib3 2.8.0. The release fixes HTTPS proxy
TLS settings that destination settings could ignore or override
(GHSA-8988-9cw3-xx77), unbounded memory use while reading chunked responses
(GHSA-vxq7-64xx-v4gw), and an infinite loop in chunked Deflate streaming
(GHSA-gh4c-6fx4-qh6g).

If you reach an integration through an HTTPS proxy that depends on custom proxy
certificates, confirm the proxy connection after upgrading: proxy TLS settings
are now applied to the proxy rather than taken from the destination.

---

### Update oauthlib to 4.0.0 for Google sign-in

Installations with the Google integration now lock oauthlib 4.0.0, which
requests-oauthlib and google-auth-oauthlib use for the browser sign-in that
`fieldkit auth google` starts. The release's two breaking changes affect only
OAuth servers (JSONP removed from token revocation, and grant validation
reordered). The sign-in flow, its PKCE protection and token refresh continue to
work, and no action is needed.

---

### Refresh pinned GitHub Actions dependencies

Update the pinned setup-uv, CodeQL, and PyPI publishing actions to their reviewed versions while retaining exact commit pins and the existing workflow permissions and gates.

---

### Publish the first successor release as 1.0.2

The first release after 1.0.0 is published as `1.0.2`. Version `1.0.1` was
prepared but never released to PyPI; every change planned for it ships in
`1.0.2`. A `1.0.1` rehearsal package exists only on TestPyPI and is not a
supported release.

---

### Bind successor candidate identities

Bind stable successor release tags to the exported package version before builds, reject stale artifact and report identities, and retain exact source and digest checks through bundle and consumer verification. Historical first-release evidence and pending publication approvals remain unchanged.

---

### Release from one signed tag in a single workflow run

A signed version tag now drives the whole release. The workflow builds one
candidate, publishes and verifies it on TestPyPI, waits for the `pypi`
environment approval, then publishes the same files to PyPI and creates the
GitHub release. A workflow dispatch is a dry run that publishes nothing. After a
transient failure, **Re-run failed jobs** resumes against the original bundle.

---

### Retire the manual release-evidence ledger

`make release-check` no longer accepts a `RELEASE_MANUAL_EVIDENCE` ledger or
reports pending manual gates. It now builds the candidate, validates the public
export, and checks the report against the release-governance policy, exiting 0
when all three pass. The governance policy no longer carries external-control
records, and `check_release_governance.py` exits 0 for a matching candidate.
The release workflow now runs TestPyPI rehearsal and consumer verification
itself, and GitHub and PyPI enforce environment protection and Trusted
Publishing when it runs.

---

### Bind documentation rehearsals to the full-enforcement gate

The one-time `Cutover verification` workflow ran only on the repository's root
push, so no later commit could produce rehearsal evidence. It is removed.
Manual documentation rehearsals now cite a successful scheduled or dispatched
`Full enforcement` run of the exact public commit on `main`, and their contract
classification is renamed from `exact_release_cutover_proof` to
`public_rehearsal_proof` (verification ID `manual.public-rehearsal`).

---

### Restore the nightly complexity gate

Data-sync step building and meeting-note tab creation are split into smaller
helpers so the scheduled full-enforcement gate passes its committed complexity
baseline again. Commands behave as before.

---

### Remove unreachable developer-automation code

The web dashboard's operations view no longer shows a "developer" stage. That
stage queried a `fieldkit driver` command that the public CLI never exposed, so
it reported an error and suggested running a command that does not exist.

The unregistered `driver`, `autonomy`, and `health` command modules and their
domains are removed from the package, along with the `FIELDKIT_HARNESS_ROOT`
environment variable, which only they read.

### Remove the GitHub issue tracker and PR queue

`fieldkit issue` and the web dashboard's PR tab are removed. Both managed
fieldkit's own development repository rather than account work. The
`github_repo` configuration key is no longer read; existing configuration
files that set it still load. `fieldkit version --features` no longer reports
an `issues` section.

---

### Refresh integration and contributor dependencies

Refresh Google authentication, Vertex AI, LiteLLM, web-server, build, and contributor-tool dependencies. Align the isolated build backend and release-build profile on Hatchling 1.32.4.

---

### Preserve execution routing in installed-artifact smoke checks

Keep inherited proxy, CA, registry and execution-control settings when validating installed packages. Isolate application state, credentials and caches, disable dotenv and user-site imports, and retain the offline-network and behavioral assertions.

---

### Validate successor release versions without rewriting initial evidence

Prepare version 1.0.1 and validate stable successor package versions against the historical first-public-version floor. Keep the 1.0.0 policy and evidence unchanged. This metadata check does not authorize publication, prove a version unused, or replace candidate, provenance, approval, and consumer-verification gates.

---

### Run independent documentation artifact checks concurrently

Validate the wheel and source distribution in separate concurrent smoke environments. Keep both complete command contracts, all isolation and network checks, existing timeouts and deterministic evidence ordering while avoiding serial installation latency in the documentation gate.

---

### Add MEDDPICC evidence coaching to `/grill`

Add an on-demand MEDDPICC coaching reference to `/grill` with evidence red flags and question ideas for all eight elements. Coaching keeps exact live ClosePlan wording and native choices authoritative and does not introduce qualification scores or stage gates.

---

### Provision the secret scanner in full enforcement

Install and verify the policy-pinned Gitleaks executable before the scheduled full quality gate, so public-tree safety runs on a clean hosted runner.

---

### Clean up abandoned pytest session directories safely

Pytest now marks its temporary config directories with a held ownership lock. Later sessions remove only unlocked marked directories and warn if cleanup fails, while leaving live and legacy directories untouched.

---

### Validate pursuit stages before creating files

`pursuit create` now rejects unsupported stages before writing a pursuit file or directory, including in dry-run mode.

---

### Clarify how to verify a new pursuit

The post-create guidance now explains that health excludes pre-pipeline pursuits and points to read-only audit JSON for immediate verification.

---

### Keep pursuit previews read-only

`pursuit create --dry-run` no longer creates an empty `pursuits/` directory.

---

### Record the publisher that actually ran in release promotion evidence

Release promotion evidence now names `pypa/gh-action-pypi-publish` v1.14.2,
the revision the release workflow publishes with, instead of the earlier
v1.9.0. The release workflow policy fails when the workflow's attestation or
publishing action differs from the revision the evidence records, so the two
cannot drift apart again.

---

### Append meeting notes to Docs workbooks

`meeting note` now reads the workbook's current tab count before creating a new tab, and shows a bounded tool error when tab creation fails.

---

### Keep empty pursuit JSON output parseable

Empty audit, health, and forecast results now send their diagnostic to stderr while preserving exit status 3 and leaving JSON stdout empty.

---

### Use operation-specific data-sync timeouts

`fieldkit sync` now gives each subprocess step a named timeout. Dry runs display the selected ceiling, and a timed-out step reports its name and ceiling while the pipeline continues to a partial result.

---

### Fix release compatibility smoke checks

The artifact smoke check now uninstalls with `uv` when `uv` created the test
environment, which has no `pip`. The core compatibility jobs now install `uv`,
so they can verify the documented `uv tool install` path. Both defects had
blocked every compatibility run, and with it every release run.

---

### Publish PEP 740 attestations by explicit release policy

Releases published to TestPyPI or PyPI attach a PEP 740 publish attestation to
every wheel and sdist, signed for the release workflow's Trusted Publishing
identity. This was already the publishing action's default; the release
workflow now sets it explicitly, and the release workflow policy fails if
either publication job omits or disables it.

---

## [1.0.0]

Prepared as the first public release but never published; its contents ship in
1.0.2.

### Added

- A portable local-first CLI for structured pursuit, pipeline, task, watcher,
  brief, and packaged-skill workflows.
- Optional Salesforce, Google, AI, local-web, browser-auth, and
  organization-provided integration capabilities.
- A credential-free installation and first-success path.
- Supported core compatibility checks for Python 3.11 through 3.14 on Ubuntu
  and macOS, plus a Fedora/RHEL-family container profile.
- Public contribution, governance, support, security, privacy, compatibility,
  and release documentation.
- Reproducible wheel and source-distribution validation with privacy and package
  boundary checks.
- Fork-safe, least-privilege CI with stable required checks and retained test,
  compatibility, coverage, and quality evidence.
- Locked dependency auditing, license review, automated dependency updates,
  CodeQL analysis, and OpenSSF Scorecard reporting.
- Versioned repository-control policy for protected refs, immutable automation,
  merge behavior, security features, and cutover verification.
