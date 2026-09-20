"""Tests for the live work-order done-check validator."""

from pathlib import Path

import pytest

import scripts.check_work_order_done_checks as checker


def _write(path: Path, frontmatter: str, body: str = "") -> None:
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


@pytest.mark.unit
def test_live_inventory_excludes_documents_without_issues(tmp_path: Path) -> None:
    _write(
        tmp_path / "live.md",
        'issues: ["#1"]\ndone_checks:\n  version: 1\n  checks:\n    - id: exists\n      argv: [test, -f, x]',
    )
    _write(tmp_path / "AUTHORING.md", "audience: developer")

    paths = checker.live_work_orders(tmp_path)

    assert [path.name for path in paths] == ["live.md"]


@pytest.mark.unit
def test_validator_reports_missing_contract_and_checker(tmp_path: Path) -> None:
    work_orders = tmp_path / "work-orders"
    work_orders.mkdir()
    _write(work_orders / "missing.md", 'issues: ["#1"]')
    _write(
        work_orders / "checker.md",
        'issues: ["#2"]\ndone_checks:\n  version: 1\n  checks:\n    - id: compound\n      checker: scripts/done_checks/missing.py',
    )

    errors = checker.validate_work_orders(work_orders, tmp_path)

    assert len(errors) == 2
    assert any("done_checks" in error for error in errors)
    assert any("checker does not exist" in error for error in errors)


@pytest.mark.unit
def test_validator_rejects_nonisolated_referenced_checker(tmp_path: Path) -> None:
    work_orders = tmp_path / "work-orders"
    checker_root = tmp_path / "scripts" / "done_checks"
    work_orders.mkdir()
    checker_root.mkdir(parents=True)
    (checker_root / "unsafe.py").write_text("import subprocess\n", encoding="utf-8")
    _write(
        work_orders / "unsafe.md",
        'issues: ["#1"]\ndone_checks:\n  version: 1\n  checks:\n    - id: compound\n      checker: scripts/done_checks/unsafe.py',
    )

    errors = checker.validate_work_orders(work_orders, tmp_path)

    assert any("forbidden import 'subprocess'" in error for error in errors)


@pytest.mark.unit
def test_validator_does_not_execute_legacy_fence(tmp_path: Path) -> None:
    work_orders = tmp_path / "work-orders"
    work_orders.mkdir()
    sentinel = tmp_path / "executed"
    _write(
        work_orders / "safe.md",
        'issues: ["#1"]\ndone_checks:\n  version: 1\n  checks:\n    - id: exists\n      argv: [test, -f, README.md]',
        f"```bash\ntouch {sentinel}\n```\n",
    )

    errors = checker.validate_work_orders(work_orders, tmp_path)

    assert errors == ()
    assert not sentinel.exists()


@pytest.mark.unit
def test_validator_reports_malformed_work_order_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "malformed.md").write_text(
        '---\nissues: ["#1"]\ndone_checks: [unterminated\n---\n# Work order\n', encoding="utf-8"
    )

    errors = checker.validate_work_orders(tmp_path, tmp_path)

    assert len(errors) == 1
    assert "invalid YAML frontmatter" in errors[0]


@pytest.mark.unit
@pytest.mark.parametrize("issues", ['"#1"', "[]", "[1]"])
def test_validator_rejects_invalid_issues_shape(tmp_path: Path, issues: str) -> None:
    _write(
        tmp_path / "invalid.md",
        f"issues: {issues}\ndone_checks:\n  version: 1\n  checks:\n    - id: exists\n      argv: [test, -f, README.md]",
    )

    errors = checker.validate_work_orders(tmp_path, tmp_path)

    assert len(errors) == 1
    assert "issues must be a nonempty list of strings" in errors[0]


@pytest.mark.unit
def test_frontmatter_parser_preserves_delimiter_text_in_block_scalars(tmp_path: Path) -> None:
    _write(
        tmp_path / "valid.md",
        'issues: ["#1"]\nnote: |\n  --- remains data\ndone_checks:\n  version: 1\n  checks:\n    - id: exists\n      argv: [test, -f, README.md]',
    )

    errors = checker.validate_work_orders(tmp_path, tmp_path)

    assert errors == ()
