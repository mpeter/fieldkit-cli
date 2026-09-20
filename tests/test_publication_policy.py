"""Unit tests for the deterministic GitHub publication PII policy."""

import io
import json
import os
from functools import partial
from pathlib import Path

import pytest

import fieldkit.publication_policy as publication_policy
from fieldkit.publication_policy import (
    PayloadSource,
    ViolationCategory,
    evaluate_publication_command,
)

pytestmark = pytest.mark.unit


def _accounts() -> list[str]:
    return ["Northwind Demonstration"]


def _run_main(monkeypatch: pytest.MonkeyPatch, input_bytes: bytes) -> int:
    stdin = io.TextIOWrapper(io.BytesIO(input_bytes), encoding="utf-8")
    monkeypatch.setattr(publication_policy.sys, "stdin", stdin)
    monkeypatch.setattr(
        publication_policy,
        "evaluate_publication_command",
        partial(evaluate_publication_command, account_names_getter=_accounts),
    )
    return publication_policy.main()


@pytest.mark.parametrize(
    "command",
    [
        "gh issue create --title 'Safe title' --body 'Safe placeholder text'",
        "gh pr edit 7 --title 'Safe title'",
        "gh issue comment 7 --body 'Safe comment'",
        "fieldkit issue create --type bug --title 'Safe title' --body 'Safe body'",
        "uv run fieldkit issue edit historic regression --body 'Safe body'",
        "uv run fieldkit issue note historic regression 'Safe note'",
    ],
)
def test_evaluate_publication_command_allows_safe_inspectable_text(command: str, tmp_path: Path) -> None:
    result = evaluate_publication_command(command, cwd=tmp_path, account_names_getter=_accounts)

    assert result.allowed is True
    assert result.category is None
    assert result.source is None


@pytest.mark.parametrize(
    ("command", "category", "source"),
    [
        (
            "gh issue create --title 'Northwind Demonstration update' --body 'Safe body'",
            ViolationCategory.ACCOUNT_NAME,
            PayloadSource.TITLE,
        ),
        (
            "gh issue create --title 'Safe title' --body '" + "/home/" + "operator/work'",
            ViolationCategory.ABSOLUTE_HOME_PATH,
            PayloadSource.BODY,
        ),
        (
            "gh issue comment 7 --body 'from:" + "agent-handle reviewed this'",
            ViolationCategory.SLACK_HANDLE,
            PayloadSource.BODY,
        ),
        (
            "fieldkit issue note historic regression 'Northwind Demonstration update'",
            ViolationCategory.ACCOUNT_NAME,
            PayloadSource.BODY,
        ),
        (
            "gh issue create --title 'Safe title' --body 'pii-guard: ignore Northwind Demonstration'",
            ViolationCategory.ACCOUNT_NAME,
            PayloadSource.BODY,
        ),
    ],
)
def test_evaluate_publication_command_blocks_pii_category_only(
    command: str, category: ViolationCategory, source: PayloadSource, tmp_path: Path
) -> None:
    result = evaluate_publication_command(command, cwd=tmp_path, account_names_getter=_accounts)

    assert result.allowed is False
    assert result.category is category
    assert result.source is source
    assert "Northwind Demonstration" not in repr(result)


