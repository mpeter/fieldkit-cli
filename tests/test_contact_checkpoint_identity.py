"""Synthetic checkpoint regressions; no account files or providers are consulted."""

import copy
import json
import time
from pathlib import Path

import pytest

from fieldkit.contact import _enrich_helpers as pipeline
from fieldkit.contact import enrich
from fieldkit.enrich import _helpers as persistence
from fieldkit.enrich import _io
from fieldkit.enrich.schema import EnrichmentCheckpoint

pytestmark = pytest.mark.unit


def contacts(count: int, account: str = "acme-corp") -> list[dict]:
    return [
        {
            "full_name": f"Example Person {i}",
            "company": "Acme Corp",
            "account": account,
            "email": f"person{i}@example.com",
            "source": "account-file",
        }
        for i in range(count)
    ]


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(pipeline, "enrich_dir", lambda: tmp_path)
    monkeypatch.setattr(persistence, "enrich_dir", lambda: tmp_path)
    monkeypatch.setattr(_io, "enrich_dir", lambda: tmp_path)
    monkeypatch.setattr(pipeline, "contacts_memory_dir", lambda: memory)
    monkeypatch.setattr(pipeline, "get_user_email", lambda: None)
    monkeypatch.setattr(pipeline, "build_domain_account_map", dict)
    monkeypatch.setattr(pipeline, "get_internal_domains", list)
    monkeypatch.setattr(pipeline, "_prepare_gmail_cache", lambda candidates: None)
    monkeypatch.setattr(pipeline, "query_gmail_cache", lambda contact: contact)
    monkeypatch.setattr(enrich, "migrate_legacy_memory_files", lambda: 0)
    monkeypatch.setattr(enrich, "load_raw_contacts", lambda: contacts(6))
    return tmp_path


def test_sequential_scopes_with_identical_raw_input_restart(state: Path) -> None:
    first = enrich.enrich_records()
    assert first.total_enriched == 6
    previous = json.loads((state / _io.CONTACTS_ENRICHED).read_text(encoding="utf-8"))
    memory_before = {p.name: p.read_bytes() for p in (state / "memory").iterdir()}
    second = enrich.enrich_records(account="acme-corp")
    assert second.total_enriched == 12
    written = json.loads((state / _io.CONTACTS_ENRICHED).read_text(encoding="utf-8"))
    assert written[:6] == previous
    assert set(memory_before) <= {p.name for p in (state / "memory").iterdir()}


