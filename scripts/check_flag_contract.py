#!/usr/bin/env python3
"""check_flag_contract.py — Enforce the D3 flag contract across all leaf commands.

D3 rules (from openspec/changes/cli-ux-redesign/design.md):
  1. External writes (Salesforce, GitHub, Google) require --confirm.
  2. Workspace writes (fieldkit_home / fieldkit_data) offer --dry-run.
  3. Every leaf command should expose --json for machine-readable output.

Rules 1 and 2 are HARD gates, and they apply to *declared* commands only —
those carrying `@declare_write(...)` in `fieldkit.cli_registry`. That
restriction is what makes them mean anything.

An earlier revision of this script gated on the registry's `write_class`
without distinguishing declared from inferred. Inference reads `--confirm` to
decide "external", so "external and no --confirm" evaluated to `X and not X`:
zero violations were reachable for any flag combination, and the reassuring
"2/2 external writes, 24/24 workspace writes" was X/X by construction. Worse,
the failure it existed to catch — a command that writes externally *without*
offering --confirm — is precisely the case inference reports as "read-only".
Seven `issue` subcommands that POST to the GitHub REST API sat in that blind
spot.

So: declarations are the gate; inferred entries are reported as unverified
coverage, never as a pass.

Rule 3 is both a completed ratchet and a dynamic gate. The number of non-exempt
commands exposing --json may never fall below `_JSON_COVERAGE_FLOOR`, and every
individual eligible command missing the flag is blocking. The per-command gate
prevents a newly added leaf from hiding behind the completed absolute floor.

Uses fieldkit.cli_registry.build_registry() — the same in-process
introspection that backs `fieldkit commands --json`, so there is one source of
truth.

Exit codes:
    0 — all hard gates pass (advisory issues may still be present)
    1 — one or more hard-gate violations found
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from fieldkit.cli_registry import CommandEntry, build_registry  # noqa: E402

# Commands for which --json is not a missing feature. Each entry needs a reason
# that survives being read aloud; "hasn't got round to it" is not one, and
# belongs in the advisory list where it will keep nagging.
_JSON_EXEMPT: dict[str, str] = {
    "completion": "emits a shell script consumed by `eval`; JSON would break the eval",
    "web serve": "blocking server — it never returns a result document to serialize",
    "auth sf": "interactive credential prompt; the contract is a side effect on the credential store",
    "auth backstory": "interactive OAuth flow; the contract is a side effect on the gateway credential store",
    "auth google": "interactive OAuth flow; the contract is a side effect on the credential store",
    "auth shadowbot": "interactive OAuth flow; the contract is a side effect on the credential store",
    "companion run": (
        "pass-through executor — echoes the child process's stdout/stderr raw and exits with its "
        "code; wrapping that in JSON would corrupt the inner command's own --json output"
    ),
    "ingest promote": (
        "interactive per-item triage loop ((m)ine/(w)aiting-on/(s)kip/(q)uit); the contract is the "
        "operator's decisions landing in TASKS.md, not a report to serialize"
    ),
}

# Considered and REJECTED as an exemption: `ingest run`.
#
# The proposed reason was "largely a stub ('not yet implemented') with an interactive
# doc-confirm flow; no stable report shape to commit to yet". Checked against the code,
# none of that holds:
#   - It is not a stub. `src/fieldkit/commands/ingest/run.py` is a full transcript-ingest
#     implementation. `spec.status == "stub"` there is a per-*pipeline* registry attribute
#     (some pipeline IDs are stubs), and the "not yet implemented" string is a fallback
#     branch for future pipeline IDs — neither describes the command.
#   - The interactive flow is opt-in (`--interactive`, default False). The default path is
#     a parallel thread-pool run with no prompting.
#   - It has a stable report shape already: per-source "Wrote: <path> (account: <slug>)"
#     plus a terminal "Summary: N processed, N skipped, N error(s)", and a pending-source
#     list under --dry-run. That is more JSON-shaped than most of the advisory list.
#
# So `ingest run` is a real --json gap, in the same [workspace] cohort as `ingest
# reprocess` and `pursuit create`. Exempting it would launder a gap out of the ratchet
# denominator on a false premise — precisely what the "reason that survives being read
# aloud" bar above exists to prevent. It stays advisory, where it keeps nagging.

# Ratchet floor: the number of non-exempt commands that expose --json today.
# It may only be raised. Rolling --json out is incremental, so a hard gate on
# full coverage would fail on day one and be reverted; a floor makes the
# incremental work irreversible instead, which is the property that matters.
# Raise this when coverage rises — the check prints the new value when it can.
#
# 26 -> 81 when the issue, gmail/driver/health, and sf/pursuit/meeting/ingest/watch
# batches landed together. Raised once at integration rather than three times:
# each batch measured its own gain against the same 26 baseline (+12, +17, +26),
# so the three floors would have been mutually stale on merge.
#
# 81 -> 91 when the first #1866 slice added structured output to the two saved
# document openers and watcher logs; 91 -> 95 for Gmail decay/enrichment/sync
# and the Salesforce schema reference. The remaining gaps are being closed in
# reviewable slices; each merge raises this number and nothing lowers it.
_JSON_COVERAGE_FLOOR = 102

# Commands that sweep every account today and offer no way to narrow that — the
# `--account SLUG` debt from D3.2. Derived by tracing what each command iterates
# over, not from flag presence, because the whole point is that these have no
# flag to read.
#
# The list may only shrink. Adding `--account` to one of these and leaving it
# here fails the check: a debt register nobody prunes stops being read, and the
# whole reason D3.2 was unactionable for so long is that it pointed at a brief
# (`docs/work-orders/account-filter-plumbing.md`) which never existed.
#
# NOT listed, deliberately: commands that take a single target positionally
# (`sf opportunity OPP_ID`, `meeting note PURSUIT_FILE`) have nothing to narrow,
# and `sf listview TARGET` already filters — it just spells the filter as a
# positional rather than `--account`.
# The D3.2 account-filter debt is discharged. Kept (empty) rather than deleted
# because the check that enforces it is the thing stopping this list rotting
# back into prose the next time a command starts sweeping accounts.
#
# How the last three closed, which is worth recording because two of them were
# never really filter gaps:
#   - ingest reprocess -> filter built. Artifacts carry no account column, but
#     each is written under accounts/<slug>/, so the scope is recoverable from
#     content_path after routing. The earlier "needs a schema change first" note
#     here was wrong.
#   - ingest run   -> a filter is not meaningful. The account is the *output* of
#     this command, computed per source during routing; there is nothing to
#     narrow on the way in.
#   - ingest route -> a filter is not meaningful either: its whole input is
#     accounts/unknown/, i.e. files whose account is by definition not yet
#     known. What it needed was the opposite of a filter — an operator override
#     for a single file, shipped as `--file X --force-account Y`.
_ACCOUNT_FILTER_GAPS: dict[str, str] = {}


def _has_opt(entry: CommandEntry, opt: str) -> bool:
    return any(opt in f.opts for f in entry.flags)


def _declared(entries: list[CommandEntry]) -> list[CommandEntry]:
    return [e for e in entries if e.write_class_source == "declared"]


def _json_eligible(entries: list[CommandEntry]) -> list[CommandEntry]:
    """Commands for which --json is meaningful — the ratchet's denominator."""
    return [e for e in entries if e.full_name not in _JSON_EXEMPT]


