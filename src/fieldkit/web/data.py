"""fieldkit.web.data — Data providers for the web dashboard.

Every provider is a small, injectable function over either the local
filesystem (briefs, watcher alerts) or the fieldkit CLI's ``--json``
output (pipeline health, forecast, quota). The CLI-as-contract choice is
deliberate: importing ``fieldkit.commands.*`` from a domain module would
invert the tach import direction, while the ``--json`` flags are already
the machine interface every other agent consumer uses.
"""

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fieldkit.companion.feed import WATCHER_FRESHNESS_WINDOW
from fieldkit.companion.outbox import list_proposals as list_outbox_proposals
from fieldkit.companion.outbox import load_proposal as load_outbox_proposal
from fieldkit.errors import WebDataError

# Exit codes that can carry valid JSON on stdout. Default pursuit health reports
# now exit 0; exit 1 remains accepted for explicit strict or partial-result callers.
_JSON_OK_EXIT_CODES = frozenset({0, 1})

_CLI_TIMEOUT_SECONDS = 120


def _fieldkit_argv() -> list[str]:
    """Return the argv prefix used to invoke the fieldkit CLI.

    Prefers the installed ``fieldkit`` binary; falls back to
    ``python -m fieldkit`` so the web server works from a dev checkout
    where the tool has not been reinstalled yet.
    """
    binary = shutil.which("fieldkit")
    if binary is not None:
        return [binary]
    return [sys.executable, "-m", "fieldkit"]


