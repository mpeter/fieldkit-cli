"""Google Tasks read and mutation domain adapter."""

import datetime as dt
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal, cast

from fieldkit.config import TIMEOUT_GWS_CLI
from fieldkit.errors import AuthError, WebDataError

_TASK_LIST_TITLE = "fieldkit"
_MAX_PAGES = 100
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
_AUTH_MARKERS = ("auth", "credential", "unauthenticated", "log in", "login")


class GTaskValidationError(ValueError):
    """Raised when a local Google Tasks argument is invalid."""


@dataclass(frozen=True)
class GoogleTask:
    """Public Google Task record returned by the dashboard API."""

    id: str
    title: str
    notes: str
    status: str
    due: str | None
    completed: str | None
    section: Literal["today", "active", "other"]
    account: str | None

    def to_dict(self) -> dict[str, str | None]:
        """Return the JSON-safe public representation."""
        return asdict(self)


@dataclass(frozen=True)
class TaskMutation:
    """Preview or result of a Google Tasks mutation."""

    operation: Literal["create", "complete"]
    task_list_id: str
    task_id: str | None
    title: str | None
    section: Literal["today", "active"] | None
    account: str | None
    due: str | None
    confirmed: bool

    def to_dict(self) -> dict[str, str | bool | None]:
        """Return the JSON-safe public representation."""
        return asdict(self)


