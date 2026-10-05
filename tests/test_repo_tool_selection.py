"""Checkout-only tests must run regardless of the production-code diff."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.repo_tool_selection import repo_tool_test_paths

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SELECTION_TIMEOUT_SECONDS = 60


@pytest.mark.parametrize(
    "source",
    [
        "import hooks.fresh_hook",
        "from hooks import fresh_hook",
        "from scripts import fresh_script",
        "import fresh_script",
        'importlib.import_module("fresh_script")',
        'Path("scripts/fresh_script.py")',
        'ROOT / "hooks" / "fresh_hook.py"',
        'spec_from_file_location("tool", SCRIPT_DIR / "fresh_script.py")',
    ],
)
def test_new_tool_tests_are_discovered(tmp_path: Path, source: str) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/fresh_script.py").write_text("", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    target = tests / "test_new_feature.py"
    target.write_text(source, encoding="utf-8")
    unrelated = tests / "test_domain.py"
    unrelated.write_text("from fieldkit import errors", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {target}
    assert unrelated not in result


def test_tool_references_through_test_helpers_are_selected(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    helper = tests / "new_helper.py"
    helper.write_text("import scripts.new_tool", encoding="utf-8")
    intermediary = tests / "another_helper.py"
    intermediary.write_text("from new_helper import run", encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("from another_helper import run", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {helper, intermediary, target}


def test_inventory_includes_tools_added_after_previous_collection(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    tests = tmp_path / "tests"
    tests.mkdir()
    target = tests / "test_future.py"
    target.write_text("import future_tool", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == set()
    (tmp_path / "scripts/future_tool.py").write_text("", encoding="utf-8")
    assert repo_tool_test_paths(tmp_path) == {target}


@pytest.mark.integration
@pytest.mark.parametrize("disable_tach", [True, False])
def test_native_pytest_selection(tmp_path: Path, disable_tach: bool) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_tools.py").write_text(
        'def test_hook():\n    path = "hooks/new_hook.py"\n    assert path\n', encoding="utf-8"
    )
    (tests / "test_domain.py").write_text("def test_domain():\n    assert False\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(_ROOT)}
    command = [sys.executable, "-m", "pytest", "tests", "-p", "scripts.repo_tool_selection"]
    if disable_tach:
        command.extend(["-p", "no:tach"])
    result = subprocess.run(
        [*command, "--repo-tools-only", "-q", "-o", "addopts="],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SELECTION_TIMEOUT_SECONDS,
    )

    assert result.returncode == (0 if disable_tach else 4), result.stdout + result.stderr
    if disable_tach:
        assert "1 passed, 1 deselected" in result.stdout
    else:
        assert "--repo-tools-only requires -p no:tach" in result.stderr


@pytest.mark.parametrize("package", ["helpers", "helpers/nested"])
def test_package_reexports_select_consumers(tmp_path: Path, package: str) -> None:
    tests = tmp_path / "tests"
    helpers = tests / package
    helpers.mkdir(parents=True)
    leaf = helpers / "runner.py"
    leaf.write_text("import scripts.new_tool", encoding="utf-8")
    initializer = helpers / "__init__.py"
    initializer.write_text("from .runner import run as execute", encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text(f"from tests.{package.replace('/', '.')} import execute", encoding="utf-8")
    unrelated = tests / "test_domain.py"
    unrelated.write_text("from fieldkit import errors", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {leaf, initializer, target}
    assert unrelated not in result


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("rename", [False, True])
def test_tool_backed_fixtures_select_consumers(tmp_path: Path, alias: bool, rename: bool) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    conftest = tests / "conftest.py"
    imported = "from scripts.foo import run as invoke" if alias else "import scripts.foo"
    call = "invoke()" if alias else "scripts.foo.run()"
    decorator = '@pytest.fixture(name="tool_runner")' if rename else "@pytest.fixture"
    fixture_name = "internal_runner" if rename else "tool_runner"
    conftest.write_text(
        f"import pytest\n{imported}\ndef helper():\n    return {call}\n"
        f"{decorator}\ndef {fixture_name}():\n    return helper()\n",
        encoding="utf-8",
    )
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")
    unrelated = tests / "test_domain.py"
    unrelated.write_text("def test_domain():\n    assert True", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {conftest, target}
    assert unrelated not in result


def test_selection_plugin_registration_does_not_select_unrelated_fixtures(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    conftest = tests / "conftest.py"
    conftest.write_text(
        'pytest_plugins = ("scripts.repo_tool_selection",)\ndef unrelated_fixture():\n    return "domain"\n',
        encoding="utf-8",
    )
    target = tests / "test_domain.py"
    target.write_text("def test_domain(unrelated_fixture):\n    assert unrelated_fixture", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {conftest}


def test_tool_backed_autouse_fixture_selects_only_its_subtree(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    nested = tests / "nested"
    nested.mkdir(parents=True)
    conftest = nested / "conftest.py"
    conftest.write_text(
        """import pytest
