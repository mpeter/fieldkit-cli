"""Behavioral tests for the repository audit checks."""

from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import audit_check

AuditCheck = Callable[[Path], list[str]]


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("check", "relative_path", "violation", "conforming"),
    [
        (
            audit_check.check_A01_no_bare_exception_subclasses,
            "src/fieldkit/sample.py",
            "class SampleError(Exception):\n    pass\n",
            "class SampleError(FieldkitError):\n    pass\n",
        ),
        (
            audit_check.check_A02_no_future_annotations,
            "src/fieldkit/sample.py",
            "from __future__ import annotations\n",
            "VALUE = 1\n",
        ),
        (
            audit_check.check_A03_no_subprocess_without_timeout,
            "src/fieldkit/sample.py",
            "import subprocess\nsubprocess.run(['true'])\n",
            "import subprocess\nsubprocess.run(['true'], timeout=1)\n",
        ),
        (
            audit_check.check_A04_no_hardcoded_org_domain,
            "src/fieldkit/sample.py",
            "",
            "ADDRESS = 'user@example.com'\n",
        ),
        (
            audit_check.check_A05_sf_retry_covers_gateway_errors,
            "src/fieldkit/config/retry.py",
            "RETRY_TRANSIENT_STATUSES = frozenset({500, 502})\n",
            "RETRY_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})\n",
        ),
        (
            audit_check.check_A06_no_domain_sys_exit,
            "src/fieldkit/sample.py",
            "raise SystemExit(1)\n",
            "raise SampleError('failed')\n",
        ),
        (
            audit_check.check_A07_pursuit_frontmatter_sf_fields_typed,
            "src/fieldkit/pursuit/models.py",
            "sf_amount: Any | None = None\n",
            "sf_amount: float | None = None\n",
        ),
        (
            audit_check.check_A08_prompt_injection_guards,
            "src/fieldkit/commands/brief/main.py",
            "PROMPT = 'brief'\n",
            "from fieldkit.llm.sanitize import wrap_user_data\n",
        ),
    ],
    ids=[f"A{number:02d}" for number in range(1, 9)],
)
def test_audit_check_detects_violation_and_accepts_conforming_fixture(
    tmp_path: Path,
    check: AuditCheck,
    relative_path: str,
    violation: str,
    conforming: str,
) -> None:
    if check is audit_check.check_A04_no_hardcoded_org_domain:
        organization_domain = "red" + "hat-" + "internal.com"
        violation = f"ADDRESS = 'user@{organization_domain}'\n"

    if check is audit_check.check_A08_prompt_injection_guards:
        for guarded_path in (
            "src/fieldkit/ingest/pipeline.py",
            "src/fieldkit/commands/pipeline/render.py",
        ):
            _write(tmp_path, guarded_path, "from fieldkit.llm.sanitize import wrap_user_data\n")

    _write(tmp_path, relative_path, violation)
    violations = check(tmp_path)
    assert violations

    _write(tmp_path, relative_path, conforming)
    conforming_result = check(tmp_path)
    assert conforming_result == []


@pytest.mark.unit
def test_a05_reports_missing_retry_policy_declaration(tmp_path: Path) -> None:
    _write(tmp_path, "src/fieldkit/config/retry.py", "RETRY_MAX_ATTEMPTS = 3\n")
    violations = audit_check.check_A05_sf_retry_covers_gateway_errors(tmp_path)
    assert violations == ["src/fieldkit/config/retry.py: RETRY_TRANSIENT_STATUSES not found"]


@pytest.mark.unit
def test_a05_ignores_commented_and_string_lookalikes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/fieldkit/config/retry.py",
        """# RETRY_TRANSIENT_STATUSES = frozenset({500, 502, 504})
EXAMPLE = "RETRY_TRANSIENT_STATUSES = frozenset({500, 502, 504})"
RETRY_TRANSIENT_STATUSES = frozenset({429, 500, 502})
""",
    )
    violations = audit_check.check_A05_sf_retry_covers_gateway_errors(tmp_path)
    assert violations == ["src/fieldkit/config/retry.py: RETRY_TRANSIENT_STATUSES missing [504]"]