def run_gws(args: list[str]) -> str:
    """Run ``gws <args>`` and return stdout."""
    binary = shutil.which("gws")
    if binary is None:
        raise WebDataError("gws CLI not found on PATH — install gws and run the gws auth flow in a terminal")
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_GWS_CLI,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WebDataError(f"gws {' '.join(args[:3])} timed out after {TIMEOUT_GWS_CLI}s") from exc
    except OSError as exc:
        raise WebDataError(
            "gws could not start — verify the gws installation and run the gws auth flow in a terminal"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no stderr"
        message = f"gws {' '.join(args[:3])} exited {result.returncode}: {detail}; run the gws auth flow in a terminal"
        if any(marker in result.stderr.lower() for marker in _AUTH_MARKERS):
            raise AuthError(message)
        raise WebDataError(message)
    return result.stdout


def _page(stdout: str) -> tuple[list[object], str | None]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise WebDataError("gws produced non-JSON output") from exc
    if not isinstance(payload, dict):
        raise WebDataError("gws produced a non-object JSON payload")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise WebDataError("gws JSON payload has non-list items")
    token = payload.get("nextPageToken")
    if token is not None and (not isinstance(token, str) or not token):
        raise WebDataError("gws JSON payload has an invalid nextPageToken")
    return cast(list[object], items), token


def _pages(
    resource: list[str],
    params: dict[str, object],
    *,
    gws_runner: Callable[[list[str]], str],
) -> list[object]:
    items: list[object] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(_MAX_PAGES):
        page_params = {**params}
        if token is not None:
            page_params["pageToken"] = token
        stdout = gws_runner([*resource, "--params", json.dumps(page_params, separators=(",", ":"), sort_keys=True)])
        page_items, next_token = _page(stdout)
        items.extend(page_items)
        if next_token is None:
            return items
        if next_token in seen_tokens:
            raise WebDataError("gws pagination repeated a page token")
        seen_tokens.add(next_token)
        token = next_token
    raise WebDataError(f"gws pagination exceeded {_MAX_PAGES} pages")


def _task_from(raw: object) -> GoogleTask:
    if not isinstance(raw, dict):
        raise WebDataError("gws task item is not an object")
    task_id = raw.get("id")
    title = raw.get("title")
    if not isinstance(task_id, str) or not task_id:
        raise WebDataError("gws task item has an empty id")
    if not isinstance(title, str) or not title:
        raise WebDataError(f"gws task {task_id!r} has an empty title")

    notes_value = raw.get("notes", "")
    notes = notes_value if isinstance(notes_value, str) else ""
    lines = notes.splitlines()
    section: Literal["today", "active", "other"] = "other"
    if lines and lines[0] in {"section:today", "section:active"}:
        section = cast(Literal["today", "active"], lines[0].removeprefix("section:"))
    account = None
    if len(lines) > 1 and lines[1].startswith("account:"):
        parsed_account = lines[1].removeprefix("account:")
        account = parsed_account or None

    def optional_string(name: str) -> str | None:
        value = raw.get(name)
        return value if isinstance(value, str) else None

    status_value = raw.get("status", "")
    return GoogleTask(
        id=task_id,
        title=title,
        notes=notes,
        status=status_value if isinstance(status_value, str) else "",
        due=optional_string("due"),
        completed=optional_string("completed"),
        section=section,
        account=account,
    )


def resolve_task_list_id(*, gws_runner: Callable[[list[str]], str] = run_gws) -> str:
    """Return the id of the exact-title Google Tasks list named ``fieldkit``."""
    task_lists = _pages(["tasks", "tasklists", "list"], {}, gws_runner=gws_runner)
    for raw in task_lists:
        if not isinstance(raw, dict):
            raise WebDataError("gws task-list item is not an object")
        if raw.get("title") == _TASK_LIST_TITLE:
            candidate = raw.get("id")
            if not isinstance(candidate, str) or not candidate:
                raise WebDataError("Google Tasks list 'fieldkit' has an empty id")
            return candidate
    raise WebDataError("Google Tasks list 'fieldkit' not found")


def list_tasks(*, gws_runner: Callable[[list[str]], str] = run_gws) -> list[GoogleTask]:
    """Return every task from the Google Tasks list named ``fieldkit``."""
    task_list_id = resolve_task_list_id(gws_runner=gws_runner)

    raw_tasks = _pages(
        ["tasks", "tasks", "list"],
        {"showCompleted": True, "showHidden": True, "tasklist": task_list_id},
        gws_runner=gws_runner,
    )
    tasks: list[GoogleTask] = []
    seen_ids: set[str] = set()
    for raw in raw_tasks:
        task = _task_from(raw)
        if task.id not in seen_ids:
            seen_ids.add(task.id)
            tasks.append(task)
    return tasks


def _validate_id(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise GTaskValidationError(f"{label} must contain only letters, numbers, underscores, and hyphens")


def _normalize_due(due: str | None) -> str | None:
    if due is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) is None:
        raise GTaskValidationError("due must be a valid date in YYYY-MM-DD format")
    try:
        parsed = dt.date.fromisoformat(due)
    except ValueError as exc:
        raise GTaskValidationError("due must be a valid date in YYYY-MM-DD format") from exc
    return f"{parsed.isoformat()}T00:00:00.000Z"


def _mutation_response(stdout: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise WebDataError("gws mutation produced non-JSON output") from exc
    if not isinstance(payload, dict) or not payload:
        raise WebDataError("gws mutation produced an empty or non-object JSON payload")
    task_id = payload.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise WebDataError("gws mutation response has an empty task id")
    return cast(dict[str, object], payload)


def create_task(
    title: str,
    *,
    section: Literal["today", "active"],
    account: str | None = None,
    due: str | None = None,
    confirm: bool = False,
    gws_runner: Callable[[list[str]], str] = run_gws,
) -> TaskMutation:
    """Preview or create one task in the Google Tasks ``fieldkit`` list."""
    if not title.strip() or len(title) > 1024:
        raise GTaskValidationError("title must contain 1 to 1024 characters")
    if section not in {"today", "active"}:
        raise GTaskValidationError("section must be today or active")
    if account is not None:
        _validate_id(account, "account")
    normalized_due = _normalize_due(due)
    task_list_id = resolve_task_list_id(gws_runner=gws_runner)
    if not confirm:
        return TaskMutation("create", task_list_id, None, title, section, account, normalized_due, False)

    notes = f"section:{section}" + (f"\naccount:{account}" if account is not None else "")
    body: dict[str, str] = {"notes": notes, "title": title}
    if normalized_due is not None:
        body["due"] = normalized_due
    stdout = gws_runner(
        [
            "tasks",
            "tasks",
            "insert",
            "--params",
            json.dumps({"tasklist": task_list_id}, separators=(",", ":"), sort_keys=True),
            "--json",
            json.dumps(body, separators=(",", ":"), sort_keys=True),
        ]
    )
    payload = _mutation_response(stdout)
    return TaskMutation("create", task_list_id, cast(str, payload["id"]), title, section, account, normalized_due, True)


def complete_task(
    task_id: str,
    *,
    confirm: bool = False,
    gws_runner: Callable[[list[str]], str] = run_gws,
) -> TaskMutation:
    """Preview or complete one task in the Google Tasks ``fieldkit`` list."""
    _validate_id(task_id, "task id")
    task_list_id = resolve_task_list_id(gws_runner=gws_runner)
    if not confirm:
        return TaskMutation("complete", task_list_id, task_id, None, None, None, None, False)

    stdout = gws_runner(
        [
            "tasks",
            "tasks",
            "patch",
            "--params",
            json.dumps({"task": task_id, "tasklist": task_list_id}, separators=(",", ":"), sort_keys=True),
            "--json",
            json.dumps({"status": "completed"}, separators=(",", ":"), sort_keys=True),
        ]
    )
    _mutation_response(stdout)
    return TaskMutation("complete", task_list_id, task_id, None, None, None, None, True)
