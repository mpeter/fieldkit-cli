"""Tests for scripts/check_flag_contract.py — the D3 flag contract gate.

The point of these tests is narrow and specific: prove the gate can fail.

The previous revision derived `write_class` from `--confirm` and then checked
that external commands have `--confirm` — `X and not X`, which returns 0 for
every possible input. It reported "2/2 external writes" and looked healthy
while catching nothing. A gate that cannot fail is indistinguishable from no
gate, so every hard rule below is tested with an input that must trip it, not
only with inputs that pass.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from fieldkit.cli_registry import CommandEntry, CommandFlag

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_flag_contract.py"


def _load_checker():  # type: ignore[no-untyped-def]
    """Import the check script by path — scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("check_flag_contract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_flag_contract"] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

#: The floor as committed. Captured before the autouse fixture zeroes it.
_REAL_JSON_FLOOR = checker._JSON_COVERAGE_FLOOR


@pytest.fixture(autouse=True)
def _neutral_json_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the --json ratchet floor for synthetic fixtures.

    The tests below build one- and two-command registries to isolate a single
    rule. The committed floor (26) would trip on every one of them, so each
    test would pass or fail for the wrong reason. Ratchet tests set their own
    floor; the live-registry tests restore the committed one.
    """
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 0)


def _entry(
    full_name: str,
    *,
    write_class: str = "read-only",
    source: str = "declared",
    opts: tuple[str, ...] = (),
    confirm_exempt: str | None = None,
) -> CommandEntry:
    flags = [CommandFlag(opts=[o], is_flag=True, type_name="boolean", help=None) for o in opts]
    return CommandEntry(
        full_name=full_name,
        summary="test command",
        flags=flags,
        write_class=write_class,
        write_class_source=source,  # type: ignore[arg-type]
        confirm_exempt=confirm_exempt,
    )


# --------------------------------------------------------------------------
# The gate must be able to fail
# --------------------------------------------------------------------------


def test_declared_external_without_confirm_is_a_violation() -> None:
    """Rule 1 fires: this is the input the old tautological gate could not fail on."""
    entries = [_entry("sf danger", write_class="external", opts=("--json",))]
    exit_code = checker.check(entries=entries)
    assert exit_code == 1


def test_declared_workspace_without_dry_run_is_a_violation() -> None:
    """Rule 2 fires on a declared workspace write with no --dry-run."""
    entries = [_entry("brief scribble", write_class="workspace", opts=("--json",))]
    exit_code = checker.check(entries=entries)
    assert exit_code == 1


def test_violation_names_the_offending_command(capsys: pytest.CaptureFixture[str]) -> None:
    """The failure output identifies which command broke the rule, not just a count."""
    entries = [_entry("sf danger", write_class="external", opts=("--json",))]
    checker.check(entries=entries)
    out = capsys.readouterr().out
    assert "HARD GATE" in out
    assert "sf danger" in out
    assert "--confirm is missing" in out


# --------------------------------------------------------------------------
# ... and must pass the cases that are genuinely fine
# --------------------------------------------------------------------------


def test_declared_external_with_confirm_passes() -> None:
    entries = [_entry("sf set-field", write_class="external", opts=("--confirm", "--json"))]
    assert checker.check(entries=entries) == 0


def test_declared_workspace_with_dry_run_passes() -> None:
    entries = [_entry("sf reconcile", write_class="workspace", opts=("--dry-run", "--json"))]
    assert checker.check(entries=entries) == 0


def test_confirm_exemption_suppresses_the_violation_but_is_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exemption is an escape hatch you can grep for, not a silent skip."""
    entries = [
        _entry(
            "issue create",
            write_class="external",
            opts=("--json",),
            confirm_exempt="driver loop invokes this unattended",
        )
    ]
    exit_code = checker.check(entries=entries)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "EXEMPT" in out
    assert "issue create" in out
    assert "driver loop invokes this unattended" in out


# --------------------------------------------------------------------------
# Inference is reported, never gated on
# --------------------------------------------------------------------------


def test_inferred_external_without_confirm_is_not_gated() -> None:
    """Inferred entries cannot violate rule 1 — which is exactly why they are not the gate.

    An inferred "external" got that label *from* --confirm, so this state is
    unreachable in the live registry. Asserting it here pins the design: the
    check consults declarations only.
    """
    entries = [_entry("legacy thing", write_class="external", source="inferred", opts=("--json",))]
    assert checker.check(entries=entries) == 0


