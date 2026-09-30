#!/usr/bin/env python3
"""Install one fieldkit artifact outside the checkout and exercise a declared profile contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from scripts import release_consumer

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = Path("pyproject.toml")
COMMAND_TIMEOUT_SECONDS = 60
VENV_CREATE_TIMEOUT_SECONDS = 180
INSTALL_TIMEOUT_SECONDS = 300
_MAX_DIAGNOSTIC_CHARS = 500
Installer = Literal["pip", "uv"]
_FIRST_USE_SERVICES = ("sf", "gmail", "google", "shadowbot")
_FIRST_USE_SKILLS = frozenset({"brief", "meeting", "pursuit-auditor", "task-management"})
_TRIAL_CREDENTIAL_ENV = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
)
_TRIAL_ROOT_PROGRAM = """import json
from fieldkit.config import CONFIG_PATH, get_fieldkit_data, get_fieldkit_home
print(json.dumps({
    'config': str(CONFIG_PATH),
    'data': str(get_fieldkit_data()),
    'workspace': str(get_fieldkit_home()),
}, sort_keys=True))
"""


@dataclass(frozen=True, order=True)
class SmokeCriterion:
    """Outcome of one installed-package smoke criterion."""

    criterion_id: str
    status: str
    diagnostic: str = ""


@dataclass(frozen=True)
class SmokeReport:
    """Versioned compatibility evidence for one exact artifact."""

    schema_version: int
    source_revision: str | None
    artifact_name: str
    artifact_sha256: str
    profile: str
    python: str
    platform: str
    criteria: tuple[SmokeCriterion, ...]

    @property
    def ok(self) -> bool:
        """Return whether every installed-package criterion passed."""
        return self.source_revision is not None and all(criterion.status == "pass" for criterion in self.criteria)

    def to_dict(self) -> dict[str, object]:
        """Render the stable machine-readable evidence shape."""
        report = {**asdict(self), "status": "pass" if self.ok else "fail"}
        if self.source_revision is None:
            report.update(status="diagnostic", source_binding="unattested-dirty")
        return report


def _run(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run a bounded command in the isolated smoke environment."""
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=timeout)


def _criterion(criterion_id: str, result: subprocess.CompletedProcess[str], expected_exit: int = 0) -> SmokeCriterion:
    """Translate a process result into bounded criterion evidence."""
    if result.returncode == expected_exit:
        return SmokeCriterion(criterion_id, "pass")
    output = (result.stderr or result.stdout).strip().replace("\n", " ")
    prefix = f"exit {result.returncode}, expected {expected_exit}: "
    available = _MAX_DIAGNOSTIC_CHARS - len(prefix)
    diagnostic = prefix + (output if len(output) <= available else f"...{output[-(available - 3) :]}")
    return SmokeCriterion(criterion_id, "fail", diagnostic)


def _first_use_doctor_criterion(result: subprocess.CompletedProcess[str]) -> SmokeCriterion:
    """Require a clean disabled-service diagnosis for a credential-free trial."""
    criterion = _criterion("SMOKE151", result)
    if criterion.status == "fail":
        return criterion
    expected_prefixes = {f"{service}: optional — not configured (" for service in _FIRST_USE_SERVICES}
    lines = result.stdout.splitlines()
    actual_prefixes = {
        prefix for prefix in expected_prefixes if any(line.startswith(prefix) and line.endswith(")") for line in lines)
    }
    if result.stderr or actual_prefixes != expected_prefixes or len(lines) != len(_FIRST_USE_SERVICES):
        return SmokeCriterion(
            "SMOKE151",
            "fail",
            "doctor did not report exactly the four credential-free services as not configured on stdout",
        )
    return criterion


def _first_use_skill_list_criterion(result: subprocess.CompletedProcess[str]) -> SmokeCriterion:
    """Require clean human-readable discovery of representative packaged skills."""
    criterion = _criterion("SMOKE152", result)
    if criterion.status == "fail":
        return criterion
    listed_names = {
        line.split(maxsplit=1)[0] for line in result.stdout.splitlines() if line and not line.startswith("-")
    }
    has_summary = re.search(r"^\d+ skills available \(\d+ with evals\)\.", result.stdout, re.MULTILINE) is not None
    if result.stderr or not listed_names >= _FIRST_USE_SKILLS or not has_summary:
        return SmokeCriterion(
            "SMOKE152",
            "fail",
            "skill list did not cleanly report the representative packaged workflows",
        )
    return criterion


