"""Reports preserve provider failures and use explicit deterministic modes."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from fieldkit.brief import merged as brief_merged
from fieldkit.brief import pipeline_only as brief
from fieldkit.cli_exit import cli_main
from fieldkit.commands.brief import cli as brief_adapter
from fieldkit.commands.brief import generate as brief_generate
from fieldkit.commands.pipeline import cli as pipeline_adapter
from fieldkit.errors import EmptyOutputError, LLMError, LLMErrorCategory
from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta, stage2_extract
from fieldkit.pipeline import main as pipeline

pytestmark = pytest.mark.unit


@pytest.fixture
def report_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for module in (brief, pipeline):
        monkeypatch.setattr(module, "llm_disabled", lambda: False, raising=False)
        monkeypatch.setattr(module, "get_llm_model", lambda: "vertex_ai/test-model", raising=False)
    monkeypatch.setattr(brief, "get_fieldkit_home", lambda: tmp_path)
    for name in ("collect_pursuit_alerts", "collect_champion_signals", "collect_decay_signals", "collect_stale_prose"):
        monkeypatch.setattr(brief, name, lambda *args: "No current signals.")
    monkeypatch.setattr(brief, "collect_tasks", lambda *args: ("No tasks.", "Nothing waiting."))
    monkeypatch.setattr(brief, "_collect_degraded_sources", lambda *args: [])
    monkeypatch.setattr(pipeline, "collect_all_pursuit_data", lambda *args, **kwargs: ([], [], []))
    return tmp_path


@pytest.mark.parametrize(("category", "status"), [("auth", 2), ("rate-limit", 1), ("general", 3)])
@pytest.mark.parametrize("report", ["brief", "pipeline"])
def test_failed_provider_persists_safe_degraded_report_and_original_status(
    report_sources: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: LLMErrorCategory,
    status: int,
    report: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = brief if report == "brief" else pipeline
    failure = LLMError("private-provider-payload-sentinel", category)
    provider = Mock(side_effect=failure)
    monkeypatch.setattr(module, "synthesize", provider)

    with pytest.raises(SystemExit) as error, cli_main():
        if report == "brief":
            brief_adapter._run_pipeline_only(no_llm=False, account=None)
        else:
            pipeline_adapter._run(no_llm=False, data_root_override=report_sources)

    assert error.value.code == status
    provider.assert_called_once()
    documents = list((report_sources / "briefs").glob("*.md"))
    assert len(documents) == 1
    content = documents[0].read_text(encoding="utf-8")
    assert "[DEGRADED]" in content
    assert "private-provider-payload-sentinel" not in content
    assert "[LLM STUB]" not in content
    assert "private-provider-payload-sentinel" not in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["flag", "disabled", "no-model"])
@pytest.mark.parametrize("report", ["brief", "pipeline"])
def test_deterministic_report_does_not_call_provider(
    report_sources: Path, monkeypatch: pytest.MonkeyPatch, mode: str, report: str
) -> None:
    module = brief if report == "brief" else pipeline
    monkeypatch.setattr(module, "llm_disabled", lambda: mode == "disabled", raising=False)
    monkeypatch.setattr(
        module, "get_llm_model", lambda: None if mode == "no-model" else "vertex_ai/test-model", raising=False
    )
    provider = Mock(side_effect=AssertionError("deterministic report called provider"))
    monkeypatch.setattr(module, "synthesize", provider)
    if report == "brief":
        brief_adapter._run_pipeline_only(no_llm=mode == "flag", account=None)
    else:
        pipeline_adapter._run(no_llm=mode == "flag", data_root_override=report_sources)
    provider.assert_not_called()
    documents = list((report_sources / "briefs").glob("*.md"))
    assert len(documents) == 1
    content = documents[0].read_text(encoding="utf-8")
    assert "[LLM STUB]" not in content
    assert "[DEGRADED]" not in content
    if report == "pipeline":
        assert "## Pursuit Health Table" in content
        assert "## Narrative Summary" not in content


def test_pipeline_renderer_propagates_original_provider_failure(
    report_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = LLMError("synthetic provider failure", "auth")
    monkeypatch.setattr(pipeline, "synthesize", Mock(side_effect=failure))
    with pytest.raises(LLMError, match="synthetic provider failure") as error:
        pipeline.render_full_brief(
            [], champion_signals=[], blindspot_data=[], no_llm=False, today=datetime.now(UTC).date()
        )
    assert error.value is failure


def test_merged_brief_does_not_hide_pipeline_auth_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failure = LLMError("synthetic provider failure", "auth")
    monkeypatch.setattr(brief_merged, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(brief_merged, "collect_all_pursuit_data", lambda *args, **kwargs: ([], [], []))
    monkeypatch.setattr(brief_merged, "render_full_brief", Mock(side_effect=failure))
    with pytest.raises(LLMError, match="synthetic provider failure") as error:
        brief_merged._collect_pipeline_review(no_llm=False)
    assert error.value is failure


def test_merged_local_collection_failure_logs_bounded_cause_without_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(brief_merged, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(
        brief_merged,
        "collect_all_pursuit_data",
        Mock(side_effect=RuntimeError("private-payload-sentinel")),
    )

    result = brief_merged._collect_pipeline_review(no_llm=True)

    assert result.startswith("[Pipeline Review] unavailable:")
    assert "RuntimeError" in caplog.text
    assert "private-payload-sentinel" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_pipeline_only_dry_run_skips_provider_and_writes_nothing(
    report_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = Mock(side_effect=AssertionError("dry run called provider"))
    monkeypatch.setattr(brief, "synthesize", provider)

    brief_adapter._run_pipeline_only(no_llm=False, account=None, dry_run=True)

    provider.assert_not_called()
    assert not (report_sources / "briefs").exists()


@pytest.fixture
def merged_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(brief_merged, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(brief_merged, "get_accounts_config", lambda: {"accounts": []})
    monkeypatch.setattr(brief_merged, "_parse_internal_domains", lambda config: set())
    monkeypatch.setattr(brief_merged, "resolve_user_email", lambda config: "user@example.com")
    monkeypatch.setattr(brief_merged, "get_mcp_endpoint", lambda name: None)
    monkeypatch.setattr(brief_merged, "_collect_alert_source", lambda *args, **kwargs: [])
    monkeypatch.setattr(brief_merged, "_collect_calendar_meetings", lambda *args, **kwargs: [])
    monkeypatch.setattr(brief_merged, "detect_cross_account_signals", lambda root: [])
    monkeypatch.setattr(
        brief_merged, "render_brief", lambda **kwargs: f"# Merged brief\n\n{kwargs['pipeline_review_md']}\n"
    )
    monkeypatch.setattr("fieldkit.watch.morning_brief.write_run_status", lambda **kwargs: "written")
    return tmp_path


@pytest.mark.parametrize(("category", "status"), [("auth", 2), ("rate-limit", 1), ("general", 3)])
def test_merged_provider_failure_persists_deterministic_brief_and_original_category(
    merged_sources: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    category: LLMErrorCategory,
    status: int,
) -> None:
    status_write = Mock(return_value="written")
    monkeypatch.setattr("fieldkit.watch.morning_brief.write_run_status", status_write)
    failure = LLMError("private-provider-payload-sentinel", category)

    def failed_review(*, no_llm: bool, account: str | None = None, fallback_review: list[str] | None = None) -> str:
        assert not no_llm
        assert fallback_review is not None
        fallback_review.append("## Deterministic pipeline review")
        raise failure

    review = Mock(side_effect=failed_review)
    monkeypatch.setattr(brief_merged, "_collect_pipeline_review", review)
    with pytest.raises(SystemExit) as error, cli_main():
        brief_generate._run_generate_inner(date_str="2026-09-29", dry_run=False, verbose=False, no_llm=False)

    assert error.value.code == status
    review.assert_called_once()
    output = merged_sources / "briefs" / "morning-brief-2026-09-29.md"
    content = output.read_text(encoding="utf-8")
    assert "[DEGRADED]" in content
    assert "Deterministic pipeline review" in content
    assert "private-provider-payload-sentinel" not in content
    assert "private-provider-payload-sentinel" not in capsys.readouterr().err
    assert status_write.call_args.kwargs["outcome"] == "partial"
    assert status_write.call_args.kwargs["failures"] >= 1


def test_merged_provider_failure_collects_pipeline_once(
    merged_sources: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = LLMError("synthetic provider failure", "auth")
    collected = Mock(return_value=([], [], []))
    rendered = Mock(side_effect=[failure, "## Deterministic pipeline review"])
    monkeypatch.setattr(brief_merged, "collect_all_pursuit_data", collected)
    monkeypatch.setattr(brief_merged, "render_full_brief", rendered)

    with pytest.raises(LLMError) as error:
        brief_generate._run_generate_inner(date_str="2026-09-29", dry_run=False, verbose=False, no_llm=False)

    assert error.value is failure
    collected.assert_called_once()
    assert [call.kwargs["no_llm"] for call in rendered.call_args_list] == [False, True]


def test_merged_empty_provider_response_persists_degraded_brief(
    merged_sources: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(brief_merged, "collect_all_pursuit_data", lambda *args, **kwargs: ([], [], []))
    monkeypatch.setattr(pipeline, "llm_disabled", lambda: False)
    monkeypatch.setattr(pipeline, "get_llm_model", lambda: "vertex_ai/test-model")
    monkeypatch.setattr(pipeline, "synthesize", lambda *args, **kwargs: "   ")

    with pytest.raises(LLMError, match="empty pipeline narrative") as error:
        brief_generate._run_generate_inner(date_str="2026-09-29", dry_run=False, verbose=False, no_llm=False)

    assert error.value.category == "general"
    content = (merged_sources / "briefs" / "morning-brief-2026-09-29.md").read_text(encoding="utf-8")
    assert "[DEGRADED]" in content
    assert "private-provider-payload-sentinel" not in content


def test_merged_dry_run_never_calls_provider_or_writes(
    merged_sources: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(brief_merged, "collect_all_pursuit_data", lambda *args, **kwargs: ([], [], []))
    monkeypatch.setattr(pipeline, "llm_disabled", lambda: False)
    monkeypatch.setattr(pipeline, "get_llm_model", lambda: "vertex_ai/test-model")
    provider = Mock(side_effect=AssertionError("dry run called provider"))
    monkeypatch.setattr(pipeline, "synthesize", provider)

    result = brief_generate._run_generate_inner(date_str="2026-09-29", dry_run=True, verbose=False, no_llm=False)

    assert result.completed is True
    provider.assert_not_called()
    assert not (merged_sources / "briefs").exists()


@pytest.mark.parametrize("payload", ["[]", "null", "42", '"text"', '{"participants": [42]}', '{"confidence": []}'])
def test_stage2_invalid_payload_shape_returns_empty_low_confidence(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", lambda *args, **kwargs: payload)
    result = stage2_extract(Stage1Result("Fictional meeting notes."))
    assert result == TranscriptMeta(confidence="low")


def test_stage2_disabled_does_not_call_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    provider = Mock(side_effect=AssertionError("disabled extraction called provider"))
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", provider)
    result = stage2_extract(Stage1Result("Fictional meeting notes."))
    assert result == TranscriptMeta(confidence="stub")
    provider.assert_not_called()


@pytest.mark.parametrize("response", ["", "   \n"])
def test_empty_pipeline_response_persists_degraded_report_and_exits_three(
    report_sources: Path, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setattr(pipeline, "synthesize", lambda *args, **kwargs: response)
    with pytest.raises(SystemExit) as error, cli_main():
        pipeline_adapter._run(no_llm=False, data_root_override=report_sources)
    assert error.value.code == 3
    documents = list((report_sources / "briefs").glob("*.md"))
    assert len(documents) == 1
    assert "[DEGRADED]" in documents[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("report", ["brief", "pipeline"])
def test_failed_atomic_publication_preserves_prior_report_without_success_output(
    report_sources: Path, monkeypatch: pytest.MonkeyPatch, report: str, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = report_sources / "briefs"
    output_dir.mkdir()
    prefix = "morning-brief" if report == "brief" else "pipeline-review"
    output = output_dir / f"{prefix}-{datetime.now(UTC).date().isoformat()}.md"
    output.write_text("# Previously reviewed report\n", encoding="utf-8")
    before = output.read_bytes()

    def fail_publication(_temporary: Path, _destination: Path) -> Path:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(Path, "replace", fail_publication)
    with pytest.raises(OSError, match="synthetic publication failure"):
        if report == "brief":
            brief_adapter._run_pipeline_only(no_llm=True, account=None)
        else:
            pipeline_adapter._run(no_llm=True, data_root_override=report_sources)
    assert output.read_bytes() == before
    assert list(output_dir.glob("*.tmp")) == []
    captured = capsys.readouterr()
    assert "Brief saved:" not in captured.out
    assert "Saved to:" not in captured.err


def test_empty_brief_body_does_not_replace_prior_report(
    report_sources: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = report_sources / "briefs"
    output_dir.mkdir()
    output = output_dir / f"morning-brief-{datetime.now(UTC).date().isoformat()}.md"
    output.write_text("# Previously reviewed report\n", encoding="utf-8")
    monkeypatch.setattr(brief, "_render_no_llm_brief", lambda **kwargs: "")
    with pytest.raises(EmptyOutputError, match="Empty output detected"):
        brief_adapter._run_pipeline_only(no_llm=True, account=None)
    assert output.exists(), "empty replacement removed the previously reviewed report"
    assert output.read_text(encoding="utf-8") == "# Previously reviewed report\n"
    assert "Brief saved:" not in capsys.readouterr().out


@pytest.mark.parametrize("report", ["brief", "pipeline"])
def test_publication_verifies_nonempty_output_before_success(
    report_sources: Path, monkeypatch: pytest.MonkeyPatch, report: str, capsys: pytest.CaptureFixture[str]
) -> None:
    module = brief if report == "brief" else pipeline

    def broken_writer(path: Path, _content: str) -> None:
        path.write_text("", encoding="utf-8")

    monkeypatch.setattr(module, "atomic_text_write", broken_writer, raising=False)
    with pytest.raises(EmptyOutputError, match="Empty output detected"):
        if report == "brief":
            brief_adapter._run_pipeline_only(no_llm=True, account=None)
        else:
            pipeline_adapter._run(no_llm=True, data_root_override=report_sources)
    assert list((report_sources / "briefs").glob("*.md")) == []
    captured = capsys.readouterr()
    assert "Brief saved:" not in captured.out
    assert "Saved to:" not in captured.err
