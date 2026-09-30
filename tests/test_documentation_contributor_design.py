"""Bounded structural guards, not a contributor journey or code-safety proof.

Reviewed prose units are pinned in full: unrelated correct words cannot rescue
a false assertion. Wording changes require review of these source bindings.
No command extracted from documentation is executed.
"""

import ast
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

AGENT_UNITS = (
    """Exit statuses are `0` success, `1` partial/retryable, `2` authentication or
    user action required, and `3` invalid data or usage; dispatcher interruption
    exits `130`. Domain code must not terminate the process or raise Click usage
    errors. Shared CLI boundaries normalize errors and statuses through
    `cli_exit.cli_main()` and the dispatcher; the process launcher calls
    `sys.exit(main())`. See the [exit-code contract](docs/reference/exit-codes.md).""",
    """Runtime writes are limited to the configured workspace, runtime-data root,
    documented fieldkit configuration/data locations, and documented disposable
    scratch locations. See [persistent roots and harness scratch](docs/concepts.md#persistent-roots-and-harness-scratch)
    and [local data and privacy](docs/privacy.md) for harness and temporary-file
    boundaries. Validate user-derived paths before writing.""",
)
DESIGN_UNITS = (
    """The intended architecture puts input and output adaptation in commands and
    behavior in domain modules. This is contributor policy, not proof that every
    existing command already meets it.""",
    """Installed code, workspace content, and runtime data have separate
    responsibilities, resolved through configured roots. Runtime data defaults
    to the workspace's `data` directory; physical separation is optional. See the
    [product model](../concepts.md#persistent-roots-and-harness-scratch).""",
    """A command can write only to a configured workspace, runtime-data root, or
    documented fieldkit configuration or disposable scratch location. The
    [product model](../concepts.md#persistent-roots-and-harness-scratch) describes
    harness worktrees; [local data and privacy](../privacy.md) describes private
    temporary snapshots and their cleanup limits.""",
    """Exit status distinguishes success, retryable failure, required user action,
    and invalid data; dispatcher interruption exits `130`. The
    [exit-code contract](../reference/exit-codes.md) defines the shared CLI
    boundaries and process-launcher behavior.""",
    """The architecture chose thin command adapters over command-owned domain logic.
    That keeps a behavior reusable by hooks and tests, and gives each convention
    one authoritative implementation. The [roots decision](../adr/0001-local-first-roots.md)
    separates content responsibilities so a checkout, user work, and generated data
    can be handled according to their ownership and persistence needs. Contributors
    must follow the configured locations rather than assume disjoint directories.""",
)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _units(text: str) -> list[str]:
    return [_normalize(unit.removeprefix("- ")) for unit in re.split(r"\n\s*\n|\n(?=- )", text)]


