"""Held offline dependency inputs stay explicit and read-only in owners."""

import json
import os
from pathlib import Path

import pytest

from scripts import documentation_command_runner as runner
from scripts.documentation_runtime import DependencyInput

pytestmark = pytest.mark.integration


def test_dependency_mount_is_read_only_and_does_not_expose_parent(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    closure = tmp_path / "closure"
    closure.mkdir()
    (closure / "original.whl").write_bytes(b"original bytes")
    (tmp_path / "parent-secret").write_text("private", encoding="utf-8")
    descriptor = os.open(closure, os.O_PATH | os.O_DIRECTORY)
    program = """
import json,os
from pathlib import Path
root=Path('/run/fieldkit-doc-dependencies')
readonly=False
try:
    (root/'original.whl').write_bytes(b'altered')
except OSError:
    readonly=True
print(json.dumps({'data':(root/'original.whl').read_text(), 'readonly':readonly,
                  'host_parent':Path(os.environ['SOURCE_PARENT']).exists()}))
"""
    try:
        result = runner._run(
            candidate,
            (
                "/run/python",
                "-S",
                "-c",
                program.replace("os.environ['SOURCE_PARENT']", repr(str(tmp_path / "parent-secret"))),
            ),
            dependency_input=DependencyInput(descriptor, "a" * 64),
        )
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout) == {"data": "original bytes", "readonly": True, "host_parent": False}
        assert os.fstat(descriptor).st_ino == closure.stat().st_ino
    finally:
        os.close(descriptor)


@pytest.mark.unit
@pytest.mark.parametrize("argv", [runner._ARTIFACT_DIAGNOSTIC_ARGV, runner._ARTIFACT_DIAGNOSTIC_ARGV[:-1]])
def test_only_registered_artifact_argv_gets_retained_input(tmp_path: Path, argv: tuple[str, ...]) -> None:
    dependency = DependencyInput(-1, "b" * 64)
    result = runner._owner_argv(tmp_path, argv, dependency)
    assert result[-4:] == (
        "--dependency-root",
        "/run/fieldkit-doc-dependencies",
        "--dependency-manifest-sha256",
        "b" * 64,
    )
    changed = (*argv, "--unexpected")
    assert runner._owner_argv(tmp_path, changed, dependency) == runner._sandbox_argv(tmp_path, changed)
    assert runner._owner_argv(tmp_path, argv, None) == runner._sandbox_argv(tmp_path, argv)


@pytest.mark.unit
def test_invalid_dependency_digest_fails_before_starting_owner(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="digest is invalid"):
        runner._run(tmp_path, ("/run/python", "-S", "-c", "pass"), dependency_input=DependencyInput(-1, "invalid"))
