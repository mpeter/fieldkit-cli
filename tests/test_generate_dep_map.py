"""Tests for the public dependency-map generator."""

import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from scripts import generate_cli_docs, generate_dep_map

pytestmark = pytest.mark.unit


@pytest.fixture
def dependency_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sources = {
        "src/fieldkit/commands/demo/cli.py": (
            "import fieldkit.sf.client\nfrom fieldkit.gmail.sync import sync\n"
            "from fieldkit import config\nfrom ...pursuit import io\n"
        ),
        "src/fieldkit/sf/client.py": "pass\n",
        "src/fieldkit/gmail/sync.py": "pass\n",
        "src/fieldkit/config/__init__.py": "pass\n",
        "src/fieldkit/pursuit/io.py": "pass\n",
        "hooks/worker.py": "from fieldkit.config import load\n",
        "tach.toml": '[[modules]]\npath = "fieldkit.config"\ndepends_on = []\n',
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(generate_dep_map, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        generate_dep_map, "_COMMANDS", {"sample": ("Sample", "fieldkit.commands.demo.cli")}, raising=False
    )
    return tmp_path


def test_import_inventory_resolves_all_import_forms(dependency_repo: Path) -> None:
    result = generate_dep_map.read_imports()

    assert result["fieldkit.commands.demo.cli"] == {
        "fieldkit.sf.client",
        "fieldkit.gmail.sync",
        "fieldkit.config",
        "fieldkit.pursuit",
    }
    assert result["hooks.worker"] == {"fieldkit.config"}


@pytest.mark.parametrize("content", [b"\xff", b"def broken(:\n"])
def test_invalid_source_cannot_disappear_from_inventory(dependency_repo: Path, content: bytes) -> None:
    path = dependency_repo / "src/fieldkit/broken.py"
    path.write_bytes(content)

    with pytest.raises((UnicodeDecodeError, SyntaxError)) as error:
        generate_dep_map.read_imports()

    assert error.value is not None


def test_unreadable_source_fails_inventory(dependency_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.open

    def open_source(path: Path, flags: int) -> int:
        if path.name == "worker.py":
            raise PermissionError("source unavailable")
        return original(path, flags)

    monkeypatch.setattr(os, "open", open_source)
    with pytest.raises(PermissionError, match="source unavailable"):
        generate_dep_map.read_imports()


def test_dependency_map_uses_registry_and_declared_boundaries(dependency_repo: Path) -> None:
    result = generate_dep_map.generate()

    assert "| `fieldkit sample` | `fieldkit.commands.demo.cli` |" in result
    assert "`fieldkit.config`" in result
    assert "commands/docs" not in result
    assert "Boundary enforcement:" not in result
    assert "Auto-generated 20" not in result


def test_registered_missing_module_fails_generation(dependency_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        generate_dep_map, "_COMMANDS", {"missing": ("Missing", "fieldkit.commands.missing.cli")}, raising=False
    )
    with pytest.raises(ValueError, match="registered command module"):
        generate_dep_map.generate()


@pytest.mark.parametrize(
    "policy",
    [
        "",
        "modules = [42]",
        '[[modules]]\npath = "fieldkit.missing"\ndepends_on = []',
        '[[modules]]\npath = "fieldkit.config"\ndepends_on = ["fieldkit.missing"]',
        '[[modules]]\npath = "fieldkit.config"\ndepends_on = 42',
        '[[modules]]\npath = "fieldkit.config"\ndepends_on = []\n'
        '[[modules]]\npath = "fieldkit.config"\ndepends_on = []',
    ],
)
def test_invalid_boundary_policy_fails_generation(dependency_repo: Path, policy: str) -> None:
    (dependency_repo / "tach.toml").write_text(policy, encoding="utf-8")
    with pytest.raises(ValueError, match="Tach"):
        generate_dep_map.generate()


def test_source_symlink_fails_inventory(dependency_repo: Path) -> None:
    (dependency_repo / "src/fieldkit/linked.py").symlink_to(dependency_repo / "hooks/worker.py")
    with pytest.raises(ValueError, match="symlinks"):
        generate_dep_map.read_imports()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are not available")
def test_source_fifo_fails_inventory_without_reading(dependency_repo: Path) -> None:
    root = dependency_repo / "src/fieldkit"
    os.mkfifo(root / "blocked.py")

    with pytest.raises(ValueError, match="regular file"):
        list(generate_dep_map._python_files(root))


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_source_reader_rejects_nonregular_inputs(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "input.py"
    if kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("named pipes are not available")
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target.py"
        target.write_text("pass\n", encoding="utf-8")
        path.symlink_to(target)

    with pytest.raises((ValueError, OSError)) as error:
        generate_dep_map._read_source(path)

    assert error.value is not None


@pytest.mark.parametrize("extra", [0, 1])
def test_source_reader_enforces_size_bound(tmp_path: Path, extra: int) -> None:
    path = tmp_path / "input.py"
    content = "#" + " " * (generate_dep_map.MAX_SOURCE_BYTES - 1 + extra)
    path.write_text(content, encoding="utf-8")

    if extra:
        with pytest.raises(ValueError, match="size limit"):
            generate_dep_map._read_source(path)
    else:
        result = generate_dep_map._read_source(path)
        assert result == content


def test_policy_symlink_fails_generation(dependency_repo: Path) -> None:
    policy = dependency_repo / "tach.toml"
    target = dependency_repo / "policy.toml"
    policy.rename(target)
    policy.symlink_to(target)

    with pytest.raises(OSError) as error:
        generate_dep_map.generate()

    assert error.value is not None


def test_empty_adapter_distinguishes_observation_from_policy(dependency_repo: Path) -> None:
    (dependency_repo / "src/fieldkit/commands/demo/cli.py").write_text("pass\n", encoding="utf-8")

    result = generate_dep_map.generate()

    assert "| `fieldkit sample` | `fieldkit.commands.demo.cli` | None observed |" in result
    assert "| `fieldkit.config` | None declared |" in result


def test_unknown_fieldkit_import_fails_inventory(dependency_repo: Path) -> None:
    (dependency_repo / "src/fieldkit/broken.py").write_text("import fieldkit.missing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unresolved fieldkit imports"):
        generate_dep_map.read_imports()


def test_generation_is_deterministic_and_does_not_run_tools(
    dependency_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("dependency generation must not execute a subprocess")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)

    first = generate_dep_map.generate()
    second = generate_dep_map.generate()

    assert first == second
    assert "| `hooks` | `fieldkit.config` |" in first


def test_public_dependency_map_excludes_operator_specific_mcp_registry() -> None:
    """The exported reference contains portable codebase facts only."""
    generated = generate_dep_map.generate()

    assert "MCP Groups" not in generated
    assert "mcpjungle" not in generated
    assert "<fieldkit-mcp-dir>" not in generated
    assert "fieldkit-dataverse" not in generated


@pytest.mark.parametrize("generator", [generate_cli_docs, generate_dep_map])
def test_newer_output_does_not_bypass_content_check(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "reference.md"
    output.write_text("stale content\n", encoding="utf-8")
    os.utime(output, (4102444800, 4102444800))
    monkeypatch.setattr(generator, "OUTPUT", output)
    monkeypatch.setattr(generator, "generate", lambda: "current content\n")

    result = generator.main(["--check"])

    assert result == 1
    assert output.read_text(encoding="utf-8") == "stale content\n"


@pytest.mark.parametrize("generator", [generate_cli_docs, generate_dep_map])
def test_generated_reference_check_accepts_exact_content(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "reference.md"
    output.write_text("current content\n", encoding="utf-8")
    monkeypatch.setattr(generator, "OUTPUT", output)
    monkeypatch.setattr(generator, "generate", lambda: "current content\n")

    result = generator.main(["--check"])

    assert result == 0


@pytest.mark.parametrize("generator", [generate_cli_docs, generate_dep_map])
@pytest.mark.parametrize("option", ["--chek", "--che"])
def test_generated_reference_rejects_unknown_option(generator: ModuleType, option: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        generator.main([option])


@pytest.mark.parametrize("generator", [generate_cli_docs, generate_dep_map])
def test_missing_reference_check_does_not_create_output(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "missing.md"
    monkeypatch.setattr(generator, "OUTPUT", output)

    result = generator.main(["--check"])

    assert result == 1
    assert not output.exists()


@pytest.mark.parametrize("generator", [generate_cli_docs, generate_dep_map])
def test_reference_generation_writes_utf8(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "new" / "reference.md"
    monkeypatch.setattr(generator, "OUTPUT", output)
    monkeypatch.setattr(generator, "generate", lambda: "# Reference\n\n✓ current\n")

    result = generator.main([])

    assert result == 0
    assert output.read_text(encoding="utf-8") == "# Reference\n\n✓ current\n"
