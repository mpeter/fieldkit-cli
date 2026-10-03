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
from that value. The metadata policy accepts stable SemVer versions at or after
the historical `first_public_version` of `1.0.0`; that field remains unchanged for
successors.
This check establishes version shape and the historical lower bound only. It does
not establish that a version is unused, newer than every published version, or
ready for promotion. Verify the intended version against both package-index and
GitHub release records before requesting publication approval.

Never reuse a version or overwrite an artifact. If a published
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

Choose an output directory that does not exist. The candidate check refuses to
replace previous evidence and binds its result to an exact committed revision.
The current public-tree and governance policies must select the same canonical
`v<version>` tag, matching the exported `pyproject.toml` before any package build.
The active candidate is `v1.0.2`; the historical `first_public_version` stays
`1.0.0`, and prior policies and evidence remain in their original Git history.
Version `1.0.1` was never published to PyPI: its TestPyPI version was consumed
by a rehearsal of an earlier commit, so a `1.0.1` release run would stop at its
TestPyPI upload.
Do not relabel a previous report or bundle as a successor; every release run
builds and verifies its own candidate.
An export is a content snapshot; it does not publish or rewrite Git history.

The sealed candidate command reads committed Git objects. Builds and tests from
an uncommitted working tree are local preparation evidence only; they cannot be
represented as a sealed candidate for its unchanged `HEAD`.

```console
PUBLIC_CANDIDATE_REVISION="$(git rev-parse HEAD)" \
PUBLIC_CANDIDATE_OUTPUT=build/public-candidate \
make release-check
```

The command builds and retains one wheel and source-distribution pair, compares
a second controlled build for reproducibility, validates the clean public
export, creates the closed bundle and its checksums, records a
runtime SBOM and dependency receipt, checks the report against the
release-governance policy, and writes a JSON result beside the output
directory. Exit 0 means the candidate builds, exports, and matches the policy;
exit 2 means it does not, and the result names the first failure. This is a
local preparation check: the release workflow builds and verifies its own
candidate from the tag, and the TestPyPI rehearsal and consumer checks run
inside that workflow.

Validate the policy interpretation of that same report:

```console
uv run python scripts/check_release_governance.py \
  --candidate-report build/public-candidate/report.json
```

Exit 0 means the report was built for the policy's repository, package, and
planned tag. Exit 3 means the candidate report or the policy is invalid. No exit
status publishes anything.

## Verify the workflow before review

Validate the checked-in workflow contract locally before requesting review:

```console
make release-workflow-policy-check
```

The workflow has two paths:

| Mode | Trigger | Result | Required approval |
| --- | --- | --- | --- |
| Dry run | Workflow dispatch from the protected default branch | Runs compatibility, then builds and validates one retained candidate | None; it attests and publishes nothing |
| Release | Verified signed `v<project-version>` tag on a default-branch commit | Builds one candidate, attests it, publishes and verifies it on TestPyPI, then publishes and verifies the same bytes on PyPI and creates the GitHub release | The `pypi` environment review |

The signed tag authorizes building, rehearsing, and publishing the exact
default-branch commit it names. The workflow checks GitHub's signature
verification for the tag object and refuses a tag whose commit is not already
on the default branch, so the required pull-request checks have run on it. One
run then builds one sealed candidate, and every later job verifies and uses
those bytes: TestPyPI and PyPI receive identical files, and the TestPyPI
consumer check must pass before the production job can start. The `pypi`
environment review is the single human approval; review the run's bundle digest
and its TestPyPI consumer evidence before approving it.

The authority jobs consume the retained bundle; they do not rebuild the package,
check out source, use a package-index token, or overwrite an existing version.
Before the first tag, configure the `testpypi` and `pypi` environments with an
OIDC Trusted Publisher for `release.yml`, restrict their deployment refs to
`v*.*.*` tags, and require a reviewer on `pypi`. An environment limited to
protected branches rejects a tag-triggered job. Verify the live settings,
including both environments, against `.github/repository-settings.json` before
tagging:

```console
uv run python scripts/verify_repository_settings.py \
  "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
  --phase post-cutover --expected-revision "$(git rev-parse origin/main)"
```

The verifier reads settings only. It fails when an environment can deploy from
branches, carries a tag pattern other than `v*.*.*`, adds a reviewer to
`testpypi`, lacks one on `pypi`, enables self-review prevention, or when an
undeclared environment exists. PyPI exposes no API for Trusted Publisher
configuration; a wrong publisher fails the publish job before any upload, and
**Re-run failed jobs** resumes after it is corrected.

Both publication jobs upload with `pypa/gh-action-pypi-publish` and set
`attestations: true`, so every wheel and sdist on TestPyPI or PyPI carries a
PEP 740 publish attestation signed for the release workflow's Trusted
Publishing identity, in addition to the GitHub artifact attestation from the
`attest` job. The action runs its prebuilt container image
`ghcr.io/pypa/gh-action-pypi-publish`, pulled by a tag equal to the pinned
action commit rather than by image digest.

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
receipt that does not identify a successful scheduled or dispatched Full
enforcement run of that exact commit on `main`.
Credentialed examples remain non-passing until their same-candidate transcripts
and assertions are recorded against that checkout.

## Recovery and evidence

Stop on any missing, stale, mismatched, failed, or pending evidence. Preserve
the candidate report, closed bundle, checksums, SBOM, workflow run, consumer
evidence, and promotion evidence.

The candidate artifact name is fixed for the whole run, so use **Re-run failed
jobs** to resume after a transient failure: the re-run reuses the same bundle,
and a consumer check that timed out verifies again without uploading. **Re-run
all jobs** cannot replace a candidate: its first run-scoped upload fails because
an artifact with that name already exists in the run. If an index accepted some or all files and the run cannot
finish against that bundle, the version is spent. Publish a successor version
instead of repairing the release by uploading again. Retained artifacts expire
after 90 days, so finish or abandon a release run within that window.

A re-run uses the workflow file at the tagged commit, so it cannot pick up a
workflow fix. If the run fails after PyPI accepted and verified the files, finish
the GitHub release by hand from that run's candidate artifact. Set `RUN_ID` to
the failed run and `TAG` to its tag, then download the candidate, check its
checksums, compare the distribution digests with PyPI, and create the release:

```console
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
SHA="$(gh run view "$RUN_ID" --repo "$REPO" --json headSha --jq .headSha)"
gh run download "$RUN_ID" --repo "$REPO" \
  --name "release-candidate-$RUN_ID-$SHA" --dir build/recovered
(cd build/recovered/candidate/bundle && sha256sum --strict --check SHA256SUMS)
(cd build/recovered/candidate/bundle && sha256sum ./*.whl ./*.tar.gz)
curl --fail --silent "https://pypi.org/pypi/fieldkit-cli/${TAG#v}/json" \
  | jq -r '.urls[] | "\(.digests.sha256)  \(.filename)"'
gh release create "$TAG" build/recovered/candidate/bundle/*.whl \
  build/recovered/candidate/bundle/*.tar.gz \
  --repo "$REPO" --verify-tag --generate-notes
```

Create the release only when every checksum reports `OK` and both digest lists
name the same files with the same values. The run's promotion evidence still
records the failure; that record is kept as part of the release history.

For a defective published release, document the problem, yank it when
appropriate, and release a corrected successor version through the same flow.
Do not delete a public release or rewrite its evidence as a substitute for a
recovery record.

The checked-in [release-governance policy](docs/release-readiness/release-governance-policy.json)
is the machine-readable source of truth for candidate identity, roles, and
support commitments. Environment protection and Trusted Publishing are enforced
by GitHub and PyPI when the release workflow runs; configure them as described
above before the first tag.