def check(hard_gate_json: bool = False, entries: list[CommandEntry] | None = None) -> int:
    """Run the contract check. *entries* defaults to the live registry; tests inject.

    Injection is not a convenience: the previous gate's defect was that no input
    could make it fail, and that is only demonstrable by feeding it an input that
    should. tests/test_check_flag_contract.py does exactly that.
    """
    entries = build_registry() if entries is None else entries
    violations: list[str] = []
    exemptions: list[str] = []

    for e in _declared(entries):
        # Rule 1: declared external writes must have --confirm, or an explicit
        # recorded reason why they do not.
        if e.write_class == "external" and not _has_opt(e, "--confirm"):
            if e.confirm_exempt:
                exemptions.append(f"  EXEMPT     {e.full_name!s:<45} no --confirm — {e.confirm_exempt}")
            else:
                violations.append(f"  VIOLATION  {e.full_name!s:<45} declared external but --confirm is missing")

        # Rule 2: declared workspace writes must have --dry-run.
        if e.write_class == "workspace" and not _has_opt(e, "--dry-run"):
            violations.append(f"  VIOLATION  {e.full_name!s:<45} declared workspace but --dry-run is missing")

    # Rule 3: every non-exempt command must have --json. Keep the historical
    # argument for CLI compatibility; full coverage is now unconditional.
    eligible = _json_eligible(entries)
    for e in eligible:
        if not _has_opt(e, "--json"):
            msg = f"  MISSING    {e.full_name!s:<45} --json not present  [{e.write_class}]"
            violations.append(msg)

    # D3.2 account-filter debt: the gap list must stay accurate. A command that
    # has gained a filter has to leave the list, or the register rots into prose.
    by_name = {e.full_name: e for e in entries}
    for name in _ACCOUNT_FILTER_GAPS:
        entry = by_name.get(name)
        if entry is None:
            continue  # entries may be a test fixture rather than the live CLI
        if entry.account_scope == "filter":
            violations.append(
                f"  RESOLVED   {name!s:<45} now has an --account filter — remove it from _ACCOUNT_FILTER_GAPS"
            )

    # Rule 3 ratchet: coverage may rise, never fall. Blocking regardless of
    # --hard-gate, because a floor that only applies on request is not a floor.
    json_coverage = sum(1 for e in eligible if _has_opt(e, "--json"))
    if json_coverage < _JSON_COVERAGE_FLOOR:
        violations.append(
            f"  RATCHET    --json coverage fell to {json_coverage}, below the floor of "
            f"{_JSON_COVERAGE_FLOOR}. Restore the flag, or justify lowering the floor in the PR."
        )

    total = len(entries)
    declared = _declared(entries)
    inferred_writes = [e for e in entries if e.write_class_source == "inferred" and e.write_class != "read-only"]
    has_json = json_coverage

    n_eligible = len(eligible)
    print(f"D3 flag contract check — {total} leaf commands")
    print(
        f"  --json coverage    : {has_json}/{n_eligible} eligible "
        f"({100 * has_json // n_eligible if n_eligible else 100}%), floor {_JSON_COVERAGE_FLOOR}"
        f"  [{len(_JSON_EXEMPT)} exempt]"
    )
    print(f"  write declarations : {len(declared)}/{total} commands declare what they write")
    print(f"  declared external  : {sum(1 for e in declared if e.write_class == 'external')}  (gated on --confirm)")
    print(f"  declared workspace : {sum(1 for e in declared if e.write_class == 'workspace')}  (gated on --dry-run)")
    print(f"  inferred writes    : {len(inferred_writes)}  (flag-derived, NOT verified — see note below)")
    # D3 names --account SLUG alongside --json and write classification. It is
    # reported, not gated: at 27/100 a gate would fail immediately, and unlike
    # --json there is no agreed set of commands that *should* be account-scoped
    # to ratchet against. Printing it stops the number being invisible.
    n_filter = sum(1 for e in entries if e.account_scope == "filter")
    n_selector = sum(1 for e in entries if e.account_scope == "selector")
    gap_note = f"{len(_ACCOUNT_FILTER_GAPS)} known gaps — see below" if _ACCOUNT_FILTER_GAPS else "no known gaps"
    print(f"  --account          : {n_filter} filter, {n_selector} selector  ({gap_note})")
    print()

    if violations:
        print(f"HARD GATE — {len(violations)} violation(s):")
        for v in violations:
            print(v)
        print()

    if exemptions:
        print(f"Recorded exemptions — {len(exemptions)} declared external write(s) without --confirm:")
        for x in exemptions:
            print(x)
        print()

    if inferred_writes:
        print(
            f"Unverified — {len(inferred_writes)} command(s) classified by flag inference, not declaration.\n"
            "  Their write class describes which flags they offer, not what they touch, so the\n"
            "  hard gates above do not apply to them. Add @declare_write to bring one under the gate."
        )
        print()

    if _ACCOUNT_FILTER_GAPS:
        print(
            f"Account-filter debt — {len(_ACCOUNT_FILTER_GAPS)} command(s) sweep every account "
            "with no way to narrow (D3.2, not blocking):"
        )
        for name, reason in sorted(_ACCOUNT_FILTER_GAPS.items()):
            print(f"  GAP        {name!s:<45} {reason}")
        print()

    if _JSON_EXEMPT:
        print(f"Exempt from --json — {len(_JSON_EXEMPT)} command(s), excluded from the ratchet denominator:")
        for name, reason in sorted(_JSON_EXEMPT.items()):
            print(f"  EXEMPT     {name!s:<45} {reason}")
        print()

    if violations:
        return 1

    if json_coverage > _JSON_COVERAGE_FLOOR:
        # Ratcheting is manual on purpose: a floor the script rewrites for itself
        # is a floor that silently follows coverage down after a bad merge.
        print(
            f"Ratchet ready — --json coverage is {json_coverage}, above the floor of "
            f"{_JSON_COVERAGE_FLOOR}.\n"
            f"  Raise _JSON_COVERAGE_FLOOR to {json_coverage} in {Path(__file__).name} to lock the gain in."
        )
        print()

    print("Hard gates: PASS ✓")
    if inferred_writes:
        print("  (advisory and unverified items noted above — not blocking)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    hard_gate = "--hard-gate" in args
    return check(hard_gate_json=hard_gate)


if __name__ == "__main__":
    sys.exit(main())
