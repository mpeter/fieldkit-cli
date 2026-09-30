"""Tests for revision-bound driver prompt contracts."""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_GIT_TIMEOUT_SECONDS = 10


def _contract(anchor: str = "def target():\n    return 1\n") -> str:
    indented_anchor = "\n".join(f"        {line}" for line in anchor.rstrip("\n").splitlines())
    return f"""---
covers:
  - src/example.py
depends_on: [17]
edit_sites:
  version: 1
  sites:
    - path: src/example.py
      anchor: |-
{indented_anchor}
done_checks:
  version: 1
  checks:
    - id: tests
      argv: [pytest, -q]
---

# Work Order
"""


def test_prompt_contract_parses_structured_edit_site() -> None:
    from fieldkit.driver.prompt_contract import EditSite, PromptContract, parse_prompt_contract

    result = parse_prompt_contract(_contract(), kind="work-order")

    assert result == PromptContract(
        frozenset({"src/example.py"}),
        (17,),
        (EditSite("src/example.py", "def target():\n    return 1"),),
    )


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        pytest.param("not present", "missing", id="missing"),
        pytest.param("same", "ambiguous", id="ambiguous"),
    ],
)
def test_validate_edit_sites_rejects_unknown_or_ambiguous_anchor(anchor: str, expected: str) -> None:
    from fieldkit.driver.prompt_contract import PromptContractError, parse_prompt_contract, validate_edit_sites

    contract = parse_prompt_contract(_contract(anchor), kind="work-order")
    target = "same\nother\nsame\n" if anchor == "same" else "different\n"

    with pytest.raises(PromptContractError, match=expected):
        validate_edit_sites(contract, lambda _path: target)


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src\\example.py", "src/"])
def test_prompt_contract_rejects_unsafe_target_path(path: str) -> None:
    from fieldkit.driver.prompt_contract import PromptContractError, parse_prompt_contract

    text = _contract().replace("path: src/example.py", f"path: {path}")

    with pytest.raises(PromptContractError, match=r"candidate repository|POSIX path"):
        parse_prompt_contract(text, kind="work-order")


def test_openspec_sidecar_uses_same_contract_shape() -> None:
    from fieldkit.driver.prompt_contract import parse_prompt_contract

    sidecar = """covers: [src/example.py]
depends_on: []
edit_sites:
  version: 1
  sites:
    - path: src/example.py
      anchor: existing definition
done_checks:
  version: 1
  checks:
    - id: tests
      argv: [pytest, -q]
"""

    result = parse_prompt_contract(sidecar, kind="openspec")

    assert result.covers == frozenset({"src/example.py"})
    assert result.depends_on == ()
    assert result.edit_sites[0].anchor == "existing definition"


def test_prompt_contract_requires_edit_sites() -> None:
    from fieldkit.driver.prompt_contract import PromptContractError, parse_prompt_contract

    with pytest.raises(PromptContractError, match="edit_sites"):
        parse_prompt_contract("---\ncovers: [src/example.py]\n---\n", kind="work-order")


def test_prompt_contract_rejects_duplicate_yaml_keys_without_reflecting_values() -> None:
    from fieldkit.driver.prompt_contract import PromptContractError, parse_prompt_contract

    text = "covers: [src/first-private.py]\ncovers: [src/second-private.py]\nedit_sites: []\n"

    with pytest.raises(PromptContractError, match="duplicate prompt contract key") as caught:
        parse_prompt_contract(text, kind="openspec")

    assert "first-private" not in str(caught.value)
    assert "second-private" not in str(caught.value)


def test_complete_prompt_contract_rejects_missing_covers() -> None:
    from fieldkit.driver.prompt_contract import PromptContractError, parse_prompt_contract

    text = _contract().replace("covers:\n  - src/example.py\n", "")

    with pytest.raises(PromptContractError, match="covers must declare authority"):
        parse_prompt_contract(text, kind="work-order")


@pytest.mark.parametrize(
    ("covers", "target"),
    [
        pytest.param("docs/", "src/example.py", id="different-directory"),
        pytest.param("src/example.py", "src/other.py", id="different-file"),
    ],
)
def test_prompt_contract_rejects_edit_site_outside_covers(covers: str, target: str) -> None:
    from fieldkit.driver.prompt_contract import PromptContractError, parse_prompt_contract

    text = (
        _contract().replace("  - src/example.py", f"  - {covers}", 1).replace("path: src/example.py", f"path: {target}")
    )

    with pytest.raises(PromptContractError, match="outside covers"):
        parse_prompt_contract(text, kind="work-order")


