"""Domain contracts for confined, snapshot-bound Salesforce frontmatter updates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fieldkit.errors import FieldkitError, FrontmatterStalenessError
from fieldkit.pursuit.io import write_frontmatter_raw
from fieldkit.sf.frontmatter import parse_salesforce_frontmatter_payload, update_salesforce_frontmatter

pytestmark = pytest.mark.unit


def _pursuit(workspace: Path, content: str | None = None) -> Path:
    path = workspace / "accounts" / "acme-corp" / "pursuits" / "expansion.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        content
        or '---\nstage: discover\ngate-status: pending\nsf_opportunity_id: "006000000000AAA"\n---\n\n# Expansion\n',
        encoding="utf-8",
    )
    return path


def _opportunity_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "ok",
        "opportunity_id": "006000000000AAA",
        "stage": "Propose",
    }
    payload.update(changes)
    return payload


def test_payload_rejects_mixed_account_and_opportunity_identity() -> None:
    payload = _opportunity_payload(account_id="001000000000AAA")

    with pytest.raises(FieldkitError, match="mixes account and opportunity identity"):
        parse_salesforce_frontmatter_payload(payload)


def test_payload_uses_canonical_opportunity_id_validation() -> None:
    with pytest.raises(FieldkitError, match="opportunity identity is invalid"):
        parse_salesforce_frontmatter_payload(_opportunity_payload(opportunity_id="001000000000AAA"))


def test_payload_accepts_known_non_frontmatter_opportunity_metadata() -> None:
    payload = parse_salesforce_frontmatter_payload(
        _opportunity_payload(owner_email="owner@example.com", sf_account_name="Acme Corp", sf_probability=40)
    )

    assert payload.record_kind == "opportunity"
    assert payload.values["owner_email"] == "owner@example.com"


def test_payload_accepts_known_non_frontmatter_account_metadata() -> None:
    payload = parse_salesforce_frontmatter_payload(
        {
            "status": "ok",
            "account_id": None,
            "account_name": "Acme Corp",
            "industry": "Technology",
            "segment": "Enterprise",
            "owner_email": "owner@example.com",
            "open_consulting_acv": 1000,
        }
    )

    assert payload.record_kind == "account"
    assert payload.values["account_name"] == "Acme Corp"


@pytest.mark.parametrize("percentage", [True, "nan", "inf", "-inf"])
def test_payload_rejects_non_finite_or_boolean_deal_split_percentage(percentage: object) -> None:
    with pytest.raises(FieldkitError, match=r"deal_splits\[0\]\.pct must be"):
        parse_salesforce_frontmatter_payload(
            _opportunity_payload(deal_splits=[{"offering": "Services", "pct": percentage}])
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stage", {"unexpected": "mapping"}),
        ("owner", ["unexpected", "list"]),
        ("arr", True),
        ("arr", float("nan")),
        ("arr", float("inf")),
        ("arr", {"unexpected": "mapping"}),
    ],
)
def test_payload_rejects_unsupported_or_nonfinite_opportunity_field_values(field: str, value: object) -> None:
    with pytest.raises(FieldkitError, match=field):
        parse_salesforce_frontmatter_payload(_opportunity_payload(**{field: value}))


@pytest.mark.parametrize("count", [True, -1, 1.5, "2", {"unexpected": "mapping"}])
def test_payload_rejects_invalid_account_open_opportunity_count(count: object) -> None:
    with pytest.raises(FieldkitError, match="open_opportunity_count"):
        parse_salesforce_frontmatter_payload(
            {
                "status": "ok",
                "account_id": "001000000000AAA",
                "industry": "Technology",
                "open_opportunity_count": count,
            }
        )


@pytest.mark.parametrize(
    ("payload_changes", "existing_id"),
    [
        ({"opportunity_id": "006000000000AAB"}, "006000000000AAA"),
        ({"opportunity_id": None}, "NOT-AN-OPPORTUNITY-ID"),
    ],
)
def test_direct_update_refuses_mismatched_or_invalid_existing_identity_without_writes(
    tmp_path: Path,
    payload_changes: dict[str, object],
    existing_id: str,
) -> None:
    workspace = tmp_path / "workspace"
    path = _pursuit(
        workspace,
        f'---\nstage: discover\ngate-status: pending\nsf_opportunity_id: "{existing_id}"\n---\n\n# Expansion\n',
    )
    original = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns
    raw_payload = _opportunity_payload()
    if payload_changes["opportunity_id"] is None:
        raw_payload.pop("opportunity_id")
    else:
        raw_payload.update(payload_changes)
    payload = parse_salesforce_frontmatter_payload(raw_payload)

    with pytest.raises(FieldkitError, match="opportunity identity"):
        update_salesforce_frontmatter(
            path,
            workspace=workspace,
            payload=payload,
            dry_run=False,
            expected_opportunity_id=None,
        )

    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_mtime


def test_update_rejects_target_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = _pursuit(tmp_path / "outside")
    payload = parse_salesforce_frontmatter_payload(_opportunity_payload())

    with pytest.raises(FieldkitError, match="approved workspace location"):
        update_salesforce_frontmatter(
            outside,
            workspace=workspace,
            payload=payload,
            dry_run=False,
            expected_opportunity_id=None,
        )


def test_update_rejects_double_frontmatter_without_repairing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    original = "---\nstage: discover\n---\n---\nsf_stage: Propose\n---\n\n# Body\n"
    path = _pursuit(workspace, original)
    payload = parse_salesforce_frontmatter_payload(_opportunity_payload())

    with pytest.raises(FieldkitError, match="multiple frontmatter blocks"):
        update_salesforce_frontmatter(
            path,
            workspace=workspace,
            payload=payload,
            dry_run=False,
            expected_opportunity_id=None,
        )

    assert path.read_text(encoding="utf-8") == original


def test_update_binds_write_to_exact_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    path = _pursuit(workspace)
    original = path.read_text(encoding="utf-8")
    payload = parse_salesforce_frontmatter_payload(_opportunity_payload())
    from fieldkit.sf import frontmatter

    def replace_before_write(
        path_arg: str | Path,
        frontmatter_values: dict[str, Any],
        body: str,
        *,
        expected_mtime: float | None = None,
        validated_content: str | None = None,
        validated_source_content: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        path.write_text(original + "\nchanged concurrently\n", encoding="utf-8")
        write_frontmatter_raw(
            path_arg,
            frontmatter_values,
            body,
            expected_mtime=expected_mtime,
            validated_content=validated_content,
            validated_source_content=validated_source_content,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(frontmatter, "write_frontmatter_raw", replace_before_write)

    with pytest.raises(FrontmatterStalenessError, match="changed after frontmatter validation"):
        update_salesforce_frontmatter(
            path,
            workspace=workspace,
            payload=payload,
            dry_run=False,
            expected_opportunity_id=None,
        )


def test_dry_run_returns_exact_plan_without_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = _pursuit(workspace)
    original = path.read_text(encoding="utf-8")
    payload = parse_salesforce_frontmatter_payload(_opportunity_payload())

    result = update_salesforce_frontmatter(
        path,
        workspace=workspace,
        payload=payload,
        dry_run=True,
        expected_opportunity_id="006000000000AAA",
    )

    assert result.written is False
    assert result.record_kind == "opportunity"
    assert result.keys == (
        "sf_opportunity_id",
        "sf_name",
        "sf_stage",
        "sf_close_date",
        "sf_arr",
        "sf_owner",
        "sf_next_steps",
        "sf_last_pulled",
        "sf_acv",
        "sf_consulting_acv",
        "sf_training_acv",
        "sf_opportunity_number",
        "sf_contract_type",
    )
    assert path.read_text(encoding="utf-8") == original


def test_account_payload_updates_only_workspace_account_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = workspace / "accounts" / "acme-corp" / "account.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: Acme Corp\n---\n\n# Account\n", encoding="utf-8")
    payload = parse_salesforce_frontmatter_payload(
        {
            "status": "ok",
            "account_id": "001000000000AAA",
            "industry": "Technology",
            "owner": "Example Owner",
            "open_opportunity_count": 2,
        }
    )

    result = update_salesforce_frontmatter(
        path,
        workspace=workspace,
        payload=payload,
        dry_run=False,
        expected_opportunity_id=None,
    )

    assert result.written is True
    written = path.read_text(encoding="utf-8")
    assert "sf_industry: Technology" in written
    assert "sf_owner: Example Owner" in written
    assert 'sf_open_opps: "2"' in written or "sf_open_opps: 2" in written
    assert json.loads(json.dumps(result.record_kind)) == "account"