def _revision(repo_root: Path) -> str:
    """Read the exact Git revision represented by the smoke."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=False, timeout=30
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise OSError("git rev-parse HEAD failed; pass --source-revision")
    return result.stdout.strip()


def _prerequisite_versions(*, cwd: Path, env: dict[str, str]) -> tuple[SmokeCriterion, ...]:
    """Execute the documented prerequisite checks without a shell or user configuration."""
    checks = (
        ("SMOKE125", "python3", r"Python ([0-9]+\.[0-9]+\.[0-9]+)"),
        ("SMOKE126", "uv", r"uv ([0-9]+\.[0-9]+\.[0-9]+)(?: \([^\r\n]*\))?"),
    )
    criteria: list[SmokeCriterion] = []
    for identifier, command, pattern in checks:
        result = _run([command, "--version"], cwd=cwd, env=env)
        criterion = _criterion(identifier, result)
        if criterion.status == "pass":
            match = re.fullmatch(pattern, result.stdout.strip())
            criterion = (
                SmokeCriterion(identifier, "pass", f"{command} {match.group(1)}")
                if match is not None
                else SmokeCriterion(identifier, "fail", f"{command} did not emit its version")
            )
        criteria.append(criterion)
    return tuple(criteria)


def _state_manifest(root: Path) -> dict[str, tuple[int, str]]:
    """Snapshot entry types, modes, link targets, and regular-file bytes without following links."""
    entries: dict[str, tuple[int, str]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        mode = path.lstat().st_mode
        value = (
            str(path.readlink())
            if stat.S_ISLNK(mode)
            else hashlib.sha256(path.read_bytes()).hexdigest()
            if stat.S_ISREG(mode)
            else ""
        )
        entries[path.relative_to(root).as_posix()] = mode, value
    return entries


def _state_boundaries(root: Path, cwd: Path, env: dict[str, str]) -> dict[Path, dict[str, tuple[int, str]] | None]:
    """Snapshot scoped state, unrelated cwd, and isolated configured roots, including absence."""
    roots = (
        root,
        cwd,
        *(
            Path(env[name])
            for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "FIELDKIT_DATA_DIR")
            if env.get(name)
        ),
    )
    return {path: _state_manifest(path) if path.exists() or path.is_symlink() else None for path in roots}


def _empty_pursuit_health(fieldkit: Path, *, state_root: Path, cwd: Path, env: dict[str, str]) -> SmokeCriterion:
    """Require the documented empty-workspace health result without workspace writes."""
    before = _state_manifest(state_root)
    result = _run([str(fieldkit), "pursuit", "health"], cwd=cwd, env=env)
    criterion = _criterion("SMOKE150", result, expected_exit=3)
    after = _state_manifest(state_root)
    if result.returncode == 3 and (result.stdout != "No pursuit files found.\n" or result.stderr or before != after):
        criterion = SmokeCriterion("SMOKE150", "fail", "empty pursuit health output incorrect or workspace changed")
    return criterion


def _getting_started_trial(
    fieldkit: Path,
    *,
    python: Path,
    root: Path,
    cwd: Path,
    env: dict[str, str],
) -> tuple[SmokeCriterion, SmokeCriterion, SmokeCriterion]:
    """Exercise the published isolated trial in order and prove its resolved roots."""
    trial_root = root / "getting-started-trial"
    trial_root.mkdir()
    workspace = trial_root / "workspace"
    config_home = trial_root / "config"
    runtime_data = trial_root / "runtime-data"
    normal_home = Path(env["HOME"])
    normal_home.mkdir(parents=True, exist_ok=True)
    normal_home_before = _state_manifest(normal_home)
    cwd_before = _state_manifest(cwd)
    trial_env = {
        **env,
        "XDG_CONFIG_HOME": str(config_home),
        "FIELDKIT_DATA_DIR": str(runtime_data),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    for variable in _TRIAL_CREDENTIAL_ENV:
        trial_env.pop(variable, None)
    trial_env.pop("FIELDKIT_SKILLS_DIR", None)

    initialize = _run([str(fieldkit), "init", "--minimal", str(workspace)], cwd=cwd, env=trial_env)
    initialization_criterion = _criterion("SMOKE155", initialize)
    if initialize.returncode != 0:
        prerequisite = "isolated trial initialization failed"
        return (
            initialization_criterion,
            SmokeCriterion("SMOKE151", "fail", prerequisite),
            SmokeCriterion("SMOKE152", "fail", prerequisite),
        )

    doctor = _run([str(fieldkit), "doctor"], cwd=cwd, env=trial_env)
    skills = _run([str(fieldkit), "skill", "list"], cwd=cwd, env=trial_env)
    resolved = _run([str(python), "-c", _TRIAL_ROOT_PROGRAM], cwd=cwd, env=trial_env)
    expected_roots = json.dumps(
        {
            "config": str(config_home / "fieldkit" / "config.yaml"),
            "data": str(runtime_data),
            "workspace": str(workspace),
        },
        sort_keys=True,
    )
    trial_entries = _state_manifest(trial_root)
    allowed_top_levels = {"config", "runtime-data", "workspace"}
    unexpected_trial_entry = any(
        relative != "." and relative.split("/", maxsplit=1)[0] not in allowed_top_levels for relative in trial_entries
    )
    config_path = config_home / "fieldkit" / "config.yaml"
    if (
        initialize.stderr
        or not workspace.is_dir()
        or not config_path.is_file()
        or resolved.returncode != 0
        or resolved.stderr
        or resolved.stdout.strip() != expected_roots
        or _state_manifest(normal_home) != normal_home_before
        or _state_manifest(cwd) != cwd_before
        or unexpected_trial_entry
    ):
        initialization_criterion = SmokeCriterion(
            "SMOKE155",
            "fail",
            "isolated trial did not resolve and contain configuration, workspace, and runtime roots exactly",
        )
    return initialization_criterion, _first_use_doctor_criterion(doctor), _first_use_skill_list_criterion(skills)


def _published_first_use_sequences(
    fieldkit: Path, *, root: Path, env: dict[str, str]
) -> tuple[SmokeCriterion, SmokeCriterion, SmokeCriterion]:
    """Execute the README/init and user-guide command blocks as isolated ordered recipes."""
    python = fieldkit.with_name("python.exe" if fieldkit.suffix == ".exe" else "python")
    normal_home = Path(env["HOME"])
    normal_home.mkdir(parents=True, exist_ok=True)
    normal_home_before = _state_manifest(normal_home)

    def scenario_environment(name: str) -> tuple[Path, Path, Path, dict[str, str]]:
        scenario_root = root / name
        cwd = scenario_root / "run"
        cwd.mkdir(parents=True)
        workspace = cwd / "fieldkit-workspace"
        scenario_env = {
            **env,
            "XDG_CONFIG_HOME": str(scenario_root / "config"),
            "FIELDKIT_DATA_DIR": str(scenario_root / "runtime-data"),
            "PYTHON_DOTENV_DISABLED": "1",
        }
        for variable in _TRIAL_CREDENTIAL_ENV:
            scenario_env.pop(variable, None)
        return scenario_root, cwd, workspace, scenario_env

    readme_root, readme_cwd, readme_workspace, readme_env = scenario_environment("readme-init")
    readme_init = _run([str(fieldkit), "init", "--minimal", "./fieldkit-workspace"], cwd=readme_cwd, env=readme_env)
    readme_criterion = _criterion("SMOKE158", readme_init)
    if readme_init.returncode == 0:
        readme_before = _state_manifest(readme_root)
        readme_doctor = _run([str(fieldkit), "doctor"], cwd=readme_cwd, env=readme_env)
        readme_skills = _run([str(fieldkit), "skill", "list"], cwd=readme_cwd, env=readme_env)
        readme_resolved = _run([str(python), "-c", _TRIAL_ROOT_PROGRAM], cwd=readme_cwd, env=readme_env)
        readme_expected_roots = json.dumps(
            {
                "config": str(readme_root / "config" / "fieldkit" / "config.yaml"),
                "data": str(readme_root / "runtime-data"),
                "workspace": str(readme_workspace),
            },
            sort_keys=True,
        )
        if (
            readme_init.stderr
            or not readme_workspace.is_dir()
            or not (readme_root / "config" / "fieldkit" / "config.yaml").is_file()
            or _first_use_doctor_criterion(readme_doctor).status != "pass"
            or _first_use_skill_list_criterion(readme_skills).status != "pass"
            or readme_resolved.returncode != 0
            or readme_resolved.stderr
            or readme_resolved.stdout.strip() != readme_expected_roots
            or _state_manifest(readme_root) != readme_before
            or _state_manifest(normal_home) != normal_home_before
        ):
            readme_criterion = SmokeCriterion("SMOKE158", "fail", "README/init ordered first-use recipe did not match")

    normal_home_before_user = _state_manifest(normal_home)
    user_root, user_cwd, user_workspace, user_env = scenario_environment("user-guide")
    user_init = _run([str(fieldkit), "init", "--minimal", "./fieldkit-workspace"], cwd=user_cwd, env=user_env)
    user_init_criterion = _criterion("SMOKE159", user_init)
    if user_init.returncode != 0:
        return (
            readme_criterion,
            user_init_criterion,
            SmokeCriterion("SMOKE160", "fail", "SMOKE159 user-guide setup failed"),
        )
    user_block_one_before = _state_manifest(user_root)
    user_help = _run([str(fieldkit), "--help"], cwd=user_cwd, env=user_env)
    user_skills = _run([str(fieldkit), "skill", "list"], cwd=user_cwd, env=user_env)
    user_resolved = _run([str(python), "-c", _TRIAL_ROOT_PROGRAM], cwd=user_cwd, env=user_env)
    user_expected_roots = json.dumps(
        {
            "config": str(user_root / "config" / "fieldkit" / "config.yaml"),
            "data": str(user_root / "runtime-data"),
            "workspace": str(user_workspace),
        },
        sort_keys=True,
    )
    if (
        user_init.stderr
        or not user_workspace.is_dir()
        or not (user_root / "config" / "fieldkit" / "config.yaml").is_file()
        or user_help.returncode != 0
        or user_help.stderr
        or "Usage: fieldkit" not in user_help.stdout
        or _first_use_skill_list_criterion(user_skills).status != "pass"
        or user_resolved.returncode != 0
        or user_resolved.stderr
        or user_resolved.stdout.strip() != user_expected_roots
        or _state_manifest(user_root) != user_block_one_before
        or _state_manifest(normal_home) != normal_home_before_user
    ):
        user_init_criterion = SmokeCriterion("SMOKE159", "fail", "user-guide setup recipe did not match")

    block_three_before = _state_manifest(user_root)
    normal_home_before_block_three = _state_manifest(normal_home)
    user_doctor = _run([str(fieldkit), "doctor"], cwd=user_cwd, env=user_env)
    user_health = _run([str(fieldkit), "pursuit", "health"], cwd=user_cwd, env=user_env)
    user_brief = _run(
        [str(fieldkit), "brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"],
        cwd=user_cwd,
        env=user_env,
    )
    block_three_criterion = SmokeCriterion("SMOKE160", "pass")
    if (
        user_init_criterion.status != "pass"
        or _first_use_doctor_criterion(user_doctor).status != "pass"
        or user_health.returncode != 3
        or user_health.stdout != "No pursuit files found.\n"
        or user_health.stderr
        or user_brief.returncode != 0
        or not user_brief.stdout.startswith("## ☀️ Morning Brief — ")
        or "## LLM Sections (omitted — run without --no-llm to generate)" not in user_brief.stdout
        or re.fullmatch(r"\[morning_brief\] Collecting data for \d{4}-\d{2}-\d{2} \.\.\.\n", user_brief.stderr) is None
        or _state_manifest(user_root) != block_three_before
        or _state_manifest(normal_home) != normal_home_before_block_three
    ):
        block_three_criterion = SmokeCriterion("SMOKE160", "fail", "user-guide diagnosis recipe did not match")
    return readme_criterion, user_init_criterion, block_three_criterion


def _pipeline_examples(
    fieldkit: Path, *, workspace: Path, cwd: Path, env: dict[str, str]
) -> tuple[SmokeCriterion, ...]:
    """Run the published local quota and forecast commands in the offline workspace."""
    setup = _run([str(fieldkit), "pipeline", "quota", "--set", "5000000", "--period", "2026-H2"], cwd=cwd, env=env)
    quota = _run([str(fieldkit), "pipeline", "quota"], cwd=cwd, env=env)
    quota_criterion = _criterion("SMOKE128", quota)
    expected_quota = (
        "  Weighted pipeline                     : $0\n"
        "  Closed-won (configured pursuits only) : $0\n"
        "  Quota target                          : $5,000,000\n\n"
        "  Attainment gap: n/a — pursuit-scope closed-won is not comparable\n"
        "  to a full-book quota. Pass --source sf to pull live\n"
        "  territory-scoped attainment from Salesforce."
    )
    if quota.returncode == 0 and quota.stdout.partition("\n")[2].rstrip("\n") != expected_quota:
        quota_criterion = SmokeCriterion("SMOKE128", "fail", "local quota did not emit the empty-workspace report")

    pursuits = workspace / "accounts" / "example" / "pursuits"
    pursuits.mkdir(parents=True, exist_ok=True)
    for name, stage, amount in (
        ("acme-corp-expansion", "propose", 450000),
        ("midwestins-expand", "propose", 120000),
        ("globalpay-renewal", "negotiate", 800000),
    ):
        frontmatter = json.dumps({"stage": stage, "sf_contract_type": "standard", "sf_consulting_acv": amount})
        (pursuits / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")
    forecast = _run([str(fieldkit), "pursuit", "forecast"], cwd=cwd, env=env)
    forecast_criterion = _criterion("SMOKE129", forecast)
    totals = re.findall(r"^  (Closed Won|Commit|Weighted|Best Case)\s+:\s+\$([0-9,]+)", forecast.stdout, re.MULTILINE)
    if forecast.returncode == 0 and sorted(totals) != [
        ("Best Case", "1,370,000"),
        ("Closed Won", "0"),
        ("Commit", "800,000"),
        ("Weighted", "885,000"),
    ]:
        forecast_criterion = SmokeCriterion("SMOKE129", "fail", "forecast did not emit the expected scenario totals")
    return _criterion("SMOKE127", setup), quota_criterion, forecast_criterion


def _pursuit_examples(fieldkit: Path, *, workspace: Path, cwd: Path, env: dict[str, str]) -> tuple[SmokeCriterion, ...]:
    """Require a real audit report and a non-writing pending stage preview."""
    initialize = _run([str(fieldkit), "init", "--minimal", str(workspace)], cwd=cwd, env=env)
    pursuit = workspace / "accounts" / "acme-corp" / "pursuits" / "acme-corp-q3.md"
    pursuit.parent.mkdir(parents=True, exist_ok=True)
    pursuit.write_text(
        "---\nstage: discover\ngate-status: pending\nlast-transition: null\ntransition-history: []\n---\n",
        encoding="utf-8",
    )
    pursuit_before = pursuit.read_bytes()
    audit = _run([str(fieldkit), "pursuit", "audit"], cwd=cwd, env=env)
    audit_criterion = _criterion("SMOKE131", audit)
    reports = list((workspace / "accounts" / ".audit").glob("pursuit-compliance-*.md"))
    if audit.returncode == 0 and (
        len(reports) != 1
        or "current qualification: unavailable" not in reports[0].read_text(encoding="utf-8")
        or pursuit.read_bytes() != pursuit_before
    ):
        audit_criterion = SmokeCriterion("SMOKE131", "fail", "audit report missing, incorrect, or pursuit changed")

    before = {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    preview = _run([str(fieldkit), "pursuit", "advance", str(pursuit), "--dry-run"], cwd=cwd, env=env)
    preview_criterion = _criterion("SMOKE132", preview, expected_exit=1)
    after = {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    expected_preview = "[dry-run] Gate pending — would NOT advance (use --override REASON to force)"
    if preview.returncode == 1 and (expected_preview not in preview.stdout.splitlines() or before != after):
        preview_criterion = SmokeCriterion("SMOKE132", "fail", "pending preview missing or workspace files changed")
    return _criterion("SMOKE130", initialize), audit_criterion, preview_criterion


def _local_examples(fieldkit: Path, *, workspace: Path, cwd: Path, env: dict[str, str]) -> tuple[SmokeCriterion, ...]:
    """Exercise local watcher diagnostics and explicit per-process environment examples."""
    (workspace / "config" / "accounts.yaml").write_text(
        json.dumps({"accounts": {"acme-corp": {"domains": ["acme-corp.example.com"]}}}), encoding="utf-8"
    )
    pursuit = workspace / "accounts" / "acme-corp" / "pursuits" / "acme-corp-q3.md"
    pursuit.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = json.dumps({"stage": "discover", "last-transition": datetime.now(tz=UTC).date().isoformat()})
    pursuit.write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")
    pursuit_before = pursuit.read_bytes()
    checks = (
        (
            "SMOKE133",
            ["watch", "run", "pursuit-stalls", "--dry-run"],
            "1 pursuit(s) scanned, 0 stall alert(s) would fire",
        ),
        ("SMOKE134", ["watch", "status"], "No watcher runs recorded yet."),
        ("SMOKE135", ["watch", "status", "--json"], ""),
        ("SMOKE136", ["watch", "logs", "--list"], "Recent log files (1):"),
        ("SMOKE137", ["watch", "logs", "pursuit-stalls", "--tail", "100"], "Run complete: checked=1 stalled=0"),
        (
            "SMOKE138",
            ["brief", "generate", "--pipeline-only", "--dry-run"],
            "## LLM Sections (omitted — run without --no-llm to generate)",
        ),
        ("SMOKE139", ["doctor"], ""),
    )
    criteria: list[SmokeCriterion] = []
    logs_preparation: subprocess.CompletedProcess[str] | None = None
    preparation_changed_inputs = False
    for identifier, arguments, expected in checks:
        command_env = {**env, "FIELDKIT_NO_LLM": "1"}
        if identifier == "SMOKE139":
            command_env["FIELDKIT_USER_EMAIL"] = "user@example.com"
        if identifier == "SMOKE136":
            inputs_before = _state_manifest(workspace)
            other_roots_before = _state_boundaries(cwd, cwd, env)
            logs_preparation = _run(
                [str(fieldkit), "watch", "run", "pursuit-stalls", "--force"], cwd=cwd, env=command_env
            )
            inputs_after = _state_manifest(workspace)
            preserved_inputs = {
                path: value
                for path, value in inputs_after.items()
                if path.split("/", maxsplit=1)[0] not in {"logs", "watchers"}
            }
            preparation_changed_inputs = (
                preserved_inputs != inputs_before or _state_boundaries(cwd, cwd, env) != other_roots_before
            )
        state_before = _state_boundaries(workspace, cwd, env) if identifier in {"SMOKE133", "SMOKE138"} else None
        result = _run([str(fieldkit), *arguments], cwd=cwd, env=command_env)
        criterion = _criterion(identifier, result)
        valid_output = bool(result.stdout.strip()) and expected in result.stdout
        if identifier == "SMOKE135":
            try:
                valid_output = json.loads(result.stdout) == {"items": [], "count": 0, "filters": {}}
            except json.JSONDecodeError:
                valid_output = False
        if identifier == "SMOKE133":
            valid_output = (
                valid_output
                and _state_boundaries(workspace, cwd, env) == state_before
                and pursuit.read_bytes() == pursuit_before
            )
        if identifier == "SMOKE138":
            valid_output = (
                valid_output
                and "## ☀️ Morning Brief —" in result.stdout
                and "### 🚦 Pursuit Alerts" in result.stdout
                and "### 📋 Today's Commitments" in result.stdout
                and "[LLM STUB]" not in result.stdout
                and "[DEGRADED]" not in result.stdout
                and _state_boundaries(workspace, cwd, env) == state_before
                and not (workspace / "briefs").exists()
            )
        if result.returncode == 0 and not valid_output:
            criterion = SmokeCriterion(identifier, "fail", "local example output or write boundary did not match")
        if identifier in {"SMOKE136", "SMOKE137"} and logs_preparation is not None:
            if logs_preparation.returncode != 0:
                criterion = _criterion(identifier, logs_preparation)
            elif preparation_changed_inputs:
                criterion = SmokeCriterion(identifier, "fail", "watcher log preparation changed unrelated inputs")
        criteria.append(criterion)
    python = fieldkit.with_name("python.exe" if fieldkit.suffix == ".exe" else "python")
    identity = _run(
        [
            str(python),
            "-c",
            "from fieldkit.config import get_user_email_from_env; assert get_user_email_from_env() == 'user@example.com'",
        ],
        cwd=cwd,
        env={**env, "FIELDKIT_USER_EMAIL": "user@example.com"},
    )
    criteria.append(_criterion("SMOKE140", identity))
    return tuple(criteria)


GMAIL_SETUP_PROGRAM = """import json
from datetime import UTC, datetime
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page, initialize_gmail_publication
path = get_gmail_db_path()
path.parent.mkdir(parents=True, exist_ok=True)
initialize_gmail_publication(path)
def seed(db):
    db.executemany('INSERT INTO labels(label_id,label_name) VALUES (?,?)', [('acme','ref/acme-corp'),('global','ref/global-pay')])
    for number, label in [(1,'acme'),(2,'acme'),(3,'global')]:
        thread = f'thread-{number}'
        db.execute('INSERT INTO threads(thread_id,subject) VALUES (?,?)', (thread,'Acme quarterly planning'))
        db.execute('INSERT INTO messages(message_id,thread_id,from_addr,to_addr,subject,date_epoch,labels) VALUES (?,?,?,?,?,?,?)',
                   (f'message-{number}',thread,'contact@example.com','user@example.com','Acme quarterly planning',int(datetime.now(UTC).timestamp()),json.dumps([label])))
    db.execute('INSERT OR REPLACE INTO sync_state(key,value) VALUES (?,?)', (GMAIL_QUERY_READY_KEY,'true'))
