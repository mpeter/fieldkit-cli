#!/usr/bin/env python3
"""Install one fieldkit artifact outside the checkout and exercise a declared profile contract."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = Path("pyproject.toml")
COMMAND_TIMEOUT_SECONDS = 60
VENV_CREATE_TIMEOUT_SECONDS = 180
INSTALL_TIMEOUT_SECONDS = 300
_MAX_DIAGNOSTIC_CHARS = 500
Installer = Literal["pip", "uv"]


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
    source_revision: str
    artifact_name: str
    artifact_sha256: str
    profile: str
    python: str
    platform: str
    criteria: tuple[SmokeCriterion, ...]

    @property
    def ok(self) -> bool:
        """Return whether every installed-package criterion passed."""
        return all(criterion.status == "pass" for criterion in self.criteria)

    def to_dict(self) -> dict[str, object]:
        """Render the stable machine-readable evidence shape."""
        return {**asdict(self), "status": "pass" if self.ok else "fail"}


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


def _revision(repo_root: Path) -> str:
    """Read the exact Git revision represented by the smoke."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=False, timeout=30
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise OSError("git rev-parse HEAD failed; pass --source-revision")
    return result.stdout.strip()


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


def smoke(
    artifact: Path,
    *,
    repo_root: Path = REPO_ROOT,
    source_revision: str | None = None,
    expected_version: str | None = None,
    profile: str = "base",
    installer: Installer = "pip",
) -> SmokeReport:
    """Run a portable installation-profile contract against one exact artifact."""
    resolved_artifact = artifact.resolve()
    digest = hashlib.sha256(resolved_artifact.read_bytes()).hexdigest()
    revision = source_revision or _revision(repo_root)
    version = expected_version or _expected_version(repo_root)
    criteria: list[SmokeCriterion] = []
    uv = shutil.which("uv") if installer == "uv" else None
    if installer == "uv" and uv is None:
        raise OSError("uv installer requested but uv is not on PATH")

    with tempfile.TemporaryDirectory(prefix="fieldkit-artifact-smoke-") as raw_root:
        root = Path(raw_root)
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
            "TMPDIR": str(temporary_dir),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "FIELDKIT_NO_LLM": "1",
        }
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
        install = _run(
            install_argv,
            cwd=run_dir,
            env=env,
            timeout=INSTALL_TIMEOUT_SECONDS,
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
            tool_env = {
                **env,
                "UV_TOOL_DIR": str(root / "uv-tools"),
                "UV_TOOL_BIN_DIR": str(tool_bin),
                "UV_NO_CACHE": "1",
            }
            tool_install = _run([tool_uv, "tool", "install", str(resolved_artifact)], cwd=run_dir, env=tool_env)
            criteria.append(_criterion("SMOKE119", tool_install, expected_exit=0))
            if tool_install.returncode == 0:
                tool_version = _run([str(tool_bin / "fieldkit"), "--version"], cwd=run_dir, env=tool_env)
                criteria.append(_criterion("SMOKE120", tool_version))
                if tool_version.returncode == 0 and tool_version.stdout.strip() != f"fieldkit {version}":
                    criteria[-1] = SmokeCriterion(
                        "SMOKE120", "fail", f"unexpected uv tool version: {tool_version.stdout.strip()}"
                    )

        asset_program = """from importlib.resources import files
root = files('fieldkit')
required = ('_data/pursuit-frontmatter.schema.json', '_data/pursuit-narrative-template.md',
            'ingest/schema.sql', 'gmail/schema.sql', 'skills/README.md', 'web/static/index.html')
assert all(root.joinpath(path).is_file() for path in required)
"""
        criteria.append(_criterion("SMOKE103", _run([str(python), "-c", asset_program], cwd=run_dir, env=env)))
        criteria.append(
            _criterion("SMOKE104", _run([str(fieldkit), "init", "--minimal", str(workspace)], cwd=run_dir, env=env))
        )
        answers_workspace = root / "answers-workspace"
        answers_path = root / "init-answers.yaml"
        answers_path.write_text(
            "name: Example User\nemail: user@example.com\ndata_dir: " + str(answers_workspace) + "\n",
            encoding="utf-8",
        )
        answers_init = _run([str(fieldkit), "init", "--answers", str(answers_path)], cwd=run_dir, env=env)
        criteria.append(_criterion("SMOKE123", answers_init))
        if answers_init.returncode == 0 and not answers_workspace.is_dir():
            criteria[-1] = SmokeCriterion("SMOKE123", "fail", "answers initialization did not create its workspace")
        documented_workspace = run_dir / "fieldkit-workspace"
        documented_init = _run([str(fieldkit), "init", "--minimal", "./fieldkit-workspace"], cwd=run_dir, env=env)
        criteria.append(_criterion("SMOKE113", documented_init))
        if documented_init.returncode == 0 and not documented_workspace.is_dir():
            criteria[-1] = SmokeCriterion(
                "SMOKE113", "fail", "minimal initialization did not create the documented workspace"
            )
        brief_dry_run = _run(
            [str(fieldkit), "brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"],
            cwd=run_dir,
            env=env,
        )
        criteria.append(_criterion("SMOKE124", brief_dry_run))
        if brief_dry_run.returncode == 0 and (documented_workspace / "briefs").exists():
            criteria[-1] = SmokeCriterion("SMOKE124", "fail", "brief dry run created a brief directory")
        network_guard = root / "network-guard"
        network_guard.mkdir()
        (network_guard / "sitecustomize.py").write_text(
            "import socket\n"
            "def _blocked(*_args, **_kwargs):\n"
            "    raise OSError('network access is forbidden in artifact smoke')\n"
            "socket.socket.connect = _blocked\n"
            "socket.getaddrinfo = _blocked\n",
            encoding="utf-8",
        )
        offline_env = {**env, "PYTHONPATH": str(network_guard)}
        criteria.append(
            _criterion(
                "SMOKE115",
                _run(
                    [str(fieldkit), "init", "--minimal", str(root / "offline-workspace")], cwd=run_dir, env=offline_env
                ),
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
                required_names = {"auth google", "meeting link", "driver run", "web serve"}
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
                "llm": (("SMOKE205", ["driver", "--help"]),),
                "web": (("SMOKE206", ["web", "--help"]),),
                "chrome-auth": (),
                "all": (
                    ("SMOKE202", ["auth", "google", "--help"]),
                    ("SMOKE203", ["gmail", "sync", "--help"]),
                    ("SMOKE204", ["meeting", "--help"]),
                    ("SMOKE205", ["driver", "--help"]),
                    ("SMOKE206", ["web", "--help"]),
                ),
            }
            for criterion_id, command in commands[profile]:
                criteria.append(_criterion(criterion_id, _run([str(fieldkit), *command], cwd=run_dir, env=env)))
        uninstall = _run(
            [str(python), "-m", "pip", "uninstall", "--yes", "fieldkit-cli"],
            cwd=run_dir,
            env=env,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        criteria.append(_criterion("SMOKE116", uninstall))
        if uninstall.returncode == 0:
            reinstall = _run(install_argv, cwd=run_dir, env=env, timeout=INSTALL_TIMEOUT_SECONDS)
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