def test_inferred_writes_are_reported_as_unverified(capsys: pytest.CaptureFixture[str]) -> None:
    entries = [_entry("ingest run", write_class="workspace", source="inferred", opts=("--dry-run",))]
    checker.check(entries=entries)
    out = capsys.readouterr().out
    assert "Unverified" in out
    assert "NOT verified" in out


# --------------------------------------------------------------------------
# --json rule
# --------------------------------------------------------------------------


def test_missing_json_is_blocking_by_default() -> None:
    entries = [_entry("gmail sync")]
    assert checker.check(entries=entries) == 1


def test_missing_json_fails_under_hard_gate() -> None:
    entries = [_entry("gmail sync")]
    assert checker.check(hard_gate_json=True, entries=entries) == 1


# --------------------------------------------------------------------------
# Live registry
# --------------------------------------------------------------------------


def test_live_registry_passes_the_hard_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real CLI satisfies rules 1 and 2 and clears the committed ratchet floor."""
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", _REAL_JSON_FLOOR)
    assert checker.check() == 0


def test_live_sf_frontmatter_declares_workspace_dry_run() -> None:
    """The live command that writes pursuit files satisfies the workspace safety contract."""
    from fieldkit.cli_registry import build_registry

    entry = next(entry for entry in build_registry() if entry.full_name == "sf frontmatter")
    opts = {opt for flag in entry.flags for opt in flag.opts}

    assert entry.write_class == "workspace"
    assert entry.write_class_source == "declared"
    assert "--dry-run" in opts


# --------------------------------------------------------------------------
# --json ratchet
# --------------------------------------------------------------------------


def test_coverage_below_the_floor_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor blocks unconditionally — not only under --hard-gate.

    A floor that applies on request is not a floor.
    """
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 2)
    entries = [_entry("a", opts=("--json",)), _entry("b")]
    assert checker.check(entries=entries) == 1


def test_ratchet_breach_names_the_floor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 2)
    checker.check(entries=[_entry("a", opts=("--json",)), _entry("b")])
    out = capsys.readouterr().out
    assert "RATCHET" in out
    assert "below the floor of 2" in out


def test_coverage_at_the_floor_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 1)
    entries = [_entry("a", opts=("--json",))]
    assert checker.check(entries=entries) == 0