def run_cli_json(args: list[str], *, ok_exit_codes: frozenset[int] = _JSON_OK_EXIT_CODES) -> Any:
    """Run ``fieldkit <args>`` and parse its stdout as JSON.

    Args:
        args: CLI arguments after the program name, e.g.
            ``["pursuit", "health", "--json"]``.

    Returns:
        The parsed JSON payload (list or dict).

    Raises:
        WebDataError: On timeout, unexpected exit code, or unparseable output.
    """
    argv = [*_fieldkit_argv(), *args]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,  # accepted nonzero codes may still carry valid JSON
        )
    except subprocess.TimeoutExpired as exc:
        raise WebDataError(f"fieldkit {' '.join(args)} timed out after {_CLI_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise WebDataError(f"could not invoke fieldkit CLI: {exc}") from exc

    if result.returncode not in ok_exit_codes:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no stderr"
        raise WebDataError(f"fieldkit {' '.join(args)} exited {result.returncode}: {detail}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        # Surface the diagnostic (e.g. "auth expired") instead of discarding it.
        stderr_tail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no stderr"
        raise WebDataError(f"fieldkit {' '.join(args)} produced non-JSON output ({stderr_tail})") from exc


def run_cli_json_lines(args: list[str]) -> list[dict[str, Any]]:
    """Run ``fieldkit <args>`` and require one JSON object per nonblank line."""
    argv = [*_fieldkit_argv(), *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=_CLI_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WebDataError(f"fieldkit {' '.join(args)} timed out after {_CLI_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise WebDataError(f"could not invoke fieldkit CLI: {exc}") from exc
    stderr_tail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no stderr"
    if result.returncode not in _JSON_OK_EXIT_CODES:
        raise WebDataError(f"fieldkit {' '.join(args)} exited {result.returncode}: {stderr_tail}")
    rows: list[dict[str, Any]] = []
    try:
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("JSON line is not an object")
            rows.append(row)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WebDataError(f"fieldkit {' '.join(args)} produced invalid JSON-lines output ({stderr_tail})") from exc
    return rows


def run_doctor_json() -> Any:
    """Return doctor JSON even when it reports an actionable auth failure."""
    return run_cli_json(["doctor", "--json"], ok_exit_codes=frozenset({0, 2}))


@dataclass(frozen=True)
class BriefDoc:
    """The most recent saved morning brief."""

    name: str
    markdown: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""
        return {"name": self.name, "markdown": self.markdown}


@dataclass(frozen=True)
class AlertFile:
    """One watcher alert file with its content."""

    name: str
    mtime: float
    markdown: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {"name": self.name, "mtime": self.mtime, "markdown": self.markdown}


@dataclass(frozen=True)
class ProposalDoc:
    """One companion proposal awaiting operator review."""

    name: str
    mtime: float
    markdown: str
    approvable: bool
    validation_error: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "name": self.name,
            "mtime": self.mtime,
            "markdown": self.markdown,
            "approvable": self.approvable,
            "validation_error": self.validation_error,
        }

    def summary_dict(self) -> dict[str, Any]:
        """Return metadata suitable for the operations dashboard list."""
        return {
            "name": self.name,
            "mtime": self.mtime,
            "approvable": self.approvable,
            "validation_error": self.validation_error,
        }


OperationStatus = Literal["ok", "stale", "missing", "error"]


@dataclass(frozen=True)
class OperationStage:
    """One observable stage in the fieldkit mother-hen chain."""

    name: str
    status: OperationStatus
    detail: str
    updated_at: str | None
    action: str

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable representation."""
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "updated_at": self.updated_at,
            "action": self.action,
        }


@dataclass
class DataSource:
    """Injectable data access for the web app.

    Defaults are wired from fieldkit config at app-creation time
    (see ``DataSource.from_config``); tests construct one directly
    with ``tmp_path``-backed directories and a stub ``cli_json``.
    """

    briefs_dir: Path
    watchers_dir: Path
    cli_json: Any = field(default=run_cli_json)
    cli_json_lines: Callable[[list[str]], list[dict[str, Any]]] = field(default=run_cli_json_lines)
    data_dir: Path | None = None
    doctor_json: Callable[[], Any] = field(default=run_doctor_json)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(tz=UTC))

    @classmethod
    def from_config(cls) -> "DataSource":
        """Build a DataSource from the operator's fieldkit configuration."""
        from fieldkit.config import get_fieldkit_data, get_fieldkit_home, get_watchers_dir

        data_dir = Path(get_fieldkit_data())

        return cls(
            briefs_dir=get_fieldkit_home() / "briefs",
            watchers_dir=get_watchers_dir(),
            data_dir=data_dir,
        )

    def latest_brief(self) -> BriefDoc | None:
        """Return the most recent saved morning brief, or None when absent.

        ``fieldkit brief generate`` writes all morning briefs to
        ``<data>/briefs/`` (date-stamped filenames sort chronologically).
        """
        if not self.briefs_dir.is_dir():
            return None
        candidates = list(self.briefs_dir.glob("morning-brief-*.md"))
        if not candidates:
            return None
        path = max(candidates, key=lambda p: p.name)
        return BriefDoc(name=path.name, markdown=path.read_text(encoding="utf-8"))

    def list_alerts(self) -> list[AlertFile]:
        """Return all watcher alert files, most recently modified first.

        Files deleted between glob and read (watchers rewrite via
        delete+recreate) are skipped rather than failing the route.
        """
        if not self.watchers_dir.is_dir():
            return []
        alerts: list[AlertFile] = []
        for path in sorted(self.watchers_dir.glob("*-alerts.md")):
            try:
                alerts.append(
                    AlertFile(
                        name=path.name,
                        mtime=path.stat().st_mtime,
                        markdown=path.read_text(encoding="utf-8"),
                    )
                )
            except OSError:
                continue
        return sorted(alerts, key=lambda a: a.mtime, reverse=True)

    def alert_mtimes(self) -> dict[str, float]:
        """Return a name → mtime snapshot of alert files (for change detection).

        Files vanishing between glob and stat are skipped (TOCTOU-safe) so
        the SSE generator survives watcher rewrites.
        """
        if not self.watchers_dir.is_dir():
            return {}
        snapshot: dict[str, float] = {}
        for path in self.watchers_dir.glob("*-alerts.md"):
            try:
                snapshot[path.name] = path.stat().st_mtime
            except OSError:
                continue
        return snapshot

    def pipeline_health(self) -> Any:
        """Return the risk-ranked pursuit health list (CLI --json contract)."""
        return self.cli_json(["pursuit", "health", "--json"])

    def forecast(self) -> Any:
        """Return the weighted pipeline forecast (CLI --json contract)."""
        return self.cli_json(["pursuit", "forecast", "--json"])

    def quota(self) -> Any:
        """Return the quota gap summary (CLI --json contract)."""
        return self.cli_json(["pipeline", "quota", "--json"])

    def feed(self) -> list[dict[str, Any]]:
        """Return the non-consuming companion attention feed."""
        fields = (
            "item_id",
            "source",
            "account",
            "severity",
            "summary",
            "evidence_path",
            "suggested_skill",
            "observed_at",
        )
        return [
            {field_name: row.get(field_name) for field_name in fields}
            for row in self.cli_json_lines(["companion", "feed", "--json", "--all"])
        ]

    def list_proposals(self) -> list[ProposalDoc]:
        """Return pending proposals newest first without changing their state."""
        if self.data_dir is None:
            return []
        return [
            ProposalDoc(
                proposal.name, proposal.mtime, proposal.markdown, proposal.approvable, proposal.validation_error
            )
            for proposal in list_outbox_proposals(self.data_dir)
        ]

    def proposal(self, name: str) -> ProposalDoc | None:
        """Return one pending proposal, rejecting paths outside the outbox."""
        if self.data_dir is None:
            return None
        proposal = load_outbox_proposal(self.data_dir, name)
        if proposal is None:
            return None
        return ProposalDoc(
            proposal.name, proposal.mtime, proposal.markdown, proposal.approvable, proposal.validation_error
        )

    def _latest_brief_mtime(self) -> float | None:
        if not self.briefs_dir.is_dir():
            return None
        try:
            return max(path.stat().st_mtime for path in self.briefs_dir.glob("morning-brief-*.md"))
        except (OSError, ValueError):
            return None

    def _latest_companion_timestamp(self) -> str | None:
        """Return the newest valid companion journal timestamp, if any."""
        if self.data_dir is None or not self.data_dir.is_dir():
            return None
        timestamps = (
            timestamp
            for path in self.data_dir.glob("companion-journal-*.jsonl")
            for timestamp in self._journal_timestamps(path)
        )
        return max(timestamps, default=None)

    @staticmethod
    def _journal_timestamps(path: Path) -> list[str]:
        try:
            records = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
            return [
                record["timestamp"]
                for record in records
                if isinstance(record, dict) and isinstance(record.get("timestamp"), str)
            ]
        except (OSError, json.JSONDecodeError):
            return []

    def _is_stale(self, timestamp: str | None) -> bool:
        if timestamp is None:
            return True
        try:
            observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return True
        return self.now() - observed > WATCHER_FRESHNESS_WINDOW

    def _doctor_operation(self) -> OperationStage:
        try:
            services = self.doctor_json()
        except WebDataError as exc:
            return OperationStage("doctor", "error", str(exc), None, "Run fieldkit doctor and inspect its output.")
        if not isinstance(services, list):
            return OperationStage(
                "doctor",
                "error",
                f"Unexpected doctor payload: {type(services).__name__}.",
                None,
                "Run fieldkit doctor and inspect its output.",
            )
        unhealthy = [
            service for service in services if isinstance(service, dict) and service.get("healthy") is not True
        ]
        if not unhealthy:
            return OperationStage("doctor", "ok", "All configured services are healthy.", None, "")
        detail = "; ".join(
            f"{service.get('service', 'unknown')}: {service.get('message', 'unhealthy')}" for service in unhealthy
        )
        return OperationStage(
            "doctor", "error", detail, None, "Run fieldkit doctor to restore an unavailable credential."
        )

    def _watcher_operation(self) -> OperationStage:
        try:
            watcher_data = self.cli_json(["watch", "status", "--json"])
        except WebDataError as exc:
            return OperationStage("watchers", "error", str(exc), None, "Run fieldkit watch status.")
        watcher_items = watcher_data.get("items", []) if isinstance(watcher_data, dict) else []
        failures = [
            item for item in watcher_items if isinstance(item, dict) and item.get("outcome") not in {"ok", "partial"}
        ]
        timestamps = [item.get("last_run") for item in watcher_items if isinstance(item, dict)]
        updated_at = max((stamp for stamp in timestamps if isinstance(stamp, str)), default=None)
        if not watcher_items:
            return OperationStage(
                "watchers", "missing", "No watcher run has been recorded.", None, "Run fieldkit watch run --all."
            )
        if failures:
            return OperationStage(
                "watchers",
                "error",
                f"{len(failures)} watcher run(s) need investigation.",
                updated_at,
                "Inspect fieldkit watch logs, then run fieldkit watch run --all.",
            )
        if self._is_stale(updated_at):
            return OperationStage(
                "watchers", "stale", "Watcher data is older than 26 hours.", updated_at, "Run fieldkit watch run --all."
            )
        return OperationStage("watchers", "ok", "Watcher data is current.", updated_at, "")

    def _brief_operation(self) -> OperationStage:
        brief_mtime = self._latest_brief_mtime()
        if brief_mtime is None:
            return OperationStage(
                "brief", "missing", "No saved morning brief exists.", None, "Run fieldkit brief generate."
            )
        updated_at = datetime.fromtimestamp(brief_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        status: OperationStatus = "stale" if self._is_stale(updated_at) else "ok"
        action = "Run fieldkit brief generate." if status == "stale" else ""
        detail = "Brief is older than 26 hours." if status == "stale" else "Latest brief is current."
        return OperationStage("brief", status, detail, updated_at, action)

    def _companion_operation(self, proposals: list[ProposalDoc]) -> OperationStage:
        updated_at = self._latest_companion_timestamp()
        if updated_at is None:
            return OperationStage(
                "companion",
                "missing",
                "No companion pass has been journaled.",
                None,
                "Run fieldkit companion loop --once --tier propose.",
            )
        if self._is_stale(updated_at):
            return OperationStage(
                "companion",
                "stale",
                f"Companion pass is older than 26 hours; {len(proposals)} proposal(s) await review.",
                updated_at,
                "Review proposals, then run fieldkit companion loop --once --tier propose.",
            )
        return OperationStage(
            "companion",
            "ok",
            f"Companion is current; {len(proposals)} proposal(s) await review.",
            updated_at,
            "Review pending companion proposals.",
        )

    def _latest_admission_decision(self) -> dict[str, Any] | None:
        if self.data_dir is None:
            return None
        try:
            ledger = json.loads((self.data_dir / "driver" / "developer-admission.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        decisions = ledger.get("decisions", []) if isinstance(ledger, dict) else []
        if not isinstance(decisions, list):
            return None
        for decision in reversed(decisions):
            if isinstance(decision, dict):
                return decision
        return None

    def _developer_operation(self) -> OperationStage:
        admission = self._latest_admission_decision()
        if admission is not None and admission.get("allowed") is False:
            reason = str(admission.get("reason_code", "unknown"))
            detail = str(admission.get("detail", "Developer admission was denied."))
            updated_at = admission.get("ts") if isinstance(admission.get("ts"), str) else None
            if reason == "lease-held":
                return OperationStage("developer", "ok", f"Developer lease is in use: {detail}", updated_at, "")
            if reason == "daily-run-limit-reached":
                return OperationStage(
                    "developer",
                    "ok",
                    "Daily developer run limit reached; next eligibility is the next UTC day.",
                    updated_at,
                    "",
                )
            return OperationStage(
                "developer",
                "error",
                f"Developer admission denied: {reason} — {detail}",
                updated_at,
                "Review the developer admission policy before another dispatch.",
            )
        try:
            driver_data = self.cli_json(["driver", "status", "--json"])
        except WebDataError as exc:
            return OperationStage("developer", "error", str(exc), None, "Run fieldkit driver status.")
        driver_items = driver_data.get("items", []) if isinstance(driver_data, dict) else []
        latest_driver = driver_items[0] if driver_items and isinstance(driver_items[0], dict) else None
        if latest_driver is None:
            return OperationStage(
                "developer",
                "missing",
                "No developer run has been recorded.",
                None,
                "Review the admission policy before enabling a workflow.",
            )
        outcome = str(latest_driver.get("outcome", "unknown"))
        updated_at = latest_driver.get("ts") if isinstance(latest_driver.get("ts"), str) else None
        if outcome == "failed":
            return OperationStage(
                "developer",
                "error",
                str(latest_driver.get("error", "Driver failed.")),
                updated_at,
                "Review the recorded driver failure before another dispatch.",
            )
        if admission is not None and admission.get("allowed") is True:
            admission_reason = str(admission.get("reason_code", "admitted"))
            admission_updated_at = admission.get("ts") if isinstance(admission.get("ts"), str) else updated_at
            return OperationStage(
                "developer",
                "ok",
                f"Latest developer admission: {admission_reason}. Latest driver outcome: {outcome}.",
                admission_updated_at,
                "",
            )
        return OperationStage("developer", "ok", f"Latest driver outcome: {outcome}.", updated_at, "")

    def operations(self) -> dict[str, Any]:
        """Return the operator-facing state of the deterministic attention chain."""
        proposals = self.list_proposals()
        stages = [
            self._doctor_operation(),
            self._watcher_operation(),
            self._brief_operation(),
            self._companion_operation(proposals),
            self._developer_operation(),
        ]

        next_action = next(
            (stage.action for stage in stages if stage.status in {"error", "stale", "missing"}),
            "No intervention is required.",
        )
        return {
            "stages": [stage.to_dict() for stage in stages],
            "proposals": [proposal.summary_dict() for proposal in proposals],
            "next_action": next_action,
        }
