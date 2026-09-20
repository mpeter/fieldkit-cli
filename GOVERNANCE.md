# fieldkit governance

fieldkit begins public life as a solo-maintainer project. This document describes the authority that
exists now and how responsibility can grow without pretending a committee or support organization
already exists.

## Roles

- **Users** run fieldkit and provide feedback.
- **Contributors** submit issues, documentation, tests, code, reviews, or community help.
- **Triagers** are trusted contributors who may label, reproduce, clarify, and organize issues.
- **Maintainers** review and merge changes, set project direction, manage releases and repository
  settings, moderate project spaces under the contribution and platform rules, and handle private
  security reports.

The current maintainer is [@mpeter](https://github.com/mpeter). The maintainer is the final decision
maker while the project has one maintainer.

## Decisions

Routine fixes, documentation, dependency maintenance, and small improvements are decided through
normal pull-request review. New commands, breaking behavior, persisted schemas, security boundaries,
large dependencies, governance changes, and broad refactors begin with a public issue or proposal.

The project seeks rough consensus by explaining the problem, alternatives, consequences, and
evidence. Consensus does not require unanimity. When a decision remains contested, the maintainer
makes and records the decision, including enough rationale for later review.

## Review and merge

All changes to the default branch arrive through pull requests and required checks. Authors may
respond to reviews and revise their work; merge authority remains with maintainers. Security,
governance, release, and quality-gate changes receive explicit maintainer review.

With one maintainer, mandatory independent approval would be a fictional control. The repository
therefore records sensitive ownership without claiming an independent approval exists. See the
[roadmap](ROADMAP.md) for the condition under which that control can change.

## Releases and security

Maintainers approve release contents, sign release source, authorize package publication, verify
published artifacts, and decide whether to yank or replace a release. Security reports are handled
privately under [SECURITY.md](SECURITY.md). No contributor or automation receives publication or
repository-administration authority merely by opening a pull request.

## Conflicts of interest

Anyone reviewing a change should disclose a material personal or employer interest that could
affect judgment. A conflicted maintainer should seek another qualified reviewer when one exists and
record the final rationale. Employer affiliation alone does not grant project authority or make an
organization-specific requirement part of the portable product.

## Becoming a trusted contributor or maintainer

Maintainers may grant triage or maintenance responsibility based on sustained, constructive work;
sound technical and community judgment; respect for privacy and security; reliable review; and
understanding of the project's compatibility commitments. There is no contribution-count threshold.

Appointments and removals are recorded publicly with their scope. Maintainer access may be removed
for inactivity, security risk, repeated policy violation, or loss of trust after a fair private
conversation where circumstances allow.

## Changing governance

Governance changes use a public proposal and pull request. The current maintainer approves changes
until governance explicitly delegates that authority. Repository settings and `CODEOWNERS` must be
updated alongside any role change that affects enforcement.
