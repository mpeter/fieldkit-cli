"""Tests for _run_generate_inner() — CRAP coverage gate.

Target: bring line coverage from 54% to ≥ 75% to reduce CRAP score below 15.

Branches covered:
1. Happy path — all sources available, brief written successfully → returns 0
2. Config load failure (accounts.yaml missing) → returns 1
3. dry_run=True → brief printed to stdout, not written to disk
4. backstory_result as string error (source unavailable) → still completes
5. pipeline_review_result containing error prefix → degraded mode counted
6. Invalid --date value → returns 1
"""

from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock, _patch, patch

import pytest

from fieldkit.watch._morning_brief_types import BriefWriteResult, SourceNotReady
from fieldkit.watch.status import WatcherRunResult

pytestmark = pytest.mark.unit

_MOD = "fieldkit.brief.merged"


def test_merged_generator_exposes_typed_domain_result() -> None:
    from fieldkit.brief.merged import MergedBriefResult, generate_merged_brief

    assert callable(generate_merged_brief)
    assert MergedBriefResult.__name__ == "MergedBriefResult"


def test_merged_domain_preview_returns_content_without_cli_output_or_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.brief.merged import MergedBriefResult, generate_merged_brief

    with _apply_patches(_make_patches()) as mocks:
        result = generate_merged_brief(date_str="2026-06-24", dry_run=True)

    assert isinstance(result, MergedBriefResult)
    assert result.run == WatcherRunResult("ok", True, None)
    assert result.target_date is not None and result.target_date.isoformat() == "2026-06-24"
    assert result.text == "# Morning Brief\n\nContent."
    assert result.path is None
    assert result.write_result is None
    assert result.provider_failure is None
    assert capsys.readouterr().out == ""
    mocks[f"{_MOD}._write_brief_to_disk"].assert_not_called()


class _PatchOptions(TypedDict):
    return_value: object


# Minimal config returned by get_accounts_config in all happy-path tests.
_ACCOUNTS_CONFIG = {
    "accounts": [{"slug": "acme-corp", "domain": "acme-corp.example.com"}],
    "internal_domains": ["your-org.example.com"],
    "user_email": "ae@your-org.example.com",
}


def _make_patches(
    *,
    dry_run: bool = False,
    backstory_result: list[str] | SourceNotReady | str | None = None,
    pipeline_result: str = "# Pipeline Review\n\nAll good.",
    cross_account: list[object] | None = None,
    write_result: BriefWriteResult | None = None,
    config: dict[str, object] | None | bool = None,  # False → falsy config (failure case)
) -> list[tuple[str, _PatchOptions]]:
    """Return a list of (target, kwargs) tuples ready for patch()."""
    if backstory_result is None:
        backstory_result = ["## 2026-06-24 — acme / deal — stalled\n\n- Days: 5"]
    if cross_account is None:
        cross_account = []
    if write_result is None:
        write_result = BriefWriteResult(True, WatcherRunResult("ok", True, "written"), 7, 1, 0)

    cfg = _ACCOUNTS_CONFIG if config is None else config

    return [
        (f"{_MOD}.get_accounts_config", {"return_value": cfg}),
        (f"{_MOD}.get_mcp_endpoint", {"return_value": None}),
        (f"{_MOD}._parse_internal_domains", {"return_value": {"your-org.example.com"}}),
        (f"{_MOD}.resolve_user_email", {"return_value": "ae@your-org.example.com"}),
        (f"{_MOD}._collect_alert_source", {"return_value": backstory_result}),
        (f"{_MOD}._collect_calendar_meetings", {"return_value": []}),
        (f"{_MOD}._collect_pipeline_review", {"return_value": pipeline_result}),
        (f"{_MOD}.detect_cross_account_signals", {"return_value": cross_account}),
        (f"{_MOD}.render_brief", {"return_value": "# Morning Brief\n\nContent."}),
        (f"{_MOD}._write_brief_to_disk", {"return_value": write_result}),
        (f"{_MOD}.get_fieldkit_home", {"return_value": Path("/tmp/fieldkit")}),
        # Cache-decorated path helpers must be reset to avoid cross-test leakage.
        (f"{_MOD}._backstory_alerts_file", {"return_value": Path("/tmp/fieldkit/watchers/backstory-alerts.md")}),
        (
            f"{_MOD}._pursuit_stall_alerts_file",
            {"return_value": Path("/tmp/fieldkit/watchers/pursuit-stall-alerts.md")},
        ),
        (f"{_MOD}._slack_alerts_file", {"return_value": Path("/tmp/fieldkit/watchers/slack-thread-alerts.md")}),
        (
            f"{_MOD}._contract_expiry_alerts_file",
            {"return_value": Path("/tmp/fieldkit/watchers/contract-expiry-alerts.md")},
        ),
        (f"{_MOD}._draft_queue_alerts_file", {"return_value": Path("/tmp/fieldkit/watchers/draft-queue-alerts.md")}),
    ]


