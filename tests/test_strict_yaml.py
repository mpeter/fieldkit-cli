"""Fail-closed tests for the shared strict YAML parser."""

import traceback

import pytest

from fieldkit.util.strict_yaml import StrictYAMLError, load_strict_yaml

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("document", ["loop: &cycle [*cycle]\n", "outer: &outer [[*outer]]\n"])
def test_load_strict_yaml_rejects_cyclic_aliases_without_context(document: str) -> None:
    with pytest.raises(StrictYAMLError, match=r"^YAML document is invalid$") as caught:
        load_strict_yaml(document)

    assert caught.value.reason == "invalid_yaml"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "cycle" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("depth", [64, 65])
def test_load_strict_yaml_enforces_graph_depth_boundary(depth: int) -> None:
    document = "[" * depth + "]" * depth
    if depth == 65:
        with pytest.raises(StrictYAMLError, match=r"^YAML document is invalid$"):
            load_strict_yaml(document)
        return

    result = load_strict_yaml(document)

    assert isinstance(result, list)


@pytest.mark.parametrize("scalars", [9999, 10000])
def test_load_strict_yaml_enforces_graph_visit_boundary(scalars: int) -> None:
    document = "[" + ",".join("0" for _ in range(scalars)) + "]"
    if scalars == 10000:
        with pytest.raises(StrictYAMLError, match=r"^YAML document is invalid$"):
            load_strict_yaml(document)
        return

    result = load_strict_yaml(document)

    assert isinstance(result, list)
    assert len(result) == scalars


def test_load_strict_yaml_rejects_expanded_alias_visits() -> None:
    rows = ["node0: &node0 [fictional]"]
    rows.extend(f"node{i}: &node{i} [*node{i - 1}, *node{i - 1}]" for i in range(1, 13))

    with pytest.raises(StrictYAMLError, match=r"^YAML document is invalid$") as caught:
        load_strict_yaml("\n".join(rows))

    assert caught.value.reason == "invalid_yaml"


def test_load_strict_yaml_preserves_bounded_shared_alias_identity() -> None:
    result = load_strict_yaml("first: &shared [fictional]\nsecond: [*shared, *shared]\n")

    assert isinstance(result, dict)
    assert result == {"first": ["fictional"], "second": [["fictional"], ["fictional"]]}
    assert result["first"] is result["second"][0] is result["second"][1]


def test_load_strict_yaml_accepts_maximum_done_check_argument_structure() -> None:
    from fieldkit.driver.done_checks import ArgvCheck, parse_done_checks

    arguments = ", ".join(["pytest", *(f"item-{index}" for index in range(127))])
    records = "".join(f"    - id: check-{index}\n      argv: [{arguments}]\n" for index in range(64))
    document = "---\ndone_checks:\n  version: 1\n  checks:\n" + records + "---\n"

    result = parse_done_checks(document)

    assert len(result.checks) == 64
    assert all(isinstance(check, ArgvCheck) and len(check.argv) == 128 for check in result.checks)


def test_load_strict_yaml_preserves_safe_loader_values() -> None:
    result = load_strict_yaml("name: Example\nenabled: true\nitems: [one, two]\n")

    assert result == {"name": "Example", "enabled": True, "items": ["one", "two"]}


@pytest.mark.parametrize(
    "document",
    [
        "name: first\nname: second\n",
        "outer:\n  name: first\n  name: second\n",
        "defaults: &defaults\n  name: first\nselected:\n  <<: *defaults\n  name: second\n",
        "first: &first\n  name: first\nsecond: &second\n  name: second\nselected:\n  <<: [*first, *second]\n",
    ],
    ids=["top-level", "nested", "merge-override", "conflicting-merges"],
)
def test_load_strict_yaml_rejects_duplicate_keys_without_payload(document: str) -> None:
    with pytest.raises(StrictYAMLError) as caught:
        load_strict_yaml(document)

    assert caught.value.reason == "duplicate_key"
    assert str(caught.value) == "YAML document contains duplicate mapping keys"
    assert "first" not in str(caught.value)
    assert "second" not in str(caught.value)


def test_load_strict_yaml_allows_unambiguous_merge() -> None:
    result = load_strict_yaml("defaults: &defaults\n  enabled: true\nselected:\n  <<: *defaults\n")

    assert result == {"defaults": {"enabled": True}, "selected": {"enabled": True}}


@pytest.mark.parametrize("document", ["? [one, two]\n: value\n", "? {private: key}\n: value\n"])
def test_load_strict_yaml_rejects_non_scalar_mapping_keys(document: str) -> None:
    with pytest.raises(StrictYAMLError) as caught:
        load_strict_yaml(document)

    assert caught.value.reason == "non_scalar_key"
    assert str(caught.value) == "YAML mapping keys must be scalar values"
    assert "private" not in str(caught.value)


def test_load_strict_yaml_hides_invalid_source_payload() -> None:
    with pytest.raises(StrictYAMLError) as caught:
        load_strict_yaml("oauth_client_secret: [fictional-sensitive-token\n")

    assert caught.value.reason == "invalid_yaml"
    assert str(caught.value) == "YAML document is invalid"
    assert "fictional-sensitive-token" not in str(caught.value)


@pytest.mark.parametrize(
    ("document", "private_payload"),
    [
        ("[" * 2000 + "]" * 2000, "[[[["),
        ("value: " + "9" * 5000, "99999999"),
    ],
    ids=["recursive-collection-limit", "integer-conversion-limit"],
)
def test_load_strict_yaml_translates_parser_limits_without_payload(document: str, private_payload: str) -> None:
    with pytest.raises(StrictYAMLError) as caught:
        load_strict_yaml(document)

    assert caught.value.reason == "invalid_yaml"
    assert str(caught.value) == "YAML document is invalid"
    assert private_payload not in str(caught.value)
