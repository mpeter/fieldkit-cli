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
| In progress | Establish and independently verify a versioned release workflow that promotes only retained, verified artifacts after explicit operator approval. Bind independently approved controller and signer identities, the exact candidate, producer workflow, completed approval, and artifact digests before candidate execution; candidate-controlled checks and a valid signature alone are not release authority. Preserve trusted publishing, signed tags, checksums, SBOMs, provenance attestations, and post-publication checks. |
| In progress | Support normal and corrective successor releases from public history without repeating the initial clean-history cutover. Preserve the initial public identity, require current-candidate approval and artifact evidence, and independently verify both release modes before claiming the release and recovery procedures work. |
| In progress | Validate dependency inventories and SPDX expression syntax with maintained tooling, bind target-platform observations to the locked graph and runtime SBOM, and keep unobserved platforms or profiles non-passing. Keep policy tools outside the runtime package inventory. |
| In progress | Prove clean-environment package, contributor, and documentation journeys across the supported matrix; add coverage before expanding supported platforms or profiles. |
| In progress | Make first use and public workflow instructions evergreen and independently reproducible: remove private prerequisites and unsupported promises, prove the offline starting path, and bind each example and structured claim to one fail-closed verification owner. Complete this cleanup and a fresh external-engineer review before release promotion. |
| In progress | Keep repository-wide agent instructions canonical and domain guidance explicitly scoped. Verify every exported instruction path and both quality tiers, remove retired private adapter contracts from public consumers, and independently review the replacement boundary before release. |
| In progress | Keep credentialed, destructive, and live examples explicitly pending until same-candidate, exact-environment evidence exists; safely executable examples use fixed scenarios. |
| In progress | Register every credentialed and release-cutover documentation block with exactly one behavioral scenario or deterministic table-row validator. Bind its command, actor, side-effect policy, and result to the same candidate; unowned, transcript-only, hash-only, stale, and mixed-revision evidence remains non-passing. |
| Pending | Replace custom documentation isolation machinery with a proven maintained runtime and a thin fixed-argv evidence adapter. Preserve the locked environment, deny and observe unsafe effects, contain descendants, bind an independently approved controller and full toolchain, hash retained artifacts on the controller, account for complete output, and capture actual owner execution rather than candidate self-reporting. Keep backend selection, tool approval, and unproven containment explicitly pending. |
| In progress | Establish and independently verify public repository controls, private vulnerability reporting, release environments, project metadata, and trusted publishers as release prerequisites. |
| In progress | Complete and independently verify a clean-history export that binds source, policy, export tree, candidate artifacts, and promotion evidence without exposing private history. |
| Pending | Identify and verify missing strict response schemas for optional integrations. |
| Pending | Verify credential-file permission controls on every supported platform. |

## Product reliability and integrations

