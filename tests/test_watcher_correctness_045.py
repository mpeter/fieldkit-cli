"""Regression tests for Spec 045 watcher correctness fixes.

implementation note: contract_expiry.py excludes dot-prefixed account directories (.template/)
implementation note: scrub_duplicate_alerts() deduplicates pursuit-stall-alerts.md in-place;
        append_stall_alert() deduplicates within-day alerts
implementation note: contract_expiry._build_expiry_state_key() includes today's date for the
        expired tier so each calendar day produces a distinct key
"""

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# implementation note: .template/ exclusion in contract_expiry path-building
# ---------------------------------------------------------------------------


@pytest.mark.characterization
def test_contract_expiry_excludes_template_account(tmp_path: Path) -> None:
    """Dot-prefixed account directories must be excluded from contract_expiry scans.

    Calls the production watcher inner function with a fake workspace containing
    a ``.template/`` account. Asserts that no alert is fired for the template
    account (tests the production filter, not an inline copy of it).
    """
    from fieldkit.watch.contract_expiry import _run_contract_expiry_inner

    accounts_dir = tmp_path / "accounts"
    (accounts_dir / ".template" / "pursuits").mkdir(parents=True)
    # Template account has an expired contract — would alert if not filtered out
    (accounts_dir / ".template" / "pursuits" / "template.md").write_text(
        "---\nstage: qualify\nsf_close_date: 2020-01-01\n---\n", encoding="utf-8"
    )
    (accounts_dir / "acme-corp" / "pursuits").mkdir(parents=True)
    # Real account: far-future contract — should not alert
    (accounts_dir / "acme-corp" / "pursuits" / "renewal.md").write_text(
        "---\nstage: qualify\nsf_close_date: 2099-12-31\n---\n", encoding="utf-8"
    )
    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        patch("fieldkit.watch.contract_expiry.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.contract_expiry.get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.contract_expiry._load_state", return_value={}),
        patch("fieldkit.watch.contract_expiry._save_state"),
        patch("fieldkit.watch.contract_expiry.append_alert") as mock_alert,
        patch("fieldkit.watch.contract_expiry.write_run_status"),
    ):
        _run_contract_expiry_inner(
            account_filter=None,
            dry_run=True,
            critical=14,
            warning=30,
            notice=60,
        )

    # No alert must reference the .template account
    for call in mock_alert.call_args_list:
        result_arg = call.args[0] if call.args else {}
        account = result_arg.get("account", "") if isinstance(result_arg, dict) else ""
        assert ".template" not in account, f".template account must be excluded but alert fired for account={account!r}"


# ---------------------------------------------------------------------------
# implementation note: scrub_duplicate_alerts deduplication
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scrub_duplicate_alerts_removes_duplicates(tmp_path: Path) -> None:
    """scrub_duplicate_alerts() keeps first occurrence, removes duplicates."""
    from fieldkit.watch.dedup import scrub_duplicate_alerts

    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    alerts_file.write_text(
        "# Pursuit Stall Alerts\n\n"
        "## 2026-06-28 — acme-corp / deal-a — stalled in propose\n\n"
        "- **Account:** `acme-corp`\n\n"
        "## 2026-06-28 — acme-corp / deal-b — stalled in evaluate\n\n"
        "- **Account:** `acme-corp`\n\n"
        "## 2026-06-28 — acme-corp / deal-a — stalled in propose\n\n"
        "- **Account:** `acme-corp`\n\n",
        encoding="utf-8",
    )

    removed = scrub_duplicate_alerts(alerts_file)

    assert removed == 1, f"Expected 1 duplicate removed, got {removed}"
    content = alerts_file.read_text(encoding="utf-8")
    assert content.count("## 2026-06-28 — acme-corp / deal-a") == 1
    assert "## 2026-06-28 — acme-corp / deal-b" in content