def test_evaluate_publication_command_blocks_personal_email(tmp_path: Path) -> None:
    email = "employee" + "@private.invalid"
    result = evaluate_publication_command(
        f"gh issue comment 7 --body '{email}'", cwd=tmp_path, account_names_getter=_accounts
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.PERSONAL_EMAIL
    assert result.source is PayloadSource.BODY
    assert email not in repr(result)


def test_evaluate_publication_command_blocks_unsafe_regular_body_file(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Northwind Demonstration", encoding="utf-8")
    result = evaluate_publication_command(
        "gh pr create --title 'Safe title' --body-file body.md", cwd=tmp_path, account_names_getter=_accounts
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.ACCOUNT_NAME
    assert result.source is PayloadSource.BODY_FILE


def test_evaluate_publication_command_blocks_safe_regular_body_file(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Safe placeholder text", encoding="utf-8")
    result = evaluate_publication_command(
        "gh pr create --title 'Safe title' --body-file body.md", cwd=tmp_path, account_names_getter=_accounts
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.UNINSPECTABLE
    assert result.source is PayloadSource.BODY_FILE


def test_evaluate_publication_command_blocks_unreadable_body_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Safe body", encoding="utf-8")

    def _raise_permission_error(_: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", _raise_permission_error)
    result = evaluate_publication_command(
        "gh pr create --title 'Safe title' --body-file body.md", cwd=tmp_path, account_names_getter=_accounts
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.UNINSPECTABLE
    assert result.source is PayloadSource.BODY_FILE


@pytest.mark.parametrize(
    "command",
    [
        "gh issue create --title 'Safe title'",
        "gh issue create --body 'Safe body'",
        "gh issue comment 7 --editor",
        "gh pr create --title 'Safe title' --body 'Safe body' --fill",
        "gh issue create --title 'Safe title' --body $(cat body.md)",
        "gh issue create --title 'Safe title' --body 'Safe body' && date",
        "gh issue create --title 'Safe title' --body 'Safe body'\n date",
        "gh issue create --title 'Safe title' --body 'Safe body' --body 'Repeated body'",
        "gh issue comment 7 --body -",
        "gh issue create --title 'Safe title' --body 'unterminated",
        "g\\h issue create --title 'Safe title' --body 'person@example.com'",
        "\\gh issue create --title 'Safe title' --body 'person@example.com'",
        "env gh issue create --title 'Safe title' --body 'person@example.com'",
        "/usr/bin/gh issue create --title 'Safe title' --body 'person@example.com'",
        "g$GH_SUFFIX issue create --title 'Safe title' --body 'person@example.com'",
        "gh --repo owner/repo issue create --title 'Safe title' --body 'person@example.com'",
        "sh -c 'gh issue create --title Safe --body person@example.com'",
        "sh -c 'gh --repo owner/repo issue create --title Safe --body person@example.com'",
        "gh issue close 7 --comment 'Northwind Demonstration renewal details'",
        "gh issue reopen 7 --comment 'person@example.com'",
        "gh pr close 7 --comment 'person@example.com'",
        "gh pr review 7 --body 'person@example.com'",
        "gh pr merge 7 --subject 'person@example.com'",
        "fieldkit issue reopen historic regression --note person@example.com",
        "uv run fieldkit issue sync-milestone --commit person@example.com",
        "sh -c 'fieldkit issue reopen historic regression --note person@example.com'",
        'sh -c "g\\\\h issue create --title Safe --body person@example.com"',
    ],
)
def test_evaluate_publication_command_blocks_uninspectable_target(command: str, tmp_path: Path) -> None:
    result = evaluate_publication_command(command, cwd=tmp_path, account_names_getter=_accounts)

    assert result.allowed is False
    assert result.category is ViolationCategory.UNINSPECTABLE


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "invalid_utf8", "oversized"])
def test_evaluate_publication_command_blocks_uninspectable_body_file(kind: str, tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    if kind == "directory":
        body_file.mkdir()
    elif kind == "fifo":
        os.mkfifo(body_file)
    elif kind == "invalid_utf8":
        body_file.write_bytes(b"\xff")
    elif kind == "oversized":
        body_file.write_bytes(b"x" * (64 * 1024 + 1))
    command = "gh issue create --title 'Safe title' --body-file body.md"
    result = evaluate_publication_command(command, cwd=tmp_path, account_names_getter=_accounts)

    assert result.allowed is False
    assert result.category is ViolationCategory.UNINSPECTABLE
    assert result.source is PayloadSource.BODY_FILE


def test_evaluate_publication_command_allows_non_target_command(tmp_path: Path) -> None:
    result = evaluate_publication_command("git status --short", cwd=tmp_path, account_names_getter=_accounts)

    assert result.allowed is True


def test_evaluate_publication_command_blocks_command_builtin_target(tmp_path: Path) -> None:
    result = evaluate_publication_command(
        "command gh issue create --title 'Safe title' --body 'person@acme-corp.com'",
        cwd=tmp_path,
        account_names_getter=_accounts,
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.UNINSPECTABLE
    assert result.source is PayloadSource.COMMAND


@pytest.mark.parametrize(
    ("email", "blocked"),
    [
        ("admin@example.com", False),
        ("admin@acme-corp.com", True),
        ("employee@example.com", True),
        ("ab@acme-corp.com", True),
        ("1person@acme-corp.com", True),
    ],
)
def test_evaluate_publication_command_requires_fully_safe_placeholder_email(
    email: str, blocked: bool, tmp_path: Path
) -> None:
    result = evaluate_publication_command(
        f"gh issue comment 7 --body '{email}'", cwd=tmp_path, account_names_getter=_accounts
    )

    assert result.allowed is not blocked
    assert result.category is (ViolationCategory.PERSONAL_EMAIL if blocked else None)


def test_evaluate_publication_command_blocks_pii_in_fieldkit_issue_source(tmp_path: Path) -> None:
    email = "person@acme-corp.com"
    result = evaluate_publication_command(
        f"fieldkit issue create --type bug --title Safe --body Safe --source {email}",
        cwd=tmp_path,
        account_names_getter=_accounts,
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.PERSONAL_EMAIL
    assert result.source is PayloadSource.SOURCE


@pytest.mark.parametrize("action", ["close", "fix", "plan"])
def test_evaluate_publication_command_blocks_pii_in_fieldkit_issue_transition_note(action: str, tmp_path: Path) -> None:
    email = "1person@acme-corp.com"
    result = evaluate_publication_command(
        f"fieldkit issue {action} historic regression --note {email}", cwd=tmp_path, account_names_getter=_accounts
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.PERSONAL_EMAIL
    assert result.source is PayloadSource.BODY


def test_evaluate_publication_command_blocks_pii_in_fieldkit_issue_link_milestone(tmp_path: Path) -> None:
    email = "1person@acme-corp.com"
    result = evaluate_publication_command(
        f"fieldkit issue link historic regression {email}", cwd=tmp_path, account_names_getter=_accounts
    )

    assert result.allowed is False
    assert result.category is ViolationCategory.PERSONAL_EMAIL
    assert result.source is PayloadSource.TITLE


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("gh issue edit 123 --body ''", True),
        ("gh issue create --title 'Example' --body ''", True),
        ("gh issue edit 123 --body=", False),
    ],
)
def test_empty_payload_preserves_separated_and_equals_form_decisions(
    tmp_path: Path, command: str, allowed: bool
) -> None:
    result = evaluate_publication_command(command, cwd=tmp_path, account_names_getter=_accounts)

    assert result.allowed is allowed
    assert result.category is (None if allowed else ViolationCategory.UNINSPECTABLE)
    assert result.source is (None if allowed else PayloadSource.COMMAND)


def test_main_emits_only_policy_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    request = json.dumps({"command": "gh issue comment 7 --body Safe", "cwd": str(tmp_path)}).encode()

    result = _run_main(monkeypatch, request)

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"allowed": True, "category": None, "source": None}


def test_main_emits_denial_without_reflecting_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    request = json.dumps({"command": "gh issue comment 7 --body person@acme-corp.com", "cwd": str(tmp_path)}).encode()

    result = _run_main(monkeypatch, request)

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "allowed": False,
        "category": "personal_email",
        "source": "body",
    }


@pytest.mark.parametrize(
    "input_bytes",
    [
        b"not json",
        json.dumps(["not", "an", "object"]).encode(),
        json.dumps({"cwd": "."}).encode(),
        json.dumps({"command": "gh issue comment 7 --body Safe"}).encode(),
        json.dumps({"command": 1, "cwd": "."}).encode(),
        json.dumps({"command": "gh issue comment 7 --body Safe", "cwd": 1}).encode(),
    ],
)
def test_main_rejects_invalid_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], input_bytes: bytes
) -> None:
    result = _run_main(monkeypatch, input_bytes)

    assert result == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "input_bytes",
    [
        b"x" * (128 * 1024 + 1),
        json.dumps({"command": "gh issue comment 7 --body Safe", "cwd": "."}).encode() + b"\nextra",
    ],
)
def test_main_rejects_oversized_or_trailing_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], input_bytes: bytes
) -> None:
    result = _run_main(monkeypatch, input_bytes)

    assert result == 1
    assert capsys.readouterr().out == ""