# ── TestRunMorningBriefInnerHappyPath (flattened) ───────────────────────────


def test_run_generate_inner_happy_path_returns_zero_on_success() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches()
    with _apply_patches(patches):
        result = _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    assert result.exit_code == 0


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("source", ["healthy", "not-ready", "alert-error", "pipeline-error"])
def test_preview_retains_actual_source_failure_outcome_without_writes(source: str, as_json: bool) -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    backstory: list[str] | SourceNotReady | str = []
    if source == "not-ready":
        backstory = SourceNotReady("Optional input absent")
    elif source == "alert-error":
        backstory = "Source unavailable"
    patches = _make_patches(
        backstory_result=backstory,
        pipeline_result="[Pipeline Review] unavailable: retry" if source == "pipeline-error" else "# Pipeline\n",
    )
    with _apply_patches(patches) as mocks, patch("fieldkit.watch.morning_brief.write_run_status") as status_writer:
        result = _run_generate_inner(date_str=None, dry_run=True, verbose=False, as_json=as_json)

    assert result == WatcherRunResult("partial" if source.endswith("error") else "ok", True, None)
    mocks[f"{_MOD}._write_brief_to_disk"].assert_not_called()
    status_writer.assert_not_called()


def test_run_generate_inner_happy_path_write_brief_to_disk_called() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches()
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    write_mock = mocks[f"{_MOD}._write_brief_to_disk"]
    assert write_mock.called, "_write_brief_to_disk must be called in non-dry-run mode"
    assert write_mock.call_args.kwargs["output_dir"] == Path("/tmp/fieldkit") / "briefs", (
        "brief generate must write to <fieldkit_home>/briefs/, not the legacy watchers/ directory"
    )


def test_run_generate_inner_happy_path_explicit_date_str_parsed() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches()
    with _apply_patches(patches):
        result = _run_generate_inner(date_str="2026-06-20", dry_run=False, verbose=False)

    assert result.exit_code == 0


def test_run_generate_inner_happy_path_render_brief_called_with_all_sources() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches()
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    render_mock = mocks[f"{_MOD}.render_brief"]
    assert render_mock.call_count == 1


def test_run_generate_inner_labels_unconfigured_optional_alert_inputs() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches()
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    collect_calls = mocks[f"{_MOD}._collect_alert_source"].call_args_list
    messages = {call.args[2]: call.kwargs.get("not_found_msg") for call in collect_calls}
    backstory_message = messages["Backstory alerts"]
    draft_message = messages["Draft Queue"]
    slack_message = messages["Slack"]
    assert isinstance(backstory_message, str)
    assert isinstance(draft_message, str)
    assert isinstance(slack_message, str)
    assert "endpoint is not configured" in backstory_message
    assert "endpoint is not configured" in draft_message
    assert "Select `--slack`" in slack_message


def test_run_generate_inner_passes_quota_collector_identity_to_render_brief() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    quota_collector = MagicMock()
    patches = _make_patches()
    with (
        patch(f"{_MOD}._collect_pursuits_for_quota", new=quota_collector),
        _apply_patches(patches) as mocks,
    ):
        _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    render_mock = mocks[f"{_MOD}.render_brief"]
    assert render_mock.call_args.kwargs["quota_collector"] is quota_collector