| Status | Outcome |
| --- | --- |
| In progress | Keep account synchronization fail-closed: a failed primary opportunity read must never look like a successful empty account. |
| In progress | Verify Salesforce connection and timeout retries against the shared retry contract, preserve single-attempt guarded writes and adapter-specific failure outcomes, and keep standard diagnostics free of transport, response, and credential details. |
| In progress | Verify quota calendar validation and status persistence for every watcher and the aggregate runner from the final candidate. Refused status writes, degraded brief sources, and skipped partial runs must remain non-passing without masking authentication failures. Keep text and JSON outcomes consistent, distinguish a written artifact from a successful run, and preserve bounded diagnostics and no-write previews. |
| In progress | Make command write classifications reflect actual local and remote effects, and ensure partial Salesforce list-view write failures return a non-success exit status. |
| In progress | Make Salesforce sync distinguish refused, partial, and completed writes; preserve cached data on frontmatter refusal, publish caches atomically, and centralize frontmatter behavior in the domain layer. Verify supported cache locations, repeated updates, batch outcomes, and reconciliation failures before accepting workflow evidence. |
| Pending | Verify pursuit-audit account and report destinations are confined to approved roots, make report writes atomic, and make refused repairs visibly non-passing rather than masked by later audit output. |
| In progress | Verify companion command permissions against actual argv effects: cursor-writing feed calls must not masquerade as read-only, preview flags must never authorize conflicting write options, allowlist matching must provide its documented isolation, and action execution must use the intended installation. Require validated policy at the execution boundary, not only in CLI callers. Keep these controls non-passing until independently proven. |
| Pending | Add read-only support-case access only after its authentication and API contracts are independently verified. |
| Pending | Add safe fixed-price quote repricing only after calculator routing, multi-line behavior, and idempotency are proven. |
| In progress | Adopt native ClosePlan question and score semantics as the qualification authority; finish the remaining mutation-readiness and migration work. |
| In progress | Preserve declared transcript-ingest failure states so retryable and permanent source errors remain visible and recoverable. |
| In progress | Complete ingest safety across run and reprocess: enforce transport timeouts, confined atomic writes and shared locks, ownership-preserving reprocessing, non-success exits for failed or interrupted work, payload-free diagnostics, and complete note/task validation before saving durable intent. These remain release blockers until verified. |
| In progress | Make fatal close-date watcher outcomes reach scheduler-facing nonzero exits. |
| Pending | Make Slack thread-watcher authentication and fatal outcomes reach non-success exits, and make preview filesystem effects match the documented write contract. Verify bounded search coverage and identity handling before treating watcher results as complete. |
| Pending | Reconcile watcher counts, skipped-run outcomes, alert deduplication, bounded log reads, configuration-cache invalidation, and corrupt-state reporting with actual behavior; independently verify the resulting status and persistence contracts. |
| In progress | Replace raw SQLite copies with writer-published committed read models whose receipt binds a stable database identity, generation, schema, and exact immutable artifact. Enforce owner-only storage and a non-bypassable managed transaction; published artifacts must never become writable sources. Migrate every supported producer and reader, preserve explicit external export/import and recovery workflows, and prove crash, concurrency, malformed-data, and zero-source-mutation behavior before accepting read evidence. Stable bytes or a passing integrity check alone do not prove committed data. |
| Pending | Prove atomic reconciliation of removed and reassigned Gmail account labels from the final exported candidate so enrichment cannot trust stale associations. |
| Pending | Prove bounded CRM-review candidate reports from the final exported candidate against ready published caches, with truthful incomplete-scan status and no claim of an authoritative CRM comparison. |
| Pending | Add authoritative CRM reconciliation for Gmail-derived contact candidates only after identity, account scope, source freshness, and comparison coverage are independently verified. |
| Pending | Complete a shared signal and freshness model for briefs, pipeline views, and watcher findings without duplicating report pipelines. |

## Architecture, quality, and contributor experience

| Status | Outcome |
| --- | --- |
| In progress | Complete the configuration-loader decomposition while preserving the public configuration API and behavior. |
| In progress | Remove structural complexity that cannot be solved by additional tests and keep the frozen quality thresholds intact. |
| In progress | Give SQLite publication storage and Salesforce exception, transport, and response handling cohesive canonical homes. Preserve public method contracts, receipt bindings, connection ownership, and cheap exception imports; independently verify the combined changes before release. |
| In progress | Reduce bounded pull-request validation latency without weakening complete post-merge or scheduled enforcement. |
| In progress | Align the command and skill taxonomy around one vocabulary; remove unreachable or duplicate skill paths. |
| In progress | Make driver edit authority portable and mechanically enforced against the exact executed revision and submitted diff. Fail closed on uncertain ready-queue reads, constrain edit sites to scheduler ownership, and keep untrusted public comments out of unattended retry instructions. Independently prove unique anchors, drift refusal, retry evidence, and declared change boundaries. |
| In progress | Bound unattended driver output and keep run logs private. Use explicit child credential authority instead of inheriting the operator environment, sanitize provider failures, and reject malformed identity responses while safely finalizing retry reservations. Verify the execution isolation boundary independently; environment filtering alone is not a sandbox. |
| In progress | Move issue workflows into one canonical domain with GitHub-number identities, typed transport and creation-uncertainty outcomes, idempotent transitions, and truthful partial batch results. |
| In progress | Reconcile optional integration entry points with documented, portable configuration; remove stale private routes and ensure unconfigured integrations cannot obstruct local-first workflows. |
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