@pytest.mark.unit
def test_scrub_duplicate_alerts_noop_on_clean_file(tmp_path: Path) -> None:
    """scrub_duplicate_alerts() returns 0 and does not modify a clean file."""
    from fieldkit.watch.dedup import scrub_duplicate_alerts

    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    original = (
        "# Pursuit Stall Alerts\n\n"
        "## 2026-06-28 — acme-corp / deal-a — stalled in propose\n\n"
        "- **Account:** `acme-corp`\n\n"
    )
    alerts_file.write_text(original, encoding="utf-8")

    removed = scrub_duplicate_alerts(alerts_file)

    assert removed == 0
    assert alerts_file.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_stall_alert_dedup_skips_existing_entry(tmp_path: Path) -> None:
    """append_stall_alert() must not append a duplicate for today's existing heading."""
    from fieldkit.watch._pursuit_stall_render import append_stall_alert

    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    alerts_file.write_text(
        f"# Pursuit Stall Alerts\n\n"
        f"## {today} — acme-corp / renewal-deal — stalled in propose\n\n"
        f"- **Account:** `acme-corp`\n\n",
        encoding="utf-8",
    )
    initial_size = alerts_file.stat().st_size

    result: dict = {
        "account": "acme-corp",
        "pursuit": "renewal-deal",
        "stage": "propose",
        "days_since_transition": 20,
        "last_transition_date": "2026-06-01",
        "threshold_days": 14,
        "path": "accounts/acme-corp/pursuits/renewal-deal.md",
        "champion": "(unknown)",
        "sf_next_steps": "(none)",
        "native_qualification": "unavailable (no Salesforce opportunity link)",
    }

    with (
        patch("fieldkit.watch._pursuit_stall_render._alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        append_stall_alert(result, dry_run=False)

    assert alerts_file.stat().st_size == initial_size, (
        "File grew — duplicate alert was appended when it should have been skipped"
    )


# ---------------------------------------------------------------------------
# implementation note: date-keyed expired state key in contract_expiry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_expiry_state_key_includes_today() -> None:
    """_build_expiry_state_key() must include the injected date for the expired tier.

    Passing different ``today`` values must produce different keys, preventing
    permanent within-band suppression after day 1. ``today`` is injected (not
    read from the system clock) ensuring timezone-consistency with UTC.
    No patching needed — the parameter is explicit.
    """
    from fieldkit.commands.watch.contract_expiry import _build_expiry_state_key

    class _FakePath:
        stem = "renewal"

    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    today = datetime.date.today()

    key_yesterday = _build_expiry_state_key(
        "acme-corp",
        _FakePath(),  # type: ignore[arg-type]
        "expired",
        -15,
        today=yesterday,
    )
    key_today = _build_expiry_state_key(
        "acme-corp",
        _FakePath(),  # type: ignore[arg-type]
        "expired",
        -15,
        today=today,
    )

    assert key_yesterday != key_today, (
        f"Keys must differ across dates to allow daily re-alerting. yesterday={key_yesterday!r}  today={key_today!r}"
    )
    assert today.isoformat() in key_today, f"Today's date {today!r} must appear in the expired key: {key_today!r}"


# ---------------------------------------------------------------------------
# Story C: CLI tests for --scrub-duplicates flag (Spec 049)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scrub_duplicates_cli_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--scrub-duplicates short-circuits the normal scan and reports scrubbed count."""
    from click.testing import CliRunner

    from fieldkit.commands.watch.pursuit_stalls import cli

    # Build alerts file with 2 duplicate blocks using the exact heading format
    # that scrub_duplicate_alerts() matches: any line starting with "## "
    alerts_file = tmp_path / "alerts.md"
    block = "## 2026-07-05 — acme / deal-123 — stalled in discovery\n\n- **Account:** `acme`\n\n"
    alerts_file.write_text(block + block, encoding="utf-8")

    from unittest.mock import patch

    import fieldkit.commands.watch.pursuit_stalls as _wps_cmd

    # Track whether _run_pursuit_stalls is called.
    called: list[bool] = []

    with (
        patch.object(_wps_cmd, "_alerts_file", return_value=alerts_file),
        patch.object(_wps_cmd, "_run_pursuit_stalls", side_effect=lambda *a, **kw: called.append(True)),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["--scrub-duplicates"])  # boolean flag — no path arg

    assert result.exit_code == 0, result.output
    # Two identical blocks → 1 duplicate removed (scrub_duplicate_alerts counts removed blocks)
    assert "Scrubbed 1 duplicate" in result.output  # count must be present
    assert called == [], "Normal scan must not run when --scrub-duplicates is passed"


@pytest.mark.unit
def test_scrub_duplicates_positive_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: without --scrub-duplicates, _run_pursuit_stalls IS called."""
    from unittest.mock import patch

    from click.testing import CliRunner

    import fieldkit.commands.watch.pursuit_stalls as _wps_cmd
    from fieldkit.commands.watch.pursuit_stalls import cli

    # Track whether _run_pursuit_stalls is called.
    # Patch the commands module's binding (where cli() calls it from).
    called: list[bool] = []

    with (
        patch.object(_wps_cmd, "_run_pursuit_stalls", side_effect=lambda *a, **kw: called.append(True) or 0),
    ):
        # Monkeypatch config loading to avoid real filesystem reads
        monkeypatch.setattr(
            "fieldkit.watch.pursuit_stalls.get_accounts_config",
            lambda: {"accounts": {}},
        )
        monkeypatch.setattr(
            "fieldkit.watch.pursuit_stalls.was_run_today",
            lambda *a: False,
        )

        runner = CliRunner()
        runner.invoke(cli, [])  # no --scrub-duplicates

    assert called != [], "Positive control: _run_pursuit_stalls should be called on normal path"