def test_run_generate_inner_threads_account_into_render_brief() -> None:
    """historic regression regression: render_brief() must receive the --account value so the
    Project Health section is scoped the same as pipeline review — not just
    _collect_pipeline_review()."""
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches()
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=False, verbose=False, account="acme-corp")

    render_mock = mocks[f"{_MOD}.render_brief"]
    assert render_mock.call_args.kwargs["account"] == "acme-corp"


# ── TestRunMorningBriefInnerConfigFailure (flattened) ───────────────────────


def test_run_generate_inner_config_failure_returns_one_when_config_missing() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    # config=None is the default "missing" case handled by get_accounts_config returning None
    with (
        patch(f"{_MOD}.get_accounts_config", return_value=None),
        patch(f"{_MOD}.get_fieldkit_home", return_value=Path("/tmp/fieldkit")),
    ):
        result = _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    assert result.exit_code == 1


def test_run_generate_inner_config_failure_returns_one_when_config_is_empty_dict() -> None:
    """Empty dict is falsy — treated as missing config."""
    from fieldkit.commands.brief.generate import _run_generate_inner

    with (
        patch(f"{_MOD}.get_accounts_config", return_value={}),
        patch(f"{_MOD}.get_fieldkit_home", return_value=Path("/tmp/fieldkit")),
    ):
        result = _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    assert result.exit_code == 1


# ── TestRunMorningBriefInnerDryRun (flattened) ──────────────────────────────


def test_run_generate_inner_dry_run_dry_run_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches(dry_run=True)
    with _apply_patches(patches):
        result = _run_generate_inner(date_str=None, dry_run=True, verbose=False)

    assert result.exit_code == 0


def test_run_generate_inner_dry_run_overrides_selected_llm() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches(dry_run=True)
    with _apply_patches(patches) as mocks:
        result = _run_generate_inner(
            date_str=None,
            dry_run=True,
            verbose=False,
            no_llm=False,
            calendar_enabled=True,
        )

    assert result.exit_code == 0
    assert mocks[f"{_MOD}._collect_pipeline_review"].call_args.kwargs["no_llm"] is True
    assert mocks[f"{_MOD}._collect_calendar_meetings"].call_args.kwargs["enabled"] is False


def test_run_generate_inner_dry_run_dry_run_prints_brief_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches(dry_run=True)
    with _apply_patches(patches):
        _run_generate_inner(date_str=None, dry_run=True, verbose=False)

    captured = capsys.readouterr()
    # render_brief returns "# Morning Brief\n\nContent." in our mock
    assert "Morning Brief" in captured.out


def test_run_generate_inner_dry_run_dry_run_skips_write_brief_to_disk() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches(dry_run=True)
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=True, verbose=False)

    write_mock = mocks[f"{_MOD}._write_brief_to_disk"]
    assert not write_mock.called, "_write_brief_to_disk must NOT be called in dry-run mode"


# ── TestRunMorningBriefInnerInvalidDate (flattened) ─────────────────────────


def test_run_generate_inner_invalid_date_invalid_date_returns_one() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    with (
        patch(f"{_MOD}.get_fieldkit_home", return_value=Path("/tmp/fieldkit")),
    ):
        result = _run_generate_inner(date_str="not-a-date", dry_run=False, verbose=False)

    assert result.exit_code == 1


def test_run_generate_inner_invalid_date_invalid_date_format_returns_one() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    with (
        patch(f"{_MOD}.get_fieldkit_home", return_value=Path("/tmp/fieldkit")),
    ):
        result = _run_generate_inner(date_str="06/24/2026", dry_run=False, verbose=False)

    assert result.exit_code == 1


# ── TestRunMorningBriefInnerBackstoryError (flattened) ──────────────────────


def test_run_generate_inner_backstory_error_propagates_partial_writer_result() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    backstory_error = "[Backstory alerts] unavailable: file not found"
    patches = _make_patches(
        backstory_result=backstory_error,
        write_result=BriefWriteResult(True, WatcherRunResult("partial", True, "written"), 7, 1, 1),
    )
    with _apply_patches(patches):
        result = _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    assert result.exit_code == 1


