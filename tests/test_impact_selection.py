"""End-to-end regressions for data changes outside the Python import graph."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
_COMMAND_TIMEOUT_SECONDS = 30


def _run(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize(
    ("change", "pytest_options"),
    [
        ("schema", "none"),
        ("staged-python", "none"),
        ("conftest", "none"),
        ("script", "none"),
        ("dirty-head", "none"),
        ("schema", "environment"),
        ("schema", "configuration"),
    ],
)
def test_change_runs_its_consumer_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, pytest_options: str
) -> None:
    """Data and hidden staged changes cannot leave the consumer test deselected."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tach.toml").write_text('source_roots = ["."]\n', encoding="utf-8")
    if pytest_options == "configuration":
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = ["-k", "no_matching_test"]\n', encoding="utf-8"
        )
    (tmp_path / "app.py").write_text(
        'from pathlib import Path\ndef schema():\n    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")\n',
        encoding="utf-8",
    )
    (tmp_path / "schema.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        'from app import schema\ndef test_schema():\n    assert schema() == "SELECT 1;"\n', encoding="utf-8"
    )
    (tmp_path / "tests" / "conftest.py").write_text(
        """import pytest
@pytest.fixture(autouse=True)
def valid_environment():
    assert True
""",
        encoding="utf-8",
    )
    if change == "script":
        (tmp_path / "helper.py").write_text('print("SELECT 1;")\n', encoding="utf-8")
        (tmp_path / "app.py").write_text(
            "import subprocess\nimport sys\nfrom pathlib import Path\n_TIMEOUT = 5\n"
            'def schema():\n    return subprocess.run([sys.executable, str(Path(__file__).with_name("helper.py"))], '
            "check=True, capture_output=True, text=True, timeout=_TIMEOUT).stdout.strip()\n",
            encoding="utf-8",
        )
    assert _run(tmp_path, "git", "init").returncode == 0
    assert _run(tmp_path, "git", "add", ".").returncode == 0
    assert (
        _run(
            tmp_path,
            "git",
            "-c",
            "user.name=Test Contributor",
            "-c",
            "user.email=contributor@example.com",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "fixture",
        ).returncode
        == 0
    )
    if change in {"schema", "dirty-head"}:
        (tmp_path / "schema.sql").write_text("SELECT 2;", encoding="utf-8")
    elif change == "staged-python":
        source = tmp_path / "app.py"
        original = source.read_text(encoding="utf-8")
        source.write_text('def schema():\n    return "SELECT 2;"\n', encoding="utf-8")
        assert _run(tmp_path, "git", "add", "app.py").returncode == 0
        source.write_text(original, encoding="utf-8")
    elif change == "conftest":
        (tmp_path / "tests" / "conftest.py").write_text(
            """import pytest
@pytest.fixture(autouse=True)
def valid_environment():
    assert False
""",
            encoding="utf-8",
        )
    else:
        (tmp_path / "helper.py").write_text('print("SELECT 2;")\n', encoding="utf-8")

    runner = Path(__file__).resolve().parents[1] / "scripts" / "run_impact_tests.py"
    if pytest_options == "environment":
        monkeypatch.setenv("PYTEST_ADDOPTS", "-k no_matching_test")
    head_options = []
    if change == "dirty-head":
        head = _run(tmp_path, "git", "rev-parse", "HEAD")
        assert head.returncode == 0
        head_options = ["--head", head.stdout.strip()]
    result = _run(
        tmp_path, sys.executable, str(runner), "--base", "HEAD", "--github-output", "step-output", *head_options
    )

    if change == "dirty-head":
        assert result.returncode == 2, result.stdout + result.stderr
        assert "clean worktree" in result.stderr
        assert not (tmp_path / "step-output").exists()
        return

    assert result.returncode == (0 if change == "staged-python" else 1), result.stdout + result.stderr
    expected_summary = {"staged-python": "1 passed", "conftest": "1 error"}.get(change, "1 failed")
    assert expected_summary in result.stdout
    assert '"test_scope": "full-repository"' in result.stdout
    assert (tmp_path / "step-output").read_text(encoding="utf-8") == "test_scope=full-repository\n"
