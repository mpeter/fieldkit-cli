# fieldkit roadmap

This is the public source of truth for planned work. It covers every current
in-flight, pending, and backlog outcome. Private implementation records may add
engineering detail, but they do not supersede this roadmap or create a separate
public plan. Released work belongs in release history and the changelog, not
here.

Status is deliberately conservative: **in progress** means implementation or
verification is under way; **pending** means a prerequisite, decision, or live
evidence is still missing; **backlog** means the outcome is intended but not
currently being implemented.

## Release safety and future delivery

| Status | Outcome |
| --- | --- |
| In progress | Maintain a versioned release workflow that promotes only retained, verified artifacts after explicit operator approval, with trusted publishing, signed tags, checksums, SBOMs, provenance attestations, and post-publication checks. |
| In progress | Maintain clean-environment package, contributor, and documentation journeys across the supported matrix; add coverage before expanding supported platforms or profiles. |
| In progress | Keep credentialed, destructive, and live examples explicitly pending until same-candidate, exact-environment evidence exists; safely executable examples use fixed scenarios. |
| In progress | Maintain public repository controls, private vulnerability reporting, release environments, project metadata, and trusted publishers as independently verified release prerequisites. |
| In progress | Maintain a clean-history export that binds source, policy, export tree, candidate artifacts, and promotion evidence without exposing private history. |

## Product reliability and integrations

| Status | Outcome |
| --- | --- |
| In progress | Keep account synchronization fail-closed: a failed primary opportunity read must never look like a successful empty account. |
| Pending | Add read-only support-case access only after its authentication and API contracts are independently verified. |
| Pending | Add safe fixed-price quote repricing only after calculator routing, multi-line behavior, and idempotency are proven. |
| In progress | Adopt native ClosePlan question and score semantics as the qualification authority; finish the remaining mutation-readiness and migration work. |
| In progress | Preserve declared transcript-ingest failure states so retryable and permanent source errors remain visible and recoverable. |
| In progress | Make fatal close-date watcher outcomes reach scheduler-facing nonzero exits. |
| In progress | Keep every watcher state update atomic and fail closed on persistence errors. |
| In progress | Complete a shared signal and freshness model for briefs, pipeline views, and watcher findings without duplicating report pipelines. |
| In progress | Provide a read-only autonomy status view that joins health, driver, admission, and spend evidence without starting autonomous work. |

## Architecture, quality, and contributor experience

| Status | Outcome |
| --- | --- |
| In progress | Complete the configuration-loader decomposition while preserving the public configuration API and behavior. |
| In progress | Complete the Gmail synchronization decomposition so the command adapter stays thin and the domain owns authentication, state, and orchestration. |
| In progress | Complete the Slack watcher classification split and the pursuit-stall decomposition, leaving one canonical owner for each responsibility. |
| In progress | Remove structural complexity that cannot be solved by additional tests and keep the frozen quality thresholds intact. |
| In progress | Reduce bounded pull-request validation latency without weakening complete post-merge or scheduled enforcement. |
| In progress | Align the command and skill taxonomy around one vocabulary; remove unreachable or duplicate skill paths. |
| Pending | Consolidate remaining local workflow tools only where they add a capability with no existing CLI home. |
| In progress | Finish the observable admission, health, recovery, and control-plane boundaries for autonomous workflows. |
| Pending | Add calibration input to the review journal so future review routing is measured rather than assumed. |

## Compatibility candidates

| Status | Outcome |
| --- | --- |
| Backlog | Promote native Windows from experimental only after path, subprocess, installation, and file-write behavior pass the same artifact tests as the supported core. |
| Backlog | Expand optional-profile compatibility only when repeatable CI coverage exists. |

## Community growth

| Status | Outcome |
| --- | --- |
| Backlog | Add independent review requirements after another maintainer accepts that responsibility and the repository can enforce it. |
