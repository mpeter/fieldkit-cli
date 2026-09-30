"""fieldkit pursuit advance — Stage-policy check and pursuit advancement.

Evaluates the current policy for the target stage transition, prints the
decision, and updates the frontmatter in-place when policy permits or the
operator supplies an explicit override.

Stage sequence:
  pre-pipeline → prospect → qualify → discover → validate →
  propose → negotiate → closed-won

Moving to closed-lost is always allowed from any stage.

The former score-dependent transitions remain pending until a Salesforce-
native qualification policy is ratified. Historical local scores never pass
those transitions. Qualification-independent transitions retain their prior
behavior.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.cli_registry import declare_account_scope, declare_write
from fieldkit.config import get_accounts_root
from fieldkit.pursuit import parse_frontmatter, write_frontmatter_raw
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.gate_criteria import ALLOWED_GATE_STATUSES, GatePolicyDecision, evaluate_gate_policy
from fieldkit.pursuit.io import read_pursuit_text_snapshot, render_frontmatter_raw
from fieldkit.pursuit.paths import PursuitPathError, resolve_pursuit_file
from fieldkit.pursuit.stages import ALL_STAGES as ALLOWED_STAGES
from fieldkit.pursuit.stages import CLOSED_STAGES
from fieldkit.pursuit.stages import PIPELINE_STAGES as _STAGE_ORDER
from fieldkit.util.text_snapshot import TextSnapshot

LOG_PREFIX = "[pursuit-advance]"
_BACKWARD_OVERRIDE_REASON = "Backward transitions require an explicit override reason"


def _next_stage(current: str) -> str | None:
    """Return the next stage in the sequence, or None if at the end."""
    try:
        idx = _STAGE_ORDER.index(current)
        next_stage = _STAGE_ORDER[idx + 1] if idx + 1 < len(_STAGE_ORDER) else Stage.CLOSED_WON
        return str(next_stage)
    except ValueError:
        return None


def _check_gate(from_stage: str, to_stage: str) -> GatePolicyDecision:
    """Return the ratified current policy decision for a transition."""
    return evaluate_gate_policy(from_stage, to_stage)


def _read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a pursuit file.

    Returns (frontmatter_dict, raw_body_after_second_delimiter).
    Raises ValueError if no frontmatter found.
    """
    frontmatter, body, _source = _read_frontmatter_snapshot(path)
    return frontmatter, body


def _read_frontmatter_snapshot(path: Path) -> tuple[dict[str, Any], str, TextSnapshot]:
    """Parse one bounded, no-follow source and retain its exact identity."""
    source = read_pursuit_text_snapshot(path)
    result = parse_frontmatter(source.content)
    if result is None:
        raise ValueError("No YAML frontmatter in pursuit source")
    frontmatter, body = result
    return frontmatter, body, source


def _apply_transition(
    path: Path,
    fm: dict[str, Any],
    body: str,
    new_stage: str,
    gate_status: str,
    note: str,
    *,
    expected_mtime: float | None = None,
    expected_source_content: str | None = None,
) -> None:
    """Write the new stage, gate-status, last-transition, and transition-history entry."""
    if gate_status not in ALLOWED_GATE_STATUSES:
        raise ValueError(f"Invalid gate_status {gate_status!r}; must be one of {sorted(ALLOWED_GATE_STATUSES)}")
    today = datetime.now(tz=UTC).date().isoformat()
    old_stage = str(fm.get("stage", ""))

    fm["stage"] = new_stage
    fm["gate-status"] = gate_status
    fm["last-transition"] = today

    history: list[Any] = fm.get("transition-history") or []
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "date": today,
            "from": old_stage,
            "to": new_stage,
            "gate-result": gate_status,
            **({"override-reason": note.removeprefix("Override: ")} if gate_status == "override" else {}),
        }
    )
    fm["transition-history"] = history

    validated_content = None
    if expected_source_content is not None:
        validated_content = render_frontmatter_raw(path, fm, body, expected_mtime=expected_mtime)
    write_frontmatter_raw(
        path,
        fm,
        body,
        expected_mtime=expected_mtime,
        validated_content=validated_content,
        validated_source_content=expected_source_content,
    )


def _resolve_pursuit_spec(spec: str) -> Path:
    """Resolve a pursuit selector within the configured workspace."""
    return resolve_pursuit_file(get_accounts_root().parent, spec)


def _resolve_target_stage(current_stage: str, target_stage: str | None) -> str | None:
    """Resolve the target stage, returning None if no next stage exists."""
    if target_stage is not None:
        return target_stage.lower()
    return _next_stage(current_stage)


def _validate_stage_transition(current_stage: str, target_stage: str) -> str | None:
    """Validate the stage transition. Returns an error message string, or None if valid."""
    current_error = _current_stage_error(current_stage)
    if current_error is not None:
        return current_error
    return _target_stage_error(current_stage, target_stage)


