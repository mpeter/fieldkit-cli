"""Candidate controller code must not execute with host authority."""

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import documentation_candidate_execution as execution
from scripts import documentation_command_runner as command_runner
from scripts.documentation_runtime import DependencyInput, RuntimeTools

pytestmark = pytest.mark.unit


def test_candidate_controller_cannot_fabricate_supervised_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    runtime = tmp_path / "runtime"
    scripts = snapshot / "scripts"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    host_marker = tmp_path / "host-write"
    (scripts / "documentation_candidate_worker.py").write_text(
        "import json,pathlib,sys\n"
        f"pathlib.Path({str(host_marker)!r}).write_text('escaped', encoding='utf-8')\n"
        "result=sys.argv[sys.argv.index('--result')+1]\n"
        "plan=json.load(open(sys.argv[sys.argv.index('--plan')+1], encoding='utf-8'))\n"
        "json.dump({'results':[{'argv':plan[0],'exit_code':0,'stdout':'fabricated','stderr':''}],"
        "'pending':[]},open(result,'w',encoding='utf-8'))\n",
        encoding="utf-8",
    )
    descriptor = os.open("/proc/self/exe", os.O_RDONLY | os.O_CLOEXEC)
    identity = execution.TrustedInterpreter(
        descriptor,
        hashlib.sha256(Path("/proc/self/exe").read_bytes()).hexdigest(),
        os.fstat(descriptor).st_size,
    )

    @contextmanager
    def build_runtime(*_arguments: object) -> Iterator[execution.TrustedInterpreter]:
        yield identity

    prepared: DependencyInput | None = None

    @contextmanager
    def prepare(root: Path, tools: RuntimeTools, *, supplied: DependencyInput | None) -> Iterator[DependencyInput]:
        nonlocal prepared
        assert root == snapshot
        assert tools == RuntimeTools(descriptor, descriptor)
        assert supplied is None
        dependency_descriptor = os.open(snapshot, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            prepared = DependencyInput(dependency_descriptor, "b" * 64)
            yield prepared
        finally:
            os.close(dependency_descriptor)

    monkeypatch.setattr(execution, "_build_runtime", build_runtime)
    monkeypatch.setattr(execution, "prepare_dependencies", prepare)
    supervised: list[execution.BoundExecution] = []

    def supervise(context: execution.BoundExecution) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        assert context.dependency_input is prepared
        assert context.dependency_input is not None
        assert os.fstat(context.dependency_input.directory_descriptor).st_ino == snapshot.stat().st_ino
        supervised.append(context)
        return [{"argv": context.commands[0], "exit_code": 0, "stdout": "supervised", "stderr": ""}], ()

    try:
        results, pending = execution._run_bound_plan(
            snapshot,
            runtime,
            "a" * 40,
            (("owner",),),
            execution.TrustedUv(descriptor, identity.sha256, identity.size),
            identity,
            supervise,
        )
    finally:
        os.close(descriptor)

    assert results != [{"argv": ["owner"], "exit_code": 0, "stdout": "fabricated", "stderr": ""}]
    assert not host_marker.exists()
    assert len(supervised) == 1
    assert supervised[0].commands == (("owner",),)
    assert results[0]["stdout"] == "supervised"
    assert pending == ()
    assert not (runtime / "result.json").exists()
    assert prepared is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(prepared.directory_descriptor)


@pytest.mark.integration
def test_locked_owner_preserves_venv_and_held_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uv_path = Path(shutil.which("uv") or pytest.fail("uv is unavailable"))
    lock = tomllib.loads((Path(__file__).parents[1] / "uv.lock").read_text(encoding="utf-8"))
    packaging_version = next(package["version"] for package in lock["package"] if package["name"] == "packaging")
    snapshot = tmp_path / "snapshot"
    runtime = tmp_path / "runtime"
    snapshot.mkdir()
    runtime.mkdir()
    (snapshot / "pyproject.toml").write_text(
        "[project]\nname='runtime-probe'\nversion='0'\nrequires-python='>=3.11'\n"
        f"dependencies=['packaging=={packaging_version}']\n"
        "[dependency-groups]\nrelease-build=[]\n",
        encoding="utf-8",
    )
    subprocess.run(
        (str(uv_path), "lock", "--offline", "--project", str(snapshot)),
        check=True,
        capture_output=True,
        timeout=30,
        env={**os.environ, "UV_CACHE_DIR": str(execution._uv_cache_root()), "UV_INDEX_URL": "https://pypi.org/simple"},
    )
    descriptor = os.open(uv_path, os.O_RDONLY | os.O_CLOEXEC)
    uv = execution.TrustedUv(descriptor, hashlib.sha256(uv_path.read_bytes()).hexdigest(), os.fstat(descriptor).st_size)

    def reject_lookup(_command: str) -> str | None:
        pytest.fail("bound owner rediscovered a tool through ambient PATH")

    monkeypatch.setattr("scripts.documentation_command_runner.shutil.which", reject_lookup)
    try:
        with execution._trusted_interpreter() as python, execution._build_runtime(snapshot, runtime, uv, python):
            result = command_runner._run(
                snapshot,
                (
                    "uv",
                    "run",
                    "python",
                    "-I",
                    "-c",
                    "import json,sys,packaging; print(json.dumps({'prefix':sys.prefix,'dependency':packaging.__file__}))",
                ),
                runtime_root=runtime,
                runtime_tools=RuntimeTools(uv.descriptor, python.descriptor),
            )
            assert result.exit_code == 0, result.stderr
            output = json.loads(result.stdout)
            assert output["prefix"] == "/workspace/.venv"
            assert output["dependency"].startswith("/workspace/.venv/")
            assert (runtime / ".venv/bin/python").readlink() == Path("/run/python")
    finally:
        os.close(descriptor)
