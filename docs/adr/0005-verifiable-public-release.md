---
status: Accepted
applies_to: fieldkit-cli
---

# Verifiable public release

## Context

A public release must not expose private history or rely on an unverifiable
copy of the source tree.

## Decision

Classify the source Git tree under the reviewed export policy, build a
deterministic clean export, and verify the resulting public root independently.
Bind the reviewed source revision, export, retained wheel and source
distribution, and approval evidence with exact identities and digests.

## Consequences

The approved export is the source for the one-time, one-commit public history.
Retained candidate evidence must bind the exported content and exact package
artifacts to the reviewed source revision. Promotion consumes the verified
retained distributions and approval input; it does not rebuild from a tag.
An export, a passing build, or a structurally valid approval input does not
prove that public cutover or publication has occurred.

Publication remains subject to the readiness gates and explicit operator
approval in the [release procedure](https://github.com/mpeter/fieldkit-cli/blob/main/RELEASING.md). Manual and live
criteria remain pending until their required independent behavioral evidence
and authority are established. Missing, stale, failed, pending, or
mixed-revision evidence remains non-passing. A signed tag, GitHub release, and
package publication require separate verification before describing a release
as public.