def _current_stage_error(current_stage: str) -> str | None:
    """Return the validation error for a current pursuit stage."""
    if current_stage not in ALLOWED_STAGES:
        return f"'{current_stage}' is not a valid current stage.\n  Valid stages: {sorted(ALLOWED_STAGES)}"
    if current_stage in CLOSED_STAGES:
        return f"Pursuit is already closed at stage '{current_stage}' and cannot be advanced."
    return None


def _target_stage_error(current_stage: str, target_stage: str) -> str | None:
    """Return the validation error for a requested target stage."""
    if target_stage not in ALLOWED_STAGES:
        return f"'{target_stage}' is not a valid stage.\n  Valid stages: {sorted(ALLOWED_STAGES)}"
    if target_stage == current_stage:
        return f"Already at stage '{current_stage}'. Specify a different target stage with --to."
    return None


def _is_backward_transition(current_stage: str, target_stage: str) -> bool:
    """Return whether both active stages are known and the target precedes the source."""
    try:
        return _STAGE_ORDER.index(target_stage) < _STAGE_ORDER.index(current_stage)
    except ValueError:
        return False


def _evaluate_gate(
    current_stage: str,
    target_stage: str,
    override_reason: str | None,
    quiet: bool = False,
) -> tuple[bool, str, str, tuple[str, ...]]:
    """Evaluate the gate and apply override if provided.

    ``quiet`` suppresses the stdout advisory so JSON mode owns stdout; the
    same verdict is returned either way.

    Returns (passed, gate_status, note, reasons).
    """
    if _is_backward_transition(current_stage, target_stage):
        decision = GatePolicyDecision(status="pending", reasons=(_BACKWARD_OVERRIDE_REASON,))
    else:
        decision = _check_gate(current_stage, target_stage)
    passed = decision.passed
    gate_status, note = _describe_gate_decision(
        current_stage,
        target_stage=target_stage,
        decision=decision,
        quiet=quiet,
    )

    normalized_override = override_reason.strip() if override_reason is not None else ""
    if not passed and normalized_override:
        if not quiet:
            click.echo(f"\nOverride: {normalized_override}")
        gate_status = "override"
        note = f"Override: {normalized_override}"
        passed = True

    return passed, gate_status, note, decision.reasons


def _describe_gate_decision(
    current_stage: str,
    *,
    target_stage: str,
    decision: GatePolicyDecision,
    quiet: bool,
) -> tuple[str, str]:
    """Emit and describe a qualification-independent gate decision."""
    if decision.passed:
        if not quiet:
            click.echo("Gate: ✓ PASS — transition is qualification-independent")
        return "pass", f"Gate passed for {current_stage} → {target_stage}"
    if not quiet:
        click.echo("Gate: … PENDING — current qualification policy unavailable:")
        for reason in decision.reasons:
            click.echo(f"  {reason}")
    return "pending", f"Gate pending for {current_stage} → {target_stage}: {'; '.join(decision.reasons)}"


def _resolve_advance_target(
    pursuit_spec: str | None,
    account: str | None,
    name: str | None,
) -> Path:
    """Resolve the pursuit file path from either positional spec or --account/--name flags.

    Extracted from ``advance_cmd`` to reduce its cyclomatic complexity (CRAP gate).

    Args:
        pursuit_spec: Positional argument (full path or '<account>/<slug>' shorthand).
        account: Account slug from --account flag (must be paired with name).
        name: Pursuit slug from --name flag (must be paired with account).

    Returns:
        Resolved Path to the pursuit file.

    Raises:
        click.UsageError: on invalid flag combinations or missing arguments.
        SystemExit(3): when the pursuit does not resolve safely.
    """
    # implementation change: resolve pursuit from either positional or --account/--name flags.
    if account is not None and name is not None:
        if pursuit_spec is not None:
            raise click.UsageError("Provide PURSUIT_SPEC or --account/--name, not both.")
        pursuit_spec = f"{account}/{name}"
    elif account is not None or name is not None:
        raise click.UsageError("--account and --name must be used together.")
    elif pursuit_spec is None:
        raise click.UsageError("Provide PURSUIT_SPEC or use --account and --name together.")

    try:
        return _resolve_pursuit_spec(pursuit_spec)
    except PursuitPathError:
        click.echo(
            f"{LOG_PREFIX} ERROR: pursuit must be an existing file in the configured workspace",
            err=True,
        )
        raise SystemExit(EXIT_DATA) from None