apply_gmail_page(path, seed)
print(path)
"""


def _gmail_examples(
    fieldkit: Path, *, python: Path, workspace: Path, cwd: Path, env: dict[str, str]
) -> tuple[SmokeCriterion, ...]:
    """Exercise offline Gmail commands against a fictional packaged-schema cache."""
    setup = _run([str(python), "-c", GMAIL_SETUP_PROGRAM], cwd=cwd, env=env)
    database = Path(setup.stdout.strip())
    setup_criterion = _criterion("SMOKE141", setup)
    valid_database = database == workspace / "data" / "gmail.db" and database.is_file()
    if setup.returncode == 0 and not valid_database:
        setup_criterion = SmokeCriterion("SMOKE141", "fail", "setup did not produce the configured Gmail cache")
    tags = _run([str(fieldkit), "gmail", "account-tags"], cwd=cwd, env=env)
    tags_criterion = _criterion("SMOKE142", tags)
    expected_output = (
        "Upserted 3 thread-account associations across 2 account(s):\n  acme-corp: 2 threads\n  global-pay: 1 threads\n"
    )
    rows: list[tuple[str, str]] = []
    if valid_database and database.resolve().is_relative_to(workspace.resolve()):
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT thread_id, account FROM thread_accounts ORDER BY thread_id").fetchall()
        finally:
            connection.close()
    if tags.returncode == 0 and (
        tags.stdout != expected_output
        or rows != [("thread-1", "acme-corp"), ("thread-2", "acme-corp"), ("thread-3", "global-pay")]
    ):
        tags_criterion = SmokeCriterion("SMOKE142", "fail", "tag summary or persisted associations did not match")
    doctor = _run([str(fieldkit), "doctor", "gmail"], cwd=cwd, env=env)
    doctor_criterion = _criterion("SMOKE143", doctor)
    if doctor.returncode == 0 and "gmail: OK — 3 messages," not in doctor.stdout:
        doctor_criterion = SmokeCriterion("SMOKE143", "fail", "doctor did not report the fictional cache")
    pursuits = {path: path.read_bytes() for path in workspace.glob("accounts/*/pursuits/*.md")}
    enrich = _run([str(fieldkit), "gmail", "enrich-pursuits"], cwd=cwd, env=env)
    enrich_criterion = _criterion("SMOKE144", enrich)
    reports = list(workspace.glob("accounts/*/gmail-intel.md"))
    expected_report = workspace / "accounts" / "acme-corp" / "gmail-intel.md"
    expected_content = (
        "# Gmail Intelligence Report: acme-corp",
        "## Top Contacts by Email Volume",
        "## Champion Signals (Top 10 Contacts)",
        "## Per-Pursuit Thread Matches",
        "## Review Checklist",
        "contact@example.com",
    )
    report_text = expected_report.read_text(encoding="utf-8") if expected_report.is_file() else ""
    top_contacts = report_text.partition("## Top Contacts by Email Volume")[2].partition("## Champion Signals")[0]
    pursuit_matches = report_text.partition("## Per-Pursuit Thread Matches")[2].partition("## Review Checklist")[0]
    if enrich.returncode == 0 and (
        reports != [expected_report]
        or not all(content in report_text for content in expected_content)
        or "| contact@example.com | 2 |" not in top_contacts
        or "### acme-corp-q3" not in pursuit_matches
        or "Acme quarterly planning" not in pursuit_matches
        or pursuits != {path: path.read_bytes() for path in workspace.glob("accounts/*/pursuits/*.md")}
    ):
        enrich_criterion = SmokeCriterion("SMOKE144", "fail", "Gmail report missing or pursuit changed")
    return setup_criterion, tags_criterion, doctor_criterion, enrich_criterion


GMAIL_MEETING_SETUP_PROGRAM = '''from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.publication import apply_gmail_page
def seed(connection):
        connection.execute(
            """
            INSERT OR REPLACE INTO people(
                email, display_name, first_seen, last_seen, message_count,
                thread_count, initiated_count, domain, account, is_internal, meeting_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "contact@acme-corp.example.com",
                "Casey Customer",
                "2026-09-01T12:00:00Z",
                "2026-09-27T12:00:00Z",
                3,
                2,
                1,
                "acme-corp.example.com",
                "acme-corp",
                0,
                1,
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO threads(thread_id, subject) VALUES (?, ?)",
            ("thread-meeting-fixture", 'Notes: "Acme review" September 27, 2026'),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO messages(
                message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                date_epoch, labels, body_plain, body_html
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "message-meeting-fixture",
                "thread-meeting-fixture",
                "gemini-notes@google.com",
                "contact@acme-corp.example.com",
                "",
                'Notes: "Acme review" September 27, 2026',
                1_790_467_200,
                "[]",
                "https://docs.google.com/document/d/doc-fixture-1",
                "",
            ),
        )