@pytest.mark.parametrize("failure", ["outside", "missing", "ambiguous"])
def test_edit_site_errors_do_not_reflect_external_paths(failure: str) -> None:
    from fieldkit.driver.prompt_contract import PromptContractError, parse_prompt_contract, validate_edit_sites

    marker = "private-customer-record"
    text = _contract("same").replace("src/example.py", f"src/{marker}.py")
    if failure == "outside":
        text = text.replace(f"  - src/{marker}.py", "  - docs/", 1)
    with pytest.raises(PromptContractError) as caught:
        contract = parse_prompt_contract(text, kind="work-order")
        validate_edit_sites(contract, lambda _path: "same same" if failure == "ambiguous" else "different")

    assert marker not in str(caught.value)


def test_contract_fixture_does_not_write_files(tmp_path: Path) -> None:
    """Contract parsing is pure; target reads happen only through the supplied callback."""
    from fieldkit.driver.prompt_contract import parse_prompt_contract

    before = tuple(tmp_path.iterdir())
    result = parse_prompt_contract(_contract(), kind="work-order")

    assert result.edit_sites
    assert tuple(tmp_path.iterdir()) == before


def test_freeze_prompt_reads_contract_and_target_from_same_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver.prompt_source import PromptSource, freeze_prompt

    source = PromptSource(tmp_path / "docs" / "work-orders" / "work.md", "work-order")
    frozen_contract = _contract("origin anchor")
    calls: list[tuple[str, str]] = []

    def fake_blob(_root: Path, revision: str, relative: str, *, max_bytes: int) -> str:
        calls.append((revision, relative))
        return frozen_contract if relative.endswith("work.md") else "origin anchor\n"

    monkeypatch.setattr("fieldkit.driver.prompt_source._git_blob", fake_blob)

    result = freeze_prompt(tmp_path, source, "a" * 40)

    assert result.revision == "a" * 40
    assert result.contract.covers == frozenset({"src/example.py"})
    assert calls == [("a" * 40, "docs/work-orders/work.md"), ("a" * 40, "src/example.py")]


def test_freeze_openspec_requires_revision_bound_sidecar(tmp_path: Path) -> None:
    from fieldkit.driver.prompt_source import PromptSource, PromptSourceError, freeze_prompt

    tasks = tmp_path / "openspec" / "changes" / "change" / "tasks.md"
    tasks.parent.mkdir(parents=True)
    tasks.write_text("# Tasks\n", encoding="utf-8")

    with pytest.raises(PromptSourceError, match="unavailable"):
        freeze_prompt(tmp_path, PromptSource(tasks, "openspec"), None)


def test_freeze_openspec_reads_prompt_before_its_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.driver.prompt_source import PromptSource, PromptSourceError, freeze_prompt

    source = PromptSource(tmp_path / "openspec" / "changes" / "change" / "tasks.md", "openspec")

    def missing_prompt(_root: Path, _revision: str, relative: str, *, max_bytes: int) -> str:
        del max_bytes
        if relative.endswith("tasks.md"):
            raise PromptSourceError("prompt missing from frozen revision")
        return ""

    monkeypatch.setattr("fieldkit.driver.prompt_source._git_blob", missing_prompt)

    with pytest.raises(PromptSourceError, match="prompt missing"):
        freeze_prompt(tmp_path, source, "a" * 40)


def test_freeze_prompt_rejects_symlink_target(tmp_path: Path) -> None:
    from fieldkit.driver.prompt_source import PromptSource, PromptSourceError, freeze_prompt

    work_order = tmp_path / "docs" / "work-orders" / "work.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text(_contract("outside anchor"), encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside anchor\n", encoding="utf-8")
    target = tmp_path / "src" / "example.py"
    target.parent.mkdir()
    target.symlink_to(outside)

    with pytest.raises(PromptSourceError, match="unsafe"):
        freeze_prompt(tmp_path, PromptSource(work_order, "work-order"), None)


def test_freeze_prompt_rejects_revision_bound_symlink(tmp_path: Path) -> None:
    from fieldkit.driver.prompt_source import PromptSource, PromptSourceError, freeze_prompt

    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True, timeout=_GIT_TIMEOUT_SECONDS
    )
    subprocess.run(
        ["git", "config", "user.email", "developer@example.com"], cwd=tmp_path, check=True, timeout=_GIT_TIMEOUT_SECONDS
    )
    subprocess.run(
        ["git", "config", "user.name", "Example Developer"], cwd=tmp_path, check=True, timeout=_GIT_TIMEOUT_SECONDS
    )
    work_order = tmp_path / "docs" / "work-orders" / "work.md"
    work_order.parent.mkdir(parents=True)
    work_order.symlink_to("../../../outside.md")
    (tmp_path / "outside.md").write_text(_contract(), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, timeout=_GIT_TIMEOUT_SECONDS)
    subprocess.run(
        ["git", "commit", "-m", "test fixture"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout.strip()

    with pytest.raises(PromptSourceError, match="not a regular file"):
        freeze_prompt(tmp_path, PromptSource(work_order, "work-order"), revision)