def test_run_generate_inner_backstory_error_backstory_string_passed_to_render_brief() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    backstory_error = "[Backstory alerts] unavailable: file not found"
    patches = _make_patches(backstory_result=backstory_error)
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    render_mock = mocks[f"{_MOD}.render_brief"]
    call_kwargs = render_mock.call_args.kwargs
    assert call_kwargs["backstory_alerts"] == backstory_error


# ── TestRunMorningBriefInnerPipelineError (flattened) ───────────────────────


def test_run_generate_inner_pipeline_error_propagates_partial_writer_result() -> None:
    from fieldkit.commands.brief.generate import _run_generate_inner

    pipeline_error = "[Pipeline Review] unavailable: no pursuits found"
    patches = _make_patches(
        pipeline_result=pipeline_error,
        write_result=BriefWriteResult(True, WatcherRunResult("partial", True, "written"), 7, 1, 1),
    )
    with _apply_patches(patches):
        result = _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    assert result.exit_code == 1


def test_run_generate_inner_pipeline_error_pipeline_error_counted_as_degraded_source() -> None:
    """When pipeline_review_result starts with the error prefix, it is passed as
    the error string to _write_brief_to_disk (not as the healthy [] sentinel)."""
    from fieldkit.commands.brief.generate import _run_generate_inner

    pipeline_error = "[Pipeline Review] unavailable: collect_all_pursuit_data raised"
    patches = _make_patches(pipeline_result=pipeline_error)
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    write_mock = mocks[f"{_MOD}._write_brief_to_disk"]
    # sources arg is the 4th positional (index 3)
    call_args = write_mock.call_args
    # Either positional or keyword
    sources_list = call_args.args[3] if len(call_args.args) > 3 else call_args.kwargs.get("sources", [])
    # The pipeline error string must appear in the sources list (not the [] sentinel)
    assert pipeline_error in sources_list, f"Expected pipeline error string in sources, got: {sources_list}"


def test_run_generate_inner_pipeline_error_pipeline_success_uses_empty_list_sentinel() -> None:
    """When pipeline review succeeds, [] sentinel is passed so it's not counted as a failure."""
    from fieldkit.commands.brief.generate import _run_generate_inner

    pipeline_ok = "# Pipeline Review\n\n## Acme Corp\n\nAll green."
    patches = _make_patches(pipeline_result=pipeline_ok)
    with _apply_patches(patches) as mocks:
        _run_generate_inner(date_str=None, dry_run=False, verbose=False)

    write_mock = mocks[f"{_MOD}._write_brief_to_disk"]
    call_args = write_mock.call_args
    sources_list = call_args.args[3] if len(call_args.args) > 3 else call_args.kwargs.get("sources", [])
    # The [] sentinel must appear, not the pipeline markdown
    assert [] in sources_list, f"Expected [] sentinel in sources for healthy pipeline, got: {sources_list}"


# ── TestRunMorningBriefInnerVerbose (flattened) ─────────────────────────────


def test_run_generate_inner_verbose_verbose_does_not_crash() -> None:
    import logging

    from fieldkit.commands.brief.generate import _run_generate_inner

    patches = _make_patches()
    original_level = logging.getLogger().level
    try:
        with _apply_patches(patches):
            result = _run_generate_inner(date_str=None, dry_run=False, verbose=True)
    finally:
        logging.getLogger().setLevel(original_level)

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Helper: context manager that applies a list of patches and returns mocks
# ---------------------------------------------------------------------------


class _apply_patches:
    """Context manager: apply list of (target, patch_kwargs) tuples, yield mock dict."""

    def __init__(self, patch_list: list[tuple[str, _PatchOptions]]) -> None:
        self._patch_list = patch_list
        self._patchers: list[_patch[MagicMock]] = []
        self._mocks: dict[str, MagicMock] = {}

    def __enter__(self) -> dict[str, MagicMock]:
        for target, kwargs in self._patch_list:
            p = patch(target, **kwargs)
            mock = p.start()
            self._patchers.append(p)
            self._mocks[target] = mock
        return self._mocks

    def __exit__(self, *args: object) -> None:
        for p in reversed(self._patchers):
            p.stop()