def test_second_account_preserves_existing_data(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(enrich, "load_raw_contacts", lambda: contacts(6) + contacts(1, "other-corp"))
    first = enrich.enrich_records(account="acme-corp")
    assert first.total_enriched == 6
    previous = json.loads((state / _io.CONTACTS_ENRICHED).read_text(encoding="utf-8"))
    memory_before = {p.name: p.read_bytes() for p in (state / "memory").iterdir()}
    second = enrich.enrich_records(account="other-corp")
    assert second.total_enriched == 7
    assert json.loads((state / _io.CONTACTS_ENRICHED).read_text(encoding="utf-8"))[:6] == previous
    assert all((state / "memory" / name).read_bytes() == value for name, value in memory_before.items())


def test_same_input_noop_preserves_bytes(state: Path) -> None:
    raw = contacts(7)
    before = copy.deepcopy(raw)
    assert pipeline.run_enrichment_pipeline(raw) == (7, 0)
    snapshots = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    assert pipeline.run_enrichment_pipeline(raw) == (7, 0)
    assert raw == before
    assert all(p.read_bytes() == data for p, data in snapshots.items())


def test_interrupted_same_scope_resumes(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = pipeline.enrich_batch

    def interrupted(raw: list[dict], start: int):
        if start == 5:
            raise RuntimeError("synthetic interruption")
        return real(raw, start)

    monkeypatch.setattr(pipeline, "enrich_batch", interrupted)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        pipeline.run_enrichment_pipeline(contacts(7))
    checkpoint = persistence.load_checkpoint()
    assert checkpoint is not None
    assert checkpoint.total_processed == 5
    seen = []

    def tracking(raw: list[dict], start: int):
        seen.append(start)
        return real(raw, start)

    monkeypatch.setattr(pipeline, "enrich_batch", tracking)
    assert pipeline.run_enrichment_pipeline(contacts(7)) == (7, 0)
    assert seen == [5]


@pytest.mark.parametrize("change", ["order", "field", "shorter", "longer"])
def test_changed_raw_input_restarts(state: Path, change: str, caplog: pytest.LogCaptureFixture) -> None:
    assert pipeline.run_enrichment_pipeline(contacts(6)) == (6, 0)
    raw = contacts(6)
    if change == "order":
        raw.reverse()
    elif change == "field":
        raw[0]["title"] = "New title"
    elif change == "shorter":
        raw.pop()
    else:
        raw = contacts(7)
    assert pipeline.run_enrichment_pipeline(raw) == (6 + len(raw), 0)
    assert "checkpoint" in caplog.text.lower()


def test_dictionary_key_order_is_same_input(state: Path) -> None:
    assert pipeline.run_enrichment_pipeline(contacts(1)) == (1, 0)
    raw = [dict(reversed(list(contacts(1)[0].items())))]
    assert pipeline.run_enrichment_pipeline(raw) == (1, 0)


@pytest.mark.parametrize(
    "kind", ["legacy", "missing-scope", "missing-fingerprint", "unsupported", "beyond-end", "negative"]
)
def test_unusable_checkpoint_restarts(state: Path, kind: str, caplog: pytest.LogCaptureFixture) -> None:
    assert pipeline.run_enrichment_pipeline(contacts(1)) == (1, 0)
    path = state / "checkpoint.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if kind == "legacy":
        for key in ("checkpoint_version", "account_scope", "raw_contacts_fingerprint"):
            data.pop(key, None)
    elif kind == "unsupported":
        data["checkpoint_version"] = 99
    elif kind == "missing-fingerprint":
        data.pop("raw_contacts_fingerprint")
    elif kind == "missing-scope":
        data.pop("account_scope")
    elif kind == "beyond-end":
        data["total_processed"] = 100
    else:
        data["total_processed"] = -1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert pipeline.run_enrichment_pipeline(contacts(1)) == (2, 0)
    assert "checkpoint" in caplog.text.lower()


def test_empty_input_preserves_existing_state(state: Path) -> None:
    assert pipeline.run_enrichment_pipeline(contacts(1)) == (1, 0)
    snapshots = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    assert pipeline.run_enrichment_pipeline([]) == (0, 0)
    assert all(p.read_bytes() == data for p, data in snapshots.items())


def test_empty_first_run_creates_no_state(state: Path) -> None:
    assert pipeline.run_enrichment_pipeline([]) == (0, 0)
    assert list(state.iterdir()) == [state / "memory"]


def test_empty_account_filter_preserves_existing_state(state: Path) -> None:
    assert enrich.enrich_records().total_enriched == 6
    snapshots = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    result = enrich.enrich_records(account="missing-corp")
    assert result.total_raw_contacts == 0
    assert result.total_enriched == 0
    assert all(p.read_bytes() == data for p, data in snapshots.items())


def test_all_excluded_contacts_complete_without_enrichment(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "get_user_email", lambda: "person0@example.com")
    assert pipeline.run_enrichment_pipeline(contacts(1)) == (0, 0)
    checkpoint = persistence.load_checkpoint()
    assert checkpoint is not None
    assert checkpoint.total_processed == 1
    assert pipeline.run_enrichment_pipeline(contacts(1)) == (0, 0)
    assert list((state / "memory").iterdir()) == []


def test_failed_output_write_does_not_advance_checkpoint(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert pipeline.run_enrichment_pipeline(contacts(1)) == (1, 0)
    checkpoint_before = (state / "checkpoint.json").read_bytes()

    def failed_write(path: Path, value: object) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(pipeline, "_write_json_atomic", failed_write)
    with pytest.raises(OSError, match="synthetic disk failure"):
        pipeline.run_enrichment_pipeline(contacts(2))
    assert (state / "checkpoint.json").read_bytes() == checkpoint_before


def _checkpoint(**overrides: object) -> EnrichmentCheckpoint:
    fields: dict[str, object] = {
        "last_completed_account": "acme-corp",
        "last_completed_contact_index": 2,
        "total_processed": 2,
        "total_enriched": 2,
        "total_failed": 0,
        "checkpoint_version": 1,
        "account_scope": "acme-corp",
        "raw_contacts_fingerprint": "fp",
    }
    fields.update(overrides)
    return EnrichmentCheckpoint.model_validate(fields)


def test_resume_index_without_checkpoint_is_zero(caplog: pytest.LogCaptureFixture) -> None:
    index = pipeline._checkpoint_resume_index(None, "acme-corp", "fp", 4)
    assert index == 0
    assert "incompatible" not in caplog.text


@pytest.mark.parametrize("total", [2, 4])
def test_resume_index_matching_checkpoint_resumes(total: int) -> None:
    index = pipeline._checkpoint_resume_index(_checkpoint(), "acme-corp", "fp", total)
    assert index == 2


def test_resume_index_unscoped_run_matches_explicit_none_scope() -> None:
    index = pipeline._checkpoint_resume_index(_checkpoint(account_scope=None), None, "fp", 4)
    assert index == 2


@pytest.mark.parametrize(
    ("checkpoint", "account", "fingerprint", "total"),
    [
        pytest.param(_checkpoint(checkpoint_version=0), "acme-corp", "fp", 4, id="legacy-version"),
        pytest.param(
            EnrichmentCheckpoint.model_validate(
                {**_checkpoint().model_dump(exclude={"account_scope"}), "checkpoint_version": 1}
            ),
            None,
            "fp",
            4,
            id="scope-not-recorded",
        ),
        pytest.param(_checkpoint(), "globex", "fp", 4, id="scope-mismatch"),
        pytest.param(_checkpoint(), "acme-corp", "other", 4, id="fingerprint-mismatch"),
        pytest.param(_checkpoint(), "acme-corp", "fp", 1, id="offset-past-input"),
        pytest.param(_checkpoint(total_processed=-1), "acme-corp", "fp", 4, id="negative-offset"),
    ],
)
def test_resume_index_incompatible_checkpoint_restarts(
    checkpoint: EnrichmentCheckpoint,
    account: str | None,
    fingerprint: str,
    total: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    index = pipeline._checkpoint_resume_index(checkpoint, account, fingerprint, total)
    assert index == 0
    assert "incompatible enrichment checkpoint" in caplog.text


class _RecoveredRecord:
    def model_dump(self) -> dict[str, str]:
        return {"full_name": "Example Person 0", "account": "acme-corp"}


def test_failed_contact_recovered_on_retry_counts_as_enriched(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = contacts(1)
    failed = [{**raw[0], "retry_count": 1}]
    remembered: list[object] = []
    monkeypatch.setattr(pipeline, "enrich_batch", lambda contacts, start: ([], failed))
    monkeypatch.setattr(pipeline, "enrich_contact", lambda contact, retry_count: _RecoveredRecord())
    monkeypatch.setattr(pipeline, "write_to_memory", remembered.append)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    totals = pipeline.run_enrichment_pipeline(raw, account="acme-corp")

    assert totals == (1, 0)
    assert len(remembered) == 1
    assert json.loads((state / _io.CONTACTS_ENRICHED).read_text(encoding="utf-8")) == [
        {"full_name": "Example Person 0", "account": "acme-corp"}
    ]