def test_coverage_above_the_floor_prompts_to_raise_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Raising the floor is a deliberate edit, so the check has to ask for it."""
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 1)
    checker.check(entries=[_entry("a", opts=("--json",)), _entry("b", opts=("--json",))])
    out = capsys.readouterr().out
    assert "Ratchet ready" in out
    assert "Raise _JSON_COVERAGE_FLOOR to 2" in out


# --------------------------------------------------------------------------
# --json exemptions
# --------------------------------------------------------------------------


def test_exempt_command_is_not_advised_for_missing_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(checker, "_JSON_EXEMPT", {"completion": "emits a shell script"})
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 0)
    checker.check(entries=[_entry("completion")])
    out = capsys.readouterr().out
    assert "MISSING    completion" not in out
    assert "EXEMPT     completion" in out


def test_exempt_command_does_not_fail_under_hard_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """--hard-gate demands --json of eligible commands only, never the exempt ones."""
    monkeypatch.setattr(checker, "_JSON_EXEMPT", {"completion": "emits a shell script"})
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 0)
    assert checker.check(hard_gate_json=True, entries=[_entry("completion")]) == 0


def test_non_exempt_command_still_fails_under_hard_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "_JSON_EXEMPT", {"completion": "emits a shell script"})
    monkeypatch.setattr(checker, "_JSON_COVERAGE_FLOOR", 0)
    assert checker.check(hard_gate_json=True, entries=[_entry("gmail sync")]) == 1


def test_every_json_exemption_states_a_reason() -> None:
    """An exemption without a reason is indistinguishable from an oversight."""
    for name, reason in checker._JSON_EXEMPT.items():
        assert reason.strip(), f"{name} is exempt from --json with no reason given"


def test_json_exemptions_name_real_commands() -> None:
    """A stale exemption silently shrinks the ratchet denominator."""
    from fieldkit.cli_registry import build_registry

    live = {e.full_name for e in build_registry()}
    unknown = set(checker._JSON_EXEMPT) - live
    assert not unknown, f"exempt entries no longer in the CLI: {sorted(unknown)}"


def test_live_floor_is_not_above_live_coverage() -> None:
    """The committed floor must be satisfiable by the tree that ships with it."""
    from fieldkit.cli_registry import build_registry

    entries = build_registry()
    eligible = [e for e in entries if e.full_name not in checker._JSON_EXEMPT]
    coverage = sum(1 for e in eligible if any("--json" in f.opts for f in e.flags))
    assert coverage >= _REAL_JSON_FLOOR
    assert coverage == len(eligible), "every eligible public command must support --json"


# --------------------------------------------------------------------------
# Declaration coverage ratchet
# --------------------------------------------------------------------------


def test_no_write_command_is_left_to_inference() -> None:
    """Every command the registry calls a write must say so itself.

    Inference reports the flags a command offers, not what it touches, so an
    Adding a command with --dry-run and no declaration re-opens that gap, so
    this holds the count at zero rather than merely reporting it.
    """
    from fieldkit.cli_registry import build_registry

    inferred_writes = [
        e.full_name for e in build_registry() if e.write_class_source == "inferred" and e.write_class != "read-only"
    ]
    assert not inferred_writes, (
        f"{len(inferred_writes)} command(s) classified as writes by flag inference rather than "
        f"@declare_write: {sorted(inferred_writes)}. Audit what each one touches and declare it."
    )


# --------------------------------------------------------------------------
# D3.2 account-filter debt register
# --------------------------------------------------------------------------


def _account_entry(full_name: str, scope: str | None) -> CommandEntry:
    opts = ("--json", "--account") if scope else ("--json",)
    entry = _entry(full_name, opts=opts)
    return CommandEntry(
        full_name=entry.full_name,
        summary=entry.summary,
        flags=entry.flags,
        write_class=entry.write_class,
        write_class_source=entry.write_class_source,
        account_scope=scope,  # type: ignore[arg-type]
    )


def test_resolved_gap_left_in_the_register_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A debt register nobody prunes stops being read, so pruning is enforced.

    The live register is empty now that the D3.2 debt is discharged, so these
    exercise the mechanism against a synthetic one — otherwise the enforcement
    would silently stop being tested the moment it succeeded.
    """
    monkeypatch.setattr(checker, "_ACCOUNT_FILTER_GAPS", {"some cmd": "sweeps every account"})
    assert checker.check(entries=[_account_entry("some cmd", "filter")]) == 1


def test_resolved_gap_message_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(checker, "_ACCOUNT_FILTER_GAPS", {"some cmd": "sweeps every account"})
    checker.check(entries=[_account_entry("some cmd", "filter")])
    out = capsys.readouterr().out
    assert "RESOLVED" in out
    assert "_ACCOUNT_FILTER_GAPS" in out


def test_unresolved_gap_is_reported_but_not_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The debt is visible on every run without failing anyone's build."""
    monkeypatch.setattr(checker, "_ACCOUNT_FILTER_GAPS", {"some cmd": "sweeps every account"})
    assert checker.check(entries=[_account_entry("some cmd", None)]) == 0


def test_selector_does_not_count_as_resolving_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required --account names a target; it does not narrow a sweep.

    Counting it as resolution is exactly the conflation this split exists to
    prevent — it would silently retire real debt.
    """
    monkeypatch.setattr(checker, "_ACCOUNT_FILTER_GAPS", {"some cmd": "sweeps every account"})
    assert checker.check(entries=[_account_entry("some cmd", "selector")]) == 0


def test_every_gap_states_a_reason() -> None:
    for name, reason in checker._ACCOUNT_FILTER_GAPS.items():
        assert reason.strip(), f"{name} listed as a gap with no reason"


def test_gap_register_names_real_commands() -> None:
    """A stale entry would make the debt look larger than it is, forever."""
    from fieldkit.cli_registry import build_registry

    live = {e.full_name for e in build_registry()}
    unknown = set(checker._ACCOUNT_FILTER_GAPS) - live
    assert not unknown, f"gap entries no longer in the CLI: {sorted(unknown)}"


def test_no_listed_gap_already_has_a_filter() -> None:
    """The committed register must be accurate against the tree that ships with it."""
    from fieldkit.cli_registry import build_registry

    by_name = {e.full_name: e for e in build_registry()}
    already = [n for n in checker._ACCOUNT_FILTER_GAPS if by_name[n].account_scope == "filter"]
    assert not already, f"listed as gaps but already filtered: {already}"