# --account is optional here only because PURSUIT_SPEC is a positional
# alternative; it names the pursuit to advance rather than narrowing a sweep,
# so requiredness would misclassify it as a filter.
@declare_account_scope("selector")
@declare_write("workspace")
@click.command(name="advance")
@click.argument("pursuit_spec", type=str, required=False, default=None)
@click.option(
    "--account",
    "-a",
    default=None,
    metavar="ACCOUNT",
    help="Account slug (use with --name as an alternative to positional PURSUIT_SPEC).",
)
@click.option(
    "--name",
    "-n",
    default=None,
    metavar="NAME",
    help="Pursuit slug (use with --account as an alternative to positional PURSUIT_SPEC).",
)
@click.option(
    "--to",
    "target_stage",
    default=None,
    metavar="STAGE",
    help="Target stage (default: next in sequence).",
)
@click.option(
    "--override",
    "override_reason",
    default=None,
    metavar="REASON",
    help="Override a pending gate with this reason and advance anyway.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print gate evaluation without modifying the pursuit file.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the gate verdict as JSON.")
@click.help_option("-h", "--help")
def advance_cmd(
    pursuit_spec: str | None,
    account: str | None,
    name: str | None,
    target_stage: str | None,
    override_reason: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Evaluate the stage gate for a pursuit and advance it if the gate passes.

    PURSUIT_SPEC is a full path or '<account>/<slug>' shorthand, e.g.:

        fieldkit pursuit advance acme/deal-slug

    Alternatively, use --account and --name together:

        fieldkit pursuit advance --account acme --name deal-slug

    Checks the ratified current policy for the transition to the next stage
    (or --to STAGE), prints the decision, and updates the file in-place if the
    policy passes.

    Former score-dependent transitions remain pending until native ClosePlan
    policy is ratified. Use --override REASON to advance despite a pending gate.
    Use --dry-run to see what would happen without writing any changes.

    Exit codes:
      0 — policy passed and advancement written (or --dry-run)
      1 — policy pending (no override provided)
      3 — data error (file unreadable, unknown stage, etc.)
    """
    pursuit_file = _resolve_advance_target(pursuit_spec, account, name)

    try:
        fm, body, source = _read_frontmatter_snapshot(pursuit_file)
    except (ValueError, OSError):
        click.echo(f"{LOG_PREFIX} ERROR: pursuit source is not bounded UTF-8 frontmatter", err=True)
        raise SystemExit(EXIT_DATA) from None

    current_stage = str(fm.get("stage", "")).lower()
    resolved = _resolve_target_stage(current_stage, target_stage)
    if resolved is None:
        click.echo(f"{LOG_PREFIX} ERROR: '{current_stage}' has no defined next stage.", err=True)
        raise SystemExit(EXIT_DATA) from None
    target_stage = resolved

    # historic regression / historic regression: validate stage name and reject same-stage transitions.
    err_msg = _validate_stage_transition(current_stage, target_stage)
    if err_msg:
        click.echo(f"{LOG_PREFIX} ERROR: {err_msg}", err=True)
        raise SystemExit(EXIT_DATA) from None

    if not as_json:
        click.echo(f"\nPursuit:  {pursuit_file}")
        click.echo(f"Current:  {current_stage}")
        click.echo(f"Target:   {target_stage}")
        click.echo()

    passed, gate_status, note, reasons = _evaluate_gate(current_stage, target_stage, override_reason, quiet=as_json)
    if not as_json:
        click.echo()

    def _emit(advanced: bool) -> None:
        click.echo(
            json.dumps(
                {
                    "pursuit": str(pursuit_file),
                    "from_stage": current_stage,
                    "to_stage": target_stage,
                    "gate_passed": passed,
                    "gate_status": gate_status,
                    "reasons": list(reasons),
                    "override": override_reason,
                    "advanced": advanced,
                    "dry_run": dry_run,
                    "note": note,
                },
                indent=2,
                default=str,
            )
        )

    if dry_run:
        if passed:
            if as_json:
                _emit(advanced=False)
                raise SystemExit(0)
            click.echo(f"[dry-run] Would advance: {current_stage} → {target_stage}  gate-status={gate_status}")
            raise SystemExit(0)
        else:
            # The policy is pending, so include its reasons while preserving
            # the unchanged exit code.
            if as_json:
                _emit(advanced=False)
                raise SystemExit(EXIT_PARTIAL)
            click.echo("[dry-run] Gate pending — would NOT advance (use --override REASON to force)")
            raise SystemExit(EXIT_PARTIAL)  # implementation change: exit 1 when dry-run cannot advance

    if not passed:
        if as_json:
            _emit(advanced=False)
            raise SystemExit(EXIT_PARTIAL)
        click.echo(
            "Not advancing. Current transition policy is pending.\n"
            "Use --override REASON to advance with an explicit rationale."
        )
        raise SystemExit(EXIT_PARTIAL)

    _apply_transition(
        pursuit_file,
        fm,
        body,
        target_stage,
        gate_status,
        note,
        expected_mtime=source.info.st_mtime,
        expected_source_content=source.content,
    )
    if as_json:
        _emit(advanced=True)
        return
    click.echo(f"✓ Advanced: {current_stage} → {target_stage}  (gate-status: {gate_status})")
    click.echo(f"  Updated:  {pursuit_file}")


# Alias expected by the lazy-group dispatcher in cli.py
cli = advance_cmd
