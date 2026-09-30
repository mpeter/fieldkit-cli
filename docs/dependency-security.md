# Dependency security policy

fieldkit screens dependency changes for vulnerabilities and license policy violations. Pull-request
review, locked-graph vulnerability audits, and candidate license collection provide different
evidence. This page explains when each control runs and how to handle a finding.

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

The allowlist currently covers Apache-2.0, BSD-2-Clause, BSD-3-Clause, CNRI-Python, ISC, MIT, MIT-0,
MPL-2.0, and PSF-2.0. It reflects the licenses in fieldkit’s locked graph that were independently
screened for distribution. Adding an identifier is a policy change and requires maintainer review.

## What happens on a pull request

Dependabot keeps Python dependencies and GitHub Actions pins in separate weekly update groups. In
the public repository, a pull request touching the Python manifest, lock, dependency policy,
policy scripts, or dependency-review workflow starts GitHub dependency review with a read-only
token. The job compares the proposed graph with the base branch and does not install or execute
the proposed packages. Unrelated pull requests do not trigger this path-filtered workflow.

GitHub’s review action reports vulnerabilities and licenses. fieldkit then applies a supplemental
fail-closed check to the action's machine-readable output because the upstream action warns, but
does not fail, when it cannot identify a license. The retained report identifies the package URL and
the policy criterion that failed.

The supplemental checker uses the maintained `license-expression` parser to validate the whole
SPDX expression, known license and exception identifiers, and the license/exception positions in
`WITH`. Every identifier must be allowlisted, including every branch of `OR` and every `WITH`
exception. Parsing does not establish whether a particular exception legally applies to a license.
Malformed expressions and unavailable validators fail closed. These tools are development
dependencies and are excluded from fieldkit's runtime dependency graph.

Dependency review installs only the two hash-locked QA tool wheels listed in
`scripts/spdx-tool-requirements.txt`, read from the pull request's immutable base commit. Their
versions and hashes match `uv.lock`. A base commit without that reviewed tooling file cannot run
the new control and fails closed; the first published base must include it before dependent pull
requests can pass. The proposed dependency graph is never installed by this review job.

If a dependency check fails, update or remove the dependency when a safe version exists. If the
failure is license-related, confirm the package’s tagged upstream license before proposing any
policy change. Do not suppress a finding solely because a transitive dependency is familiar.

## Continuous locked-graph audit

Changes to the manifest, lock, dependency policy, policy scripts, or audit workflow on `main`, a
weekly schedule, and manual dispatch audit two scopes independently:

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
candidate's committed `uv.lock` with every optional extra enabled and the QA-only
`dev` dependency group excluded. It creates a fresh dependency environment from
that same all-extras runtime lock and records each installed distribution's exact-version PyPI URL
and declared license expression. A small collector runs with isolated Python and site startup
disabled (`-I -S`), reads metadata only from the fresh virtual environment's explicit site-package
directories, and never imports installed dependency or QA code. Installed `.pth` and
`sitecustomize.py` files therefore cannot alter this observation.
A separate environment resolves the candidate's locked development group to validate those
observations, so the SPDX parser cannot become an unexpected runtime distribution.

The SBOM and requirements exports must contain exactly the same cross-platform package inventory.
The QA validator evaluates the requirements' platform and Python markers using facts from the fresh
runtime interpreter; installed distributions must exactly match that selected target inventory. Missing
or unexpected installed packages fail, and a missing, extra, duplicate, malformed, or mismatched
SBOM component fails before license approval. Unsupported extra or dependency-group markers and
ambiguous target versions also fail rather than silently excluding packages. The retained report
binds the checked SBOM digest to the verified export revision and policy digest.

License receipt version 2 also binds the retained `runtime-license-observations.json` and
`platform-all-extras-requirements.txt` byte digests and the runtime's complete PEP 508 marker
environment. The QA interpreter projects requirements using those explicit runtime markers,
including the runtime's Python version. The closed bundle retains both inputs and verifies their
digests, observed package rows, and marker mapping against the receipt. Marker values and metadata
are bounded and screened for private data before they enter public evidence.

Generic `BSD` or `BSD License` metadata does not identify a clause count. Such packages need an
explicit SPDX declaration or an approved exact-version review exception. Every declared license
file must be present, bounded, readable UTF-8 and nonempty before metadata can be accepted.
The collector does not infer licenses from familiar text fragments or combine notices into an
invented `AND` expression. PEP 639 `AND`, `OR` and `WITH` declarations remain unchanged for strict
QA parsing. Unclassified text-only licenses remain unknown under the existing exception policy.

The license expressions in this report cover installed packages on that target. A cross-platform
SBOM component excluded by its marker has no observed license in this receipt. The scope label
describes the requested dependency profile; even `locked-all-groups-all-extras` does not prove
license approval for every platform from one interpreter. Release approval still needs fresh
evidence for the required platform and profile matrix; source validation alone is insufficient.

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
4. Run `make pr-check` using the reviewed base and clean committed candidate described in the
   [contributor guide](https://github.com/mpeter/fieldkit-cli/blob/main/CONTRIBUTING.md#verify-the-pull-request).
   Its supply-chain policy stage validates the checked-in policy and workflow wiring.
5. Require a passing dependency review; do not waive scanner execution failures.

Quality and CI policy checks do not execute a fresh vulnerability scan or collect candidate
licenses. Those receipts come from the dependency-review, locked-audit, and release-candidate
controls described above. Live public-repository settings and release enforcement remain
release prerequisites tracked in the
[roadmap](https://github.com/mpeter/fieldkit-cli/blob/main/ROADMAP.md).