from scripts.foo import run
@pytest.fixture(autouse=True)
def setup_tool():
    run()
""",
        encoding="utf-8",
    )
    target = nested / "test_indirect.py"
    target.write_text("def test_tool():\n    assert True", encoding="utf-8")
    unrelated = tests / "test_domain.py"
    unrelated.write_text("def test_domain():\n    assert True", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {conftest, target}
    assert unrelated not in result


@pytest.mark.parametrize(
    "binding",
    [
        "from . import tool_helper as invoke",
        "import scripts.foo\ninvoke = scripts.foo.run",
        'import importlib\ninvoke = importlib.import_module("scripts.foo").run',
    ],
)
def test_fixture_helper_aliases_select_consumers(tmp_path: Path, binding: str) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    helper = tests / "tool_helper.py"
    helper.write_text("import scripts.foo", encoding="utf-8")
    conftest = tests / "conftest.py"
    conftest.write_text(
        f"""import pytest
{binding}
@pytest.fixture
def tool_runner():
    return invoke()
""",
        encoding="utf-8",
    )
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {helper, conftest, target}


@pytest.mark.parametrize("package", [False, True])
@pytest.mark.parametrize("nested_registration", [False, True])
def test_registered_fixture_plugins_select_consumers(tmp_path: Path, package: bool, nested_registration: bool) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    plugin = tests / "fixture_plugin.py"
    if package:
        plugin = tests / "fixture_plugin" / "__init__.py"
        plugin.parent.mkdir()
    plugin.write_text(
        """import pytest
from scripts.foo import run as invoke
@pytest.fixture(name="tool_runner")
def internal_runner():
    return invoke()
""",
        encoding="utf-8",
    )
    conftest = tests / "conftest.py"
    registration = conftest
    if nested_registration:
        registration = tests / "registration.py"
        conftest.write_text('pytest_plugins = "tests.registration"', encoding="utf-8")
    registration.write_text('pytest_plugins = ("tests.fixture_plugin",)', encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")
    unrelated = tests / "test_domain.py"
    unrelated.write_text("def test_domain():\n    assert True", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {plugin, registration, conftest, target}
    assert unrelated not in result


def test_registered_autouse_plugin_applies_outside_plugin_directory(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    helpers = tests / "helpers"
    helpers.mkdir(parents=True)
    plugin = helpers / "fixture_plugin.py"
    plugin.write_text(
        """import pytest
import scripts.foo
@pytest.fixture(autouse=True)
def setup_tool():
    scripts.foo.run()
""",
        encoding="utf-8",
    )
    conftest = tests / "conftest.py"
    conftest.write_text('pytest_plugins = ["tests.helpers.fixture_plugin"]', encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("def test_tool():\n    assert True", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == {plugin, conftest, target}


@pytest.mark.parametrize(
    "registration",
    [
        'pytest_plugins = "fixture_plugin,empty_plugin"',
        'PLUGINS = ["fixture_plugin"]; pytest_plugins = PLUGINS',
        'BASE = ("fixture_plugin",); PLUGINS = BASE; pytest_plugins = PLUGINS',
    ],
)
@pytest.mark.parametrize("location", ["src", "tests", "."])
def test_plugin_registration_aliases_and_import_roots(tmp_path: Path, registration: str, location: str) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    plugin_dir = tmp_path / location
    plugin_dir.mkdir(exist_ok=True)
    plugin = plugin_dir / "fixture_plugin.py"
    plugin.write_text(
        """import pytest
import scripts.foo
@pytest.fixture
def tool_runner():
    return scripts.foo.run()
""",
        encoding="utf-8",
    )
    conftest = tests / "conftest.py"
    conftest.write_text(registration, encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == {plugin, conftest, target}


@pytest.mark.parametrize(
    "registration",
    [
        'pytest_plugins = []; pytest_plugins += ["tests.fixture_plugin"]',
        'pytest_plugins = []; pytest_plugins.append("tests.fixture_plugin")',
        'pytest_plugins = []; pytest_plugins.extend(["tests.fixture_plugin"])',
    ],
)
def test_test_module_plugin_registration_and_star_exports(tmp_path: Path, registration: str) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    registration_module = tests / "test_registration.py"
    registration_module.write_text(registration, encoding="utf-8")
    plugin = tests / "fixture_plugin.py"
    plugin.write_text('pytest_plugins = "tests.fixture_plugin"\nfrom .tool_helper import *', encoding="utf-8")
    helper = tests / "tool_helper.py"
    helper.write_text(
        """import pytest
import scripts.foo
@pytest.fixture
def tool_runner():
    return scripts.foo.run()