def _guard(agents: str, design: str) -> list[list[str]]:
    prohibited = (
        r"only\s+`?(?:cli_exit\.)?cli_main\([^)]*\)`?\s+calls\s+`?sys\.exit",
        r"physically disjoint|cannot be committed together",
        r"every (?:existing )?command\b[^.]*\bthin adapter",
    )
    for text, reviewed in ((agents, AGENT_UNITS), (design, DESIGN_UNITS)):
        units = _units(text)
        for pattern in prohibited:
            assert not re.search(pattern, _normalize(text), re.IGNORECASE), "reviewed prose contains a false claim"
        for unit in reviewed:
            assert units.count(_normalize(unit)) == 1, "reviewed prose unit changed"
    fences = re.findall(r"^```bash\n(.*?)^```$", agents, re.MULTILINE | re.DOTALL)
    assert len(fences) == 2, "placeholder fence inventory changed"
    commands = [shlex.split(line) for line in fences[1].splitlines()]
    assert commands == [
        ["uv", "run", "ruff", "check", "path/to/file.py"],
        ["uvx", "pyright", "path/to/file.py"],
    ], "placeholder commands changed"
    return commands


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def test_reviewed_claims_keep_their_canonical_source_bindings() -> None:
    paths = ast.parse((ROOT / "src/fieldkit/config/_paths.py").read_text(encoding="utf-8"))
    default_data = _function(paths, "_get_fieldkit_data_from_config")
    assert isinstance(default_data.body[-1], ast.Return)
    assert ast.unparse(default_data.body[-1]) == "return get_fieldkit_home() / 'data'"
    data = _function(paths, "get_fieldkit_data")
    assert ast.unparse(data.body[-1]) == "return _get_fieldkit_data_from_config()"
    scratch = _function(paths, "get_harness_scratch_root")
    scratch_source = ast.unparse(scratch)
    assert "os.environ.get('FIELDKIT_HARNESS_ROOT')" in scratch_source
    assert "os.environ.get('XDG_CACHE_HOME')" in scratch_source
    assert "Path.home() / '.cache'" in scratch_source
    assert ast.unparse(scratch.body[-1]) == "return (base / 'fieldkit').resolve()"

    launcher = ast.parse((ROOT / "src/fieldkit/__main__.py").read_text(encoding="utf-8"))
    interruption = _function(launcher, "_interruption_exit_code")
    assert isinstance(interruption.body[1], ast.If)
    assert ast.unparse(interruption.body[1].body[0]) == "return 130"
    assert ast.unparse(interruption.body[-1]) == "return 1"
    assert isinstance(launcher.body[-1], ast.If)
    assert ast.unparse(launcher.body[-1].test) == "__name__ == '__main__'"
    assert ast.unparse(launcher.body[-1].body[0]) == "sys.exit(main())"

    boundary = ast.parse((ROOT / "src/fieldkit/cli_exit.py").read_text(encoding="utf-8"))
    constants = {
        node.target.id: node.value.value
        for node in boundary.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id.startswith("EXIT_")
        and isinstance(node.value, ast.Constant)
    }
    assert constants == {"EXIT_SUCCESS": 0, "EXIT_PARTIAL": 1, "EXIT_AUTH": 2, "EXIT_DATA": 3}
    cli_boundary = ast.unparse(_function(boundary, "cli_main"))
    assert "sys.exit(normalize_explicit_exit(exc))" in cli_boundary
    assert "sys.exit(handle_cli_exception(exc))" in cli_boundary


def _documents() -> tuple[str, str]:
    return (
        (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        (ROOT / "docs/design/local-first-cli.md").read_text(encoding="utf-8"),
    )


def test_reviewed_contributor_design() -> None:
    agents, design = _documents()
    result = _guard(agents, design)
    assert result == [
        ["uv", "run", "ruff", "check", "path/to/file.py"],
        ["uvx", "pyright", "path/to/file.py"],
    ]


@pytest.mark.parametrize(
    "false_claim",
    [
        "Only cli_exit.cli_main() calls sys.exit().",
        "The roots are physically disjoint and cannot be committed together.",
        "Every existing command is a thin adapter.",
    ],
)
def test_correct_units_elsewhere_do_not_rescue_false_claims(false_claim: str) -> None:
    agents, design = _documents()
    with pytest.raises(AssertionError, match="reviewed prose contains a false claim"):
        _guard(agents, design + "\n\n" + false_claim)


@pytest.mark.parametrize(
    ("document", "old", "new"),
    [
        (
            "agents",
            "Shared CLI boundaries normalize errors and statuses through",
            "Only cli_exit.cli_main() calls sys.exit(). Shared CLI boundaries normalize errors and statuses through",
        ),
        (
            "design",
            "physical separation is optional",
            "the roots are physically disjoint and cannot be committed together",
        ),
        ("agents", "and documented disposable\n  scratch locations", ""),
        (
            "design",
            "This is contributor policy, not proof that every\n  existing command already meets it.",
            "Every existing command is a thin adapter.",
        ),
        ("agents", "exits `130`", "exits `3`"),
        ("design", "exits `130`", "exits `1`"),
        ("agents", "uvx pyright path/to/file.py", "uv run pyright path/to/file.py"),
        ("agents", "uv run ruff check path/to/file.py", "uv run ruff check path/to/file.py; echo injected"),
        ("agents", "uvx pyright path/to/file.py", "uvx pyright src/fieldkit/example.py"),
        (
            "agents",
            "uv run ruff check path/to/file.py\nuvx pyright path/to/file.py",
            "uvx pyright path/to/file.py\nuv run ruff check path/to/file.py",
        ),
    ],
)
def test_false_claim_and_placeholder_mutations_are_refused(document: str, old: str, new: str) -> None:
    agents, design = _documents()
    original = agents if document == "agents" else design
    assert old in original
    mutated = original.replace(old, new, 1)
    with pytest.raises(AssertionError, match=r"reviewed|placeholder"):
        _guard(mutated if document == "agents" else agents, mutated if document == "design" else design)