apply_gmail_page(get_gmail_db_path(), seed)
'''


def _meeting_ingest_examples(
    fieldkit: Path, *, python: Path, workspace: Path, state_root: Path, cwd: Path, env: dict[str, str]
) -> tuple[SmokeCriterion, SmokeCriterion]:
    """Execute the safe meeting and post-meeting examples against fictional local data."""
    setup = _run([str(python), "-c", GMAIL_MEETING_SETUP_PROGRAM], cwd=cwd, env=env)
    if setup.returncode != 0:
        return _criterion("SMOKE156", setup), _criterion("SMOKE157", setup)

    before_contact = _state_manifest(state_root)
    contact = _run(
        [str(fieldkit), "contact", "find", "contact@acme-corp.example.com", "--json"],
        cwd=cwd,
        env=env,
    )
    contact_criterion = _criterion("SMOKE156", contact)
    if contact.returncode == 0:
        try:
            profile = json.loads(contact.stdout)
        except json.JSONDecodeError:
            profile = None
        expected_profile = {
            "type": "resolved",
            "email": "contact@acme-corp.example.com",
            "display_name": "Casey Customer",
            "account": "acme-corp",
            "is_internal": 0,
        }
        if (
            contact.stderr
            or not isinstance(profile, dict)
            or any(profile.get(key) != value for key, value in expected_profile.items())
            or "slack_user_id" in profile
            or "slack_message_count" in profile
            or _state_manifest(state_root) != before_contact
        ):
            contact_criterion = SmokeCriterion(
                "SMOKE156", "fail", "meeting contact lookup did not return the exact fictional external profile"
            )

    before = _state_manifest(state_root)
    discover = _run(
        [
            str(fieldkit),
            "ingest",
            "discover",
            "--pipeline",
            "transcript-ingest",
            "--account",
            "acme-corp",
            "--dry-run",
        ],
        cwd=cwd,
        env=env,
    )
    discover_criterion = _criterion("SMOKE157", discover)
    expected_discovery = (
        "[dry-run] Pipeline 'transcript-ingest': 1 source(s) would be registered from gmail.db.\n"
        '  [dry-run] doc-fixture-1  (Notes: "Acme review" September 27, 2026)\n'
    )
    after = _state_manifest(state_root)
    if discover.returncode == 0 and (
        discover.stdout != expected_discovery
        or discover.stderr
        or after != before
        or (workspace / "data" / "pipeline.db").exists()
    ):
        mismatches: list[str] = []
        if discover.stdout != expected_discovery:
            mismatches.append(f"stdout={discover.stdout!r}")
        if discover.stderr:
            mismatches.append(f"stderr={discover.stderr!r}")
        if after != before:
            changed = sorted(
                set(before) ^ set(after) | {path for path in before.keys() & after if before[path] != after[path]}
            )
            mismatches.append(f"workspace changes={changed!r}")
        if (workspace / "data" / "pipeline.db").exists():
            mismatches.append("pipeline.db exists")
        detail = "; ".join(mismatches)
        if len(detail) > _MAX_DIAGNOSTIC_CHARS:
            detail = f"...{detail[-(_MAX_DIAGNOSTIC_CHARS - 3) :]}"
        discover_criterion = SmokeCriterion("SMOKE157", "fail", f"account-scoped ingest discovery mismatch: {detail}")
    return contact_criterion, discover_criterion


def _gmail_recovery_examples(
    fieldkit: Path, *, root: Path, cwd: Path, env: dict[str, str]
) -> tuple[SmokeCriterion, ...]:
    """Prove installed diagnostics preserve unusable caches and require data repair."""
    criteria: list[SmokeCriterion] = []
    unreadable = (
        "gmail: UNHEALTHY — The cache could not be verified without application writes — preserve it, stop cache users, "
        "and check its path and permissions; "
        "if damaged, stop cache users and move a recoverable backup aside before running 'fieldkit gmail sync'\n"
    )
    for identifier, state, expected_output in (
        ("SMOKE145", "missing", "gmail: optional — not configured (run 'fieldkit gmail sync')\n"),
        (
            "SMOKE146",
            "empty",
            "gmail: UNHEALTHY — The cache is empty — preserve a backup before rebuilding with 'fieldkit gmail sync'\n",
        ),
        (
            "SMOKE147",
            "corrupt",
            unreadable,
        ),
        ("SMOKE148", "missing-table", unreadable),
    ):
        directory = root / state
        directory.mkdir(parents=True)
        database = directory / "gmail.db"
        config_home = directory / "config"
        config = config_home / "fieldkit" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"gmail_db": str(database), "fieldkit_home": str(directory)}), encoding="utf-8")
        if state == "empty":
            database.touch()
        elif state == "corrupt":
            database.write_bytes(b"not a SQLite database")
        elif state == "missing-table":
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE messages(message_id TEXT)")
                connection.commit()
            finally:
                connection.close()
        before = _state_boundaries(root, cwd, env)
        result = _run(
            [str(fieldkit), "doctor", "gmail"],
            cwd=cwd,
            env={**env, "FIELDKIT_DATA_DIR": str(directory), "XDG_CONFIG_HOME": str(config_home)},
        )
        criterion = _criterion(identifier, result, expected_exit=3)
        after = _state_boundaries(root, cwd, env)
        if result.returncode == 3 and (
            before != after
            or result.stdout != expected_output
            or result.stderr
            or str(database) in result.stdout
            or any(table in result.stdout for table in ("people", "thread_accounts", "messages"))
            or any(word in result.stdout.lower() for word in ("remove", "delete"))
        ):
            criterion = SmokeCriterion(identifier, "fail", "diagnostic changed cache files or omitted repair guidance")
        criteria.append(criterion)
    return tuple(criteria)


def _expected_version(repo_root: Path) -> str:
    """Read the release version declared in project metadata."""
    pyproject = tomllib.loads((repo_root / PYPROJECT_PATH).read_text(encoding="utf-8"))
    project = pyproject.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str):
        raise ValueError("pyproject.toml project.version must be a string")
    return version


def _venv_commands(root: Path) -> tuple[Path, Path]:
    """Locate Python and fieldkit executables in a fresh virtual environment."""
    scripts = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
    executable_suffix = ".exe" if os.name == "nt" else ""
    return scripts / f"python{executable_suffix}", scripts / f"fieldkit{executable_suffix}"


_ANSWERS_EXAMPLE_PROGRAM = """import sys
from pathlib import Path
from fieldkit.util.strict_yaml import load_strict_yaml
from fieldkit.util.text_snapshot import read_text_snapshot
workspace = Path(sys.argv[1])
account = workspace / 'accounts' / 'acme-corp'
directories = (workspace.parent, workspace, workspace / 'config', workspace / 'accounts', account,
               *(account / name for name in ('pursuits', 'meetings', 'projects', 'proposals')))
if any(path.is_symlink() or not path.is_dir() for path in directories):
    raise SystemExit('unattended initialization directories must be real directories')
def read(path):
    return load_strict_yaml(read_text_snapshot(path, max_bytes=1024 * 1024).content)
expected = {
    'name': 'Example User', 'email': 'user@example.com', 'role': 'Account Executive',
    'company': 'Example Company', 'territory': 'East', 'salesforce_user_id': '',
    'accounts': ['Acme Corp'],
    'motions': ['Pre-sales pursuit', 'Landed account expansion', 'Relationship maintenance'],
}
identity = read(workspace / 'config' / 'identity.yaml')
accounts = read(workspace / 'config' / 'accounts.yaml')
stub = read_text_snapshot(account / 'account.md', max_bytes=1024 * 1024).content
frontmatter = stub.split('---', 2)
valid = (
    identity == {'identity': expected}
    and accounts == {'internal_domains': [], 'accounts': {'acme-corp': {
        'domains': [], 'team': [], 'keywords': ['Acme Corp'],
        'blindspots_min_messages': 20, 'blindspot_days': 14,
    }}}
    and len(frontmatter) == 3 and not frontmatter[0].strip()
    and load_strict_yaml(frontmatter[1]) == {'account': 'Acme Corp'}
    and '# Acme Corp' in frontmatter[2].splitlines()
)
if not valid:
    raise SystemExit('unattended initialization example artifacts mismatch')
"""


def _unattended_init(fieldkit: Path, *, python: Path, root: Path, cwd: Path, env: dict[str, str]) -> SmokeCriterion:
    workspace = root / "answers-workspace"
    answers_path = root / "init-answers.yaml"
    answers_path.write_text(
        "name: Example User\nemail: user@example.com\ndata_dir: " + str(workspace) + "\n"
        "role: Account Executive\ncompany: Example Company\nterritory: East\n"
        'salesforce_user_id: ""\naccounts:\n  - Acme Corp\n',
        encoding="utf-8",
    )
    result = _run([str(fieldkit), "init", "--answers", str(answers_path)], cwd=cwd, env=env)
    criterion = _criterion("SMOKE123", result)
    if result.returncode == 0 and not workspace.is_dir():
        return SmokeCriterion("SMOKE123", "fail", "answers initialization did not create its workspace")
    if result.returncode != 0:
        return criterion
    return _criterion(
        "SMOKE123",
        _run([str(python), "-c", _ANSWERS_EXAMPLE_PROGRAM, str(workspace)], cwd=cwd, env=env),
    )


def smoke(
    artifact: Path,
    *,
    repo_root: Path = REPO_ROOT,
    source_revision: str | None = None,
    diagnostic_dirty: bool = False,
    expected_version: str | None = None,
    profile: str = "base",
    installer: Installer = "pip",
    offline_dependencies: release_consumer.OfflineDependencies | None = None,
) -> SmokeReport:
    """Run a portable installation-profile contract against one exact artifact."""
    if diagnostic_dirty and source_revision is not None:
        raise ValueError("dirty diagnostic artifacts cannot claim a source revision")
    revision = None if diagnostic_dirty else source_revision or _revision(repo_root)
    version = expected_version or _expected_version(repo_root)
    criteria: list[SmokeCriterion] = []
    uv = shutil.which("uv") if installer == "uv" else None
    if installer == "uv" and uv is None:
        raise OSError("uv installer requested but uv is not on PATH")

    with tempfile.TemporaryDirectory(prefix="fieldkit-artifact-smoke-") as raw_root:
        root = Path(raw_root)
        closure = None
        if offline_dependencies is not None:
            from scripts import release_consumer

            closure = release_consumer.snapshot_offline_closure(artifact, offline_dependencies, root / "offline")
        resolved_artifact = closure.artifact if closure is not None else artifact.resolve()
        digest = hashlib.sha256(resolved_artifact.read_bytes()).hexdigest()
        run_dir = root / "run"
        run_dir.mkdir()
        home = root / "home"
        temporary_dir = root / "tmp"
        temporary_dir.mkdir()
        workspace = root / "workspace"
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "TMPDIR": str(temporary_dir),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "FIELDKIT_NO_LLM": "1",
        }
        if closure is not None:
            env.update(
                PIP_CONFIG_FILE=os.devnull,
                PIP_NO_CACHE_DIR="1",
                PIP_DISABLE_PIP_VERSION_CHECK="1",
                UV_OFFLINE="1",
                UV_NO_CACHE="1",
                UV_NO_CONFIG="1",
                UV_PYTHON_DOWNLOADS="never",
            )
        criteria.extend(_prerequisite_versions(cwd=run_dir, env=env))
        create_argv = (
            [uv, "venv", "--python", sys.executable, str(root / "venv")]
            if uv is not None
            else [sys.executable, "-m", "venv", str(root / "venv")]
        )
        create = _run(create_argv, cwd=run_dir, env=env, timeout=VENV_CREATE_TIMEOUT_SECONDS)
        criteria.append(_criterion("SMOKE001", create))
        python, fieldkit = _venv_commands(root)
        if create.returncode != 0:
            return SmokeReport(
                2,
                revision,
                resolved_artifact.name,
                digest,
                profile,
                platform.python_version(),
                platform.platform(),
                tuple(criteria),
            )

        install_target = str(resolved_artifact) if profile == "base" else f"{resolved_artifact}[{profile}]"
        if uv is not None:
            install_argv = [uv, "pip", "install", "--python", str(python), install_target]
        else:
            install_argv = [str(python), "-m", "pip", "install", "--disable-pip-version-check", install_target]

        def offline_install() -> subprocess.CompletedProcess[str]:
            from scripts import release_consumer

            assert closure is not None
            dependencies, installed = release_consumer.install_local_closure(
                closure,
                python,
                uv=uv,
                cwd=run_dir,
                env=env,
                runner=lambda argv, **kwargs: _run(argv, cwd=run_dir, env=env, timeout=INSTALL_TIMEOUT_SECONDS),
            )
            return dependencies if installed is None else installed

        install = (
            offline_install()
            if closure is not None
            else _run(install_argv, cwd=run_dir, env=env, timeout=INSTALL_TIMEOUT_SECONDS)
        )
        criteria.append(_criterion("SMOKE002", install))
        if install.returncode != 0:
            return SmokeReport(
                2,
                revision,
                resolved_artifact.name,
                digest,
                profile,
                platform.python_version(),
                platform.platform(),
                tuple(criteria),
            )

        version_result = _run([str(fieldkit), "--version"], cwd=run_dir, env=env)
        criteria.append(_criterion("SMOKE101", version_result))
        if version_result.returncode == 0 and version_result.stdout.strip() != f"fieldkit {version}":
            criteria[-1] = SmokeCriterion("SMOKE101", "fail", f"unexpected version: {version_result.stdout.strip()}")
        criteria.append(_criterion("SMOKE102", _run([str(fieldkit), "--help"], cwd=run_dir, env=env)))
        criteria.append(_criterion("SMOKE121", _run([str(fieldkit), "watch", "run", "--help"], cwd=run_dir, env=env)))
        criteria.append(
            _criterion(
                "SMOKE122", _run([str(fieldkit), "watch", "run", "pursuit-stalls", "--help"], cwd=run_dir, env=env)
            )
        )
        tool_uv = shutil.which("uv")
        if tool_uv is None:
            criteria.append(SmokeCriterion("SMOKE119", "fail", "uv is required for the documented tool installation"))
        else:
            tool_bin = root / "uv-tool-bin"
            tool_state = root / "tool-persistent"
            tool_workspace = tool_state / "tool-workspace"
            tool_env = {
                **env,
                "UV_TOOL_DIR": str(root / "uv-tools"),
                "UV_TOOL_BIN_DIR": str(tool_bin),
                "UV_NO_CACHE": "1",
                "XDG_CONFIG_HOME": str(tool_state / "config"),
                "XDG_DATA_HOME": str(tool_state / "data"),
                "FIELDKIT_DATA_DIR": str(tool_state / "runtime"),
            }
            tool_install_argv = [tool_uv, "tool", "install"]
            if closure is not None:
                tool_install_argv.extend(
                    [
                        "--python",
                        sys.executable,
                        "--no-python-downloads",
                        "--offline",
                        "--no-cache",
                        "--no-config",
                        "--no-index",
                        "--find-links",
                        str(closure.wheelhouse),
                        "--constraints",
                        str(closure.runtime_requirements),
                        "--build-constraints",
                        str(closure.release_build_requirements),
                    ]
                )
            tool_install_argv.append(str(closure.artifact if closure is not None else resolved_artifact))
            tool_install = _run(tool_install_argv, cwd=run_dir, env=tool_env, timeout=INSTALL_TIMEOUT_SECONDS)
            criteria.append(_criterion("SMOKE119", tool_install, expected_exit=0))
            if tool_install.returncode == 0:
                tool_version = _run([str(tool_bin / "fieldkit"), "--version"], cwd=run_dir, env=tool_env)
                criteria.append(_criterion("SMOKE120", tool_version))
                if tool_version.returncode == 0 and tool_version.stdout.strip() != f"fieldkit {version}":
                    criteria[-1] = SmokeCriterion(
                        "SMOKE120", "fail", f"unexpected uv tool version: {tool_version.stdout.strip()}"
                    )
                tool_initialize = _run(
                    [str(tool_bin / "fieldkit"), "init", "--minimal", str(tool_workspace)], cwd=run_dir, env=tool_env
                )
                setup_criterion = _criterion("SMOKE154", tool_initialize)
                if tool_initialize.returncode == 0 and (
                    not tool_workspace.is_dir() or not (tool_state / "config" / "fieldkit" / "config.yaml").is_file()
                ):
                    setup_criterion = SmokeCriterion("SMOKE154", "fail", "tool trial did not create persistent state")
                criteria.append(setup_criterion)
                (tool_state / "runtime").mkdir(parents=True, exist_ok=True)
                (tool_state / "runtime" / "retained.txt").write_text("retain runtime state\n", encoding="utf-8")
                persistent_before = _state_manifest(tool_state)
                tool_environment = root / "uv-tools" / "fieldkit-cli"
                environment_installed = tool_environment.is_dir() and not tool_environment.is_symlink()
                tool_uninstall = _run(
                    [tool_uv, "tool", "uninstall", "fieldkit-cli"],
                    cwd=run_dir,
                    env=tool_env,
                    timeout=INSTALL_TIMEOUT_SECONDS,
                )
                criteria.append(_criterion("SMOKE149", tool_uninstall))
                if setup_criterion.status == "fail":
                    criteria[-1] = SmokeCriterion("SMOKE149", "fail", "SMOKE154 uninstall prerequisite failed")
                elif tool_uninstall.returncode == 0 and (
                    not environment_installed
                    or (tool_bin / "fieldkit").exists()
                    or (tool_bin / "fieldkit").is_symlink()
                    or tool_environment.exists()
                    or tool_environment.is_symlink()
                    or _state_manifest(tool_state) != persistent_before
                ):
                    criteria[-1] = SmokeCriterion(
                        "SMOKE149", "fail", "uninstall retained application state or changed persistent user state"
                    )

        asset_program = """from importlib.resources import files
root = files('fieldkit')
required = ('_data/pursuit-frontmatter.schema.json', '_data/pursuit-narrative-template.md', '_data/driver-executor.md',
            'ingest/schema.sql', 'gmail/schema.sql', 'skills/README.md', 'web/static/index.html')
assert all(root.joinpath(path).is_file() for path in required)
"""
        criteria.append(_criterion("SMOKE103", _run([str(python), "-c", asset_program], cwd=run_dir, env=env)))
        criteria.append(
            _criterion("SMOKE104", _run([str(fieldkit), "init", "--minimal", str(workspace)], cwd=run_dir, env=env))
        )
        criteria.append(_unattended_init(fieldkit, python=python, root=root, cwd=run_dir, env=env))
        network_guard = root / "network-guard"
        network_guard.mkdir()
        network_attempt = root / "network-attempt"
        (network_guard / "sitecustomize.py").write_text(
            "import socket\n"
            "from pathlib import Path\n"
            "def _blocked(*_args, **_kwargs):\n"
            f"    Path({str(network_attempt)!r}).touch()\n"
            "    raise OSError('network access is forbidden in artifact smoke')\n"
            "socket.socket.connect = _blocked\n"
            "socket.socket.connect_ex = _blocked\n"
            "socket.socket.sendto = _blocked\n"
            "socket.getaddrinfo = _blocked\n",
            encoding="utf-8",
        )
        offline_env = {**env, "PYTHONPATH": str(network_guard)}
        criteria.extend(_published_first_use_sequences(fieldkit, root=root, env=offline_env))
        documented_workspace = run_dir / "fieldkit-workspace"
        documented_init = _run(
            [str(fieldkit), "init", "--minimal", "./fieldkit-workspace"], cwd=run_dir, env=offline_env
        )
        criteria.append(_criterion("SMOKE113", documented_init))
        if documented_init.returncode == 0 and not documented_workspace.is_dir():
            criteria[-1] = SmokeCriterion(
                "SMOKE113", "fail", "minimal initialization did not create the documented workspace"
            )
        criteria.extend(_getting_started_trial(fieldkit, python=python, root=root, cwd=run_dir, env=offline_env))
        criteria.append(_empty_pursuit_health(fieldkit, state_root=root, cwd=run_dir, env=offline_env))
        brief_dry_run = _run(
            [str(fieldkit), "brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"],
            cwd=run_dir,
            env=offline_env,
        )
        criteria.append(_criterion("SMOKE124", brief_dry_run))
        if brief_dry_run.returncode == 0 and (documented_workspace / "briefs").exists():
            criteria[-1] = SmokeCriterion("SMOKE124", "fail", "brief dry run created a brief directory")
        criteria.append(
            _criterion(
                "SMOKE115",
                _run(
                    [str(fieldkit), "init", "--minimal", str(root / "offline-workspace")], cwd=run_dir, env=offline_env
                ),
            )
        )
        criteria.extend(
            _pipeline_examples(fieldkit, workspace=root / "offline-workspace", cwd=run_dir, env=offline_env)
        )
        criteria.extend(_pursuit_examples(fieldkit, workspace=root / "pursuit-workspace", cwd=run_dir, env=offline_env))
        criteria.extend(_local_examples(fieldkit, workspace=root / "pursuit-workspace", cwd=run_dir, env=offline_env))
        criteria.extend(
            _gmail_examples(fieldkit, python=python, workspace=root / "pursuit-workspace", cwd=run_dir, env=offline_env)
        )
        criteria.extend(
            _meeting_ingest_examples(
                fieldkit,
                python=python,
                workspace=root / "pursuit-workspace",
                state_root=root,
                cwd=run_dir,
                env=offline_env,
            )
        )
        criteria.extend(_gmail_recovery_examples(fieldkit, root=root / "gmail-recovery", cwd=run_dir, env=offline_env))
        criteria.append(
            SmokeCriterion(
                "SMOKE153",
                "fail" if network_attempt.exists() else "pass",
                "offline scenario attempted network access" if network_attempt.exists() else "",
            )
        )
        criteria.append(_criterion("SMOKE105", _run([str(fieldkit), "doctor", "--json"], cwd=run_dir, env=env)))
        criteria.append(_criterion("SMOKE112", _run([str(fieldkit), "doctor"], cwd=run_dir, env=env)))
        criteria.append(_criterion("SMOKE106", _run([str(fieldkit), "skill", "list"], cwd=run_dir, env=env)))
        cursor_project = root / "cursor-project"
        (cursor_project / ".cursor").mkdir(parents=True)
        cursor_install = _run(
            [str(fieldkit), "skill", "install", "--tool", "cursor", "--skill", "brief"],
            cwd=cursor_project,
            env=env,
        )
        criteria.append(_criterion("SMOKE114", cursor_install))
        cursor_rule = cursor_project / ".cursor" / "rules" / "brief.md"
        if cursor_install.returncode == 0:
            if not cursor_rule.is_file():
                criteria[-1] = SmokeCriterion("SMOKE114", "fail", "Cursor install did not create its flat rule")
            elif "Bundled support: `ops/week-start.md`" not in cursor_rule.read_text(encoding="utf-8"):
                criteria[-1] = SmokeCriterion("SMOKE114", "fail", "Cursor rule omitted bundled Markdown support")
        registry = _run([str(fieldkit), "commands", "--json"], cwd=run_dir, env=env)
        criteria.append(_criterion("SMOKE109", registry))
        if registry.returncode == 0:
            try:
                registry_names = {entry["full_name"] for entry in json.loads(registry.stdout)}
            except (json.JSONDecodeError, KeyError, TypeError):
                criteria[-1] = SmokeCriterion("SMOKE109", "fail", "command registry did not emit its JSON contract")
            else:
                required_names = {"auth google", "meeting link", "skill eval", "web serve"}
                if not required_names <= registry_names:
                    criteria[-1] = SmokeCriterion("SMOKE109", "fail", "optional commands missing from base registry")
        features = _run([str(fieldkit), "version", "--features", "--json"], cwd=run_dir, env=env)
        criteria.append(_criterion("SMOKE111", features))
        if features.returncode == 0:
            try:
                feature_groups = {entry["group"]: entry for entry in json.loads(features.stdout)["cli"]["groups"]}
                auth_names = {entry["name"] for entry in feature_groups["auth"]["subcommands"]}
                gmail_names = {entry["name"] for entry in feature_groups["gmail"]["subcommands"]}
            except (json.JSONDecodeError, KeyError, TypeError):
                criteria[-1] = SmokeCriterion("SMOKE111", "fail", "version features did not emit its JSON contract")
            else:
                if not {"google", "sf", "shadowbot"} <= auth_names or not {"query", "sync"} <= gmail_names:
                    criteria[-1] = SmokeCriterion("SMOKE111", "fail", "version features omitted command metadata")
        if profile == "base":
            live_eval_env = dict(env)
            live_eval_env.pop("FIELDKIT_NO_LLM")
            behavioral = _run(
                [str(fieldkit), "skill", "eval", "--behavioral", "--skill", "brief", "--json"],
                cwd=run_dir,
                env=live_eval_env,
            )
            criteria.append(_criterion("SMOKE110", behavioral, expected_exit=3))
            if behavioral.returncode == 3 and "optional profile" not in behavioral.stderr:
                criteria[-1] = SmokeCriterion("SMOKE110", "fail", "missing LLM-profile guidance")
            missing_profile = _run([str(fieldkit), "web", "--help"], cwd=run_dir, env=env)
            criteria.append(_criterion("SMOKE107", missing_profile, expected_exit=3))
            if missing_profile.returncode == 3 and "optional profile" not in missing_profile.stderr:
                criteria[-1] = SmokeCriterion("SMOKE107", "fail", "missing optional-profile guidance")
        else:
            profile_literal = repr(profile)
            import_program = f"""import importlib
from fieldkit.config.optional_dependencies import OPTIONAL_PROFILE_IMPORT_ROOTS
profile = {profile_literal}
profiles = OPTIONAL_PROFILE_IMPORT_ROOTS if profile == 'all' else {{profile: OPTIONAL_PROFILE_IMPORT_ROOTS[profile]}}
for roots in profiles.values():
    for root in roots:
        importlib.import_module(root)
"""
            criteria.append(_criterion("SMOKE201", _run([str(python), "-c", import_program], cwd=run_dir, env=env)))
            commands = {
                "google": (
                    ("SMOKE202", ["auth", "google", "--help"]),
                    ("SMOKE203", ["gmail", "sync", "--help"]),
                    ("SMOKE204", ["meeting", "--help"]),
                ),
                "llm": (("SMOKE205", ["skill", "eval", "--help"]),),
                "web": (("SMOKE206", ["web", "--help"]),),
                "chrome-auth": (),
                "all": (
                    ("SMOKE202", ["auth", "google", "--help"]),
                    ("SMOKE203", ["gmail", "sync", "--help"]),
                    ("SMOKE204", ["meeting", "--help"]),
                    ("SMOKE205", ["skill", "eval", "--help"]),
                    ("SMOKE206", ["web", "--help"]),
                ),
            }
            for criterion_id, command in commands[profile]:
                criteria.append(_criterion(criterion_id, _run([str(fieldkit), *command], cwd=run_dir, env=env)))
        uninstall = _run(
            [uv, "pip", "uninstall", "--python", str(python), "fieldkit-cli"]
            if uv is not None
            else [str(python), "-m", "pip", "uninstall", "--yes", "fieldkit-cli"],
            cwd=run_dir,
            env=env,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        criteria.append(_criterion("SMOKE116", uninstall))
        if uninstall.returncode == 0:
            reinstall = (
                offline_install()
                if closure is not None
                else _run(install_argv, cwd=run_dir, env=env, timeout=INSTALL_TIMEOUT_SECONDS)
            )
            criteria.append(_criterion("SMOKE117", reinstall))
            if reinstall.returncode == 0:
                recovered = _run([str(fieldkit), "--version"], cwd=run_dir, env=env)
                criteria.append(_criterion("SMOKE118", recovered))
                if recovered.returncode == 0 and recovered.stdout.strip() != f"fieldkit {version}":
                    criteria[-1] = SmokeCriterion(
                        "SMOKE118", "fail", f"unexpected recovered version: {recovered.stdout.strip()}"
                    )
        unexpected_run_dir_entries = [entry for entry in run_dir.iterdir() if entry != documented_workspace]
        if unexpected_run_dir_entries:
            criteria.append(SmokeCriterion("SMOKE108", "fail", "command wrote into the unrelated working directory"))
        else:
            criteria.append(SmokeCriterion("SMOKE108", "pass"))

    return SmokeReport(
        2,
        revision,
        resolved_artifact.name,
        digest,
        profile,
        platform.python_version(),
        platform.platform(),
        tuple(criteria),
    )


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--source-revision")
    parser.add_argument("--diagnostic-dirty", action="store_true")
    parser.add_argument("--expected-version")
    parser.add_argument("--profile", choices=("base", "google", "llm", "web", "chrome-auth", "all"), default="base")
    parser.add_argument("--installer", choices=("pip", "uv"), default="pip")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run installed-artifact smoke and return a stable process status."""
    args = _parser().parse_args(argv)
    try:
        report = smoke(
            args.artifact,
            repo_root=args.repo_root.resolve(),
            source_revision=args.source_revision,
            diagnostic_dirty=args.diagnostic_dirty,
            expected_version=args.expected_version,
            profile=args.profile,
            installer=args.installer,
        )
    except (OSError, ValueError, subprocess.SubprocessError, tomllib.TOMLDecodeError) as exc:
        print(f"Artifact smoke: ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"Artifact smoke: {'PASS' if report.ok else 'FAIL'} "
            f"({report.artifact_name} sha256:{report.artifact_sha256})"
        )
        for criterion in report.criteria:
            print(f"  {criterion.criterion_id}: {criterion.status.upper()}")
            if criterion.diagnostic:
                print(f"    {criterion.diagnostic}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
