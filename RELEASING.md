# Releasing fieldkit

This guide is for a maintainer preparing a public `fieldkit` release. Its
post-read action is to decide whether an exact candidate is ready to enter the
operator-approved release workflow, or to stop with evidence that explains why
it is not. It does not authorize publishing, tagging, changing repository
settings, or creating a GitHub release.

## Release boundary

Only a signed tag, a GitHub release, and a package publication establish a
public release. A version in source, a built wheel, a passing local check, or a
successful dry run is not a release record. The public [release history](docs/releases.md)
is updated only after all three are independently verifiable.

The release version is the authoritative project version in `pyproject.toml`;
the workflow derives its required `v<version>` tag and package-index endpoint
from that value. Never reuse a version or overwrite an artifact. If a published
release needs correction, document it, yank it when it is harmful or unusable,
and publish a successor version.

## Before starting

Work from the exact clean revision you intend to release. Confirm that the
release notes are assembled from the current `changelog.d` fragments rather
than edited directly in `CHANGELOG.md`. Review the current compatibility,
support, governance, security, and repository-control policies before asking
for release approval.

The operator must separately approve every live action. In particular, do not
create the public repository, configure trusted publishing, dispatch a
TestPyPI run, create a tag, or publish a package merely because the commands
below are available.

## Create and assess one candidate

For the one-time clean-history public cutover, choose an output directory that
does not exist. This candidate check refuses to replace previous evidence and
binds its result to the current `HEAD`; its versioned export policy deliberately
names the planned initial `v1.0.0` tag. Later releases use the same sealed
workflow and authoritative project version, but do not repeat the clean-history
export procedure.

```console
PUBLIC_CANDIDATE_REVISION="$(git rev-parse HEAD)" \
PUBLIC_CANDIDATE_OUTPUT=build/public-candidate \
make release-check
```

The command builds the retained wheel and source distribution once, validates
the clean public export, creates the closed bundle and its checksums, records a
runtime SBOM and dependency receipt, and writes a JSON report beside the output
directory. A nonzero result is expected until every manual gate has
same-candidate evidence: package-name reservation, repository controls,
TestPyPI rehearsal, public contributor and user journeys, and cutover approval.

Validate the policy interpretation of that same report:

```console
uv run python scripts/check_release_governance.py \
  --candidate-report build/public-candidate/report.json
```

Exit 0 means the policy evidence is complete. Exit 1 means one or more
operator-owned controls are pending. Exit 2 or 3 means the candidate or policy
record is invalid. No exit status publishes anything.

## Verify the workflow before review

Validate the checked-in workflow contract locally before requesting review:

```console
make release-workflow-policy-check
```

The workflow has three deliberately separate paths:

| Mode | Trigger | Result | Required approval |
| --- | --- | --- | --- |
| Dry run | Protected default branch dispatch | Builds and validates one retained candidate | Workflow dispatch approval |
| TestPyPI | Protected default branch dispatch | Attests and publishes the retained candidate to TestPyPI | TestPyPI and operator approval |
| Production | Verified signed `v<project-version>` tag | Attests, publishes, verifies consumers, then creates the GitHub release | Final operator approval |

The authority jobs consume the retained bundle; they do not rebuild the package,
check out source, use a package-index token, or overwrite an existing version.
Configure the protected `release-approval` environment before a production tag,
and separate protected publisher environments and OIDC Trusted Publishers before
a TestPyPI or production run. A production tag must carry the protected
approval run and manifest digest in its signed annotation. The workflow then
acquires and revalidates that one retained approval artifact before it can
attest or publish; it never rebuilds source after tagging. Record the actual
remote settings before treating those controls as evidenced.

## Verify a published candidate

After an operator-approved publication, verify the exact retained candidate
from the intended package index. This observation is bounded and requires an
explicit live-index opt-in.

```console
uv run python -m scripts.check_release_consumer \
  --bundle build/public-candidate/bundle \
  --candidate-report build/public-candidate/report.json \
  --index-endpoint "https://pypi.org/pypi/fieldkit-cli/$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')/json" \
  --download-host files.pythonhosted.org \
  --download-dir build/public-candidate/downloads \
  --verify-downloads \
  --verify-install \
  --max-attempts 6 \
  --poll-interval-seconds 10 \
  --output build/public-candidate/consumer-evidence.json \
  --allow-live-index
```

The verifier requires both expected artifact names and digests, then runs the
fixed isolated wheel and source-distribution scenarios: version, help, packaged
assets, minimal initialization, diagnostics, and offline first success. It
never retries by uploading again. A timeout is pending, a wrong digest is
failed, and either state remains non-passing with retained evidence.

Manual documentation and community rehearsals require a separate clean checkout
of the public commit. The verifier rejects local changes, untracked files, a
checkout at another revision, a non-public repository API response, or a review
receipt that does not identify the exact successful Cutover verification run.
Credentialed examples remain non-passing until their same-candidate transcripts
and assertions are recorded against that checkout.

## Recovery and evidence

Stop on any missing, stale, mismatched, failed, or pending evidence. Preserve
the candidate report, closed bundle, checksums, SBOM, workflow run, consumer
evidence, promotion evidence, and relevant approval record. Do not repair a
release by rerunning publication for the same version.

For a defective published release, document the problem, yank it when
appropriate, and release a corrected successor version through the same flow.
Do not delete a public release or rewrite its evidence as a substitute for a
recovery record.

The checked-in [release-governance policy](docs/release-readiness/release-governance-policy.json)
is the machine-readable source of truth for candidate identity, roles, and
external-control evidence.