""",
        encoding="utf-8",
    )
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")
    unrelated = tests / "test_domain.py"
    unrelated.write_text("def test_domain():\n    assert True", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == {registration_module, plugin, helper, target}


@pytest.mark.parametrize("location", ["src", "tests"])
@pytest.mark.parametrize("relative", [False, True])
def test_plugin_package_reexports_fixture_from_relative_module(tmp_path: Path, location: str, relative: bool) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    package = tmp_path / location / "fixture_package"
    package.mkdir(parents=True)
    plugin = package / "__init__.py"
    imported = (
        ".fixtures"
        if relative
        else ("tests.fixture_package.fixtures" if location == "tests" else "fixture_package.fixtures")
    )
    plugin.write_text(f"from {imported} import *", encoding="utf-8")
    helper = package / "fixtures.py"
    helper.write_text(
        """import pytest
import scripts.foo
@pytest.fixture
def tool_runner():
    return scripts.foo.run()
""",
        encoding="utf-8",
    )
    module = "tests.fixture_package" if location == "tests" else "fixture_package"
    conftest = tests / "conftest.py"
    conftest.write_text(f'pytest_plugins = "{module}"', encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == {plugin, helper, conftest, target}


@pytest.mark.parametrize(
    "registration",
    [
        'pytest_plugins = ["tests.first"]; pytest_plugins = pytest_plugins + ["tests.second"]',
        'PLUGINS = ["tests.first"]; pytest_plugins = PLUGINS; PLUGINS = ["tests.second"]',
    ],
)
def test_plugin_alias_rebindings_preserve_prior_registrations(tmp_path: Path, registration: str) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    plugin = tests / "first.py"
    plugin.write_text(
        """import pytest
import scripts.foo
@pytest.fixture
def tool_runner():
    return scripts.foo.run()
""",
        encoding="utf-8",
    )
    conftest = tests / "conftest.py"
    conftest.write_text(registration, encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == {plugin, conftest, target}


@pytest.mark.parametrize(
    "binding",
    ["from fixture_package import helpers", "import fixture_package.helpers as helpers"],
)
def test_plugin_imported_member_modules_select_fixture_consumers(tmp_path: Path, binding: str) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    package = tmp_path / "src" / "fixture_package"
    package.mkdir(parents=True)
    initializer = package / "__init__.py"
    initializer.write_text("", encoding="utf-8")
    plugin = package / "plugin.py"
    plugin.write_text(
        f"""import pytest
{binding}
@pytest.fixture
def tool_runner():
    return helpers.invoke()
""",
        encoding="utf-8",
    )
    helper = package / "helpers.py"
    helper.write_text("import scripts.foo\ndef invoke():\n    return scripts.foo.run()", encoding="utf-8")
    conftest = tests / "conftest.py"
    conftest.write_text('pytest_plugins = "fixture_package.plugin"', encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("def test_tool(tool_runner):\n    assert tool_runner", encoding="utf-8")

    assert {plugin, helper, conftest, target} <= repo_tool_test_paths(tmp_path)


def test_conftest_production_dependencies_remain_tach_owned(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    package = tmp_path / "src" / "fieldkit"
    package.mkdir(parents=True)
    (package / "domain.py").write_text(
        '"""Documentation mentions scripts."""\ndef client():\n    return True', encoding="utf-8"
    )
    (tests / "conftest.py").write_text("import fieldkit.domain", encoding="utf-8")
    (tests / "test_domain.py").write_text("def test_domain(client):\n    assert client", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == set()


@pytest.mark.parametrize("hook", ["pytest_runtest_setup", "pytest_pyfunc_call", "pytest_fixture_setup"])
@pytest.mark.parametrize("plugin", [False, True])
def test_tool_backed_pytest_hooks_retain_applicable_tests(tmp_path: Path, hook: str, plugin: bool) -> None:
    tests = tmp_path / "tests"
    nested = tests / "nested"
    nested.mkdir(parents=True)
    conftest = nested / "conftest.py"
    source = conftest
    if plugin:
        source = tests / "fixture_plugin.py"
        conftest.write_text('pytest_plugins = "tests.fixture_plugin"', encoding="utf-8")
    source.write_text(
        f"import scripts.foo\ndef {hook}(item):\n    if item.get_closest_marker('tool'):\n        scripts.foo.run()",
        encoding="utf-8",
    )
    target = nested / "test_indirect.py"
    target.write_text(
        """import pytest
@pytest.mark.tool
def test_tool():
    assert True
""",
        encoding="utf-8",
    )
    sibling = tests / "test_domain.py"
    sibling.write_text("def test_domain():\n    assert True", encoding="utf-8")

    expected = {source, conftest, target, sibling} if plugin else {conftest, target}
    assert repo_tool_test_paths(tmp_path) == expected
