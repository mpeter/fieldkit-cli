# Dependency security policy

fieldkit accepts dependency changes only when the locked package graph passes vulnerability and
license review. This page explains what a contributor should expect and what a maintainer must do
when a check reports a finding.

This policy is an engineering distribution screen for an Apache-2.0 project. Package metadata and
automated classifications can be incomplete; neither this policy nor a green check is legal advice.

## Blocking policy

- A newly introduced vulnerability of any published severity fails review. In scanner terms, the
  threshold is `low` or higher.
- Runtime, development, and unknown dependency scopes are reviewed on pull requests.
- Every reported license must resolve to the checked-in SPDX allowlist.
- Missing, `UNKNOWN`, or `NOASSERTION` license metadata fails unless an exact package version has a
  current, reviewed exception.
- A scanner error is a failed control. It is never reported as “zero vulnerabilities.”

The allowlist currently covers Apache-2.0, BSD-2-Clause, BSD-3-Clause, CNRI-Python, MIT, MIT-0,
MPL-2.0, and PSF-2.0. It reflects the licenses in fieldkit’s locked graph that were independently
screened for distribution. Adding an identifier is a policy change and requires maintainer review.

## What happens on a pull request

Dependabot keeps Python dependencies and GitHub Actions pins in separate weekly update groups. In
the public repository, a change to the Python manifest, lock, or dependency policy starts GitHub
dependency review with a read-only token. The job compares the proposed graph with the base branch
and does not install or execute the proposed packages.

GitHub’s review action reports vulnerabilities and licenses. fieldkit then applies a supplemental
fail-closed check to the action’s machine-readable output because the upstream action warns, but
does not fail, when it cannot identify a license. The retained report identifies the package URL and
the policy criterion that failed.

If a dependency check fails, update or remove the dependency when a safe version exists. If the
failure is license-related, confirm the package’s tagged upstream license before proposing any
policy change. Do not suppress a finding solely because a transitive dependency is familiar.

## Continuous locked-graph audit

Default-branch changes, a weekly schedule, and manual dispatch audit two scopes independently:

- the exact locked runtime graph with every published optional profile;
- the locked development dependency group.

The version-pinned scanner reads the lock without installing the project. Each run retains the raw
result plus normalized evidence containing the source revision, scanner version, scope, affected
package and version, advisory identifiers, and known fixed versions. Runtime and development
results stay separate so a development-only finding remains visible without being described as a
shipped exposure.

## Release-candidate license evidence

Pull-request review and vulnerability auditing do not establish license evidence for every package
in a release candidate. `make release-check` therefore exports a CycloneDX inventory from the
candidate's committed `uv.lock` for the default runtime profile: it excludes the QA-only `dev`
dependency group and does not enable optional extras. It creates a fresh isolated environment from
that same runtime lock and records each installed distribution's exact-version PyPI URL and SPDX
license expression. The observed package URLs must exactly equal the SBOM's component URLs and the
report binds both inventories to the verified export revision and policy digest, including the
SBOM's SHA-256 digest. Missing, extra, duplicate, unknown, malformed, or disallowed entries make
the candidate non-promotable.

## Requesting a license exception

An exception is appropriate only when authoritative upstream evidence identifies an allowlisted
license but package metadata does not. The policy entry must include all of the following:

1. An exact-version PyPI package URL.
2. The screened SPDX identifier.
3. A responsible GitHub maintainer.
4. A stable HTTPS link to license evidence for that exact release.
5. A factual rationale for the metadata mismatch.
6. An expiry date and an earlier review condition, such as any package-version change.

Changing the package version invalidates the exception automatically because the package URL no
longer matches. Expired exceptions make the policy gate fail before dependency review begins.

## Maintainer verification

Before approving a dependency or policy change, a maintainer should:

1. Read the dependency-review summary and retained policy report.
2. Confirm that the lock changes only as expected for the proposed update.
3. Inspect upstream advisory and license evidence directly.
4. Run `make quality` locally; the supply-chain policy stage must pass.
5. Require a passing dependency review; do not waive scanner execution failures.

The checks run in the repository's quality and CI workflows. See the
[roadmap](https://github.com/mpeter/fieldkit-cli/blob/main/ROADMAP.md) for public-repository
enforcement work that has not been completed.
