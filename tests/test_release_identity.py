"""Stable successor identities shared by export, bundle and governance checks."""

import pytest

from scripts import _release_identity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("tag", ["v1.0.0", "v1.0.1", "v1.10.20", "v2.0.0"])
def test_canonical_tags_have_exact_versions(tag: str) -> None:
    assert _release_identity.tag_version(tag) == tag[1:]


@pytest.mark.parametrize(
    "tag", [None, 101, "v01.0.1", "v1.00.1", "v1.0.01", "v1.0.1rc1", "v1.0.1+local", " v1.0.1", "v1.0.1\n"]
)
def test_noncanonical_tags_fail_closed(tag: object) -> None:
    with pytest.raises(ValueError, match="canonical stable SemVer"):
        _release_identity.tag_version(tag)


@pytest.mark.parametrize(
    ("name", "kind"),
    [("fieldkit_cli-1.0.1-py3-none-any.whl", "wheel"), ("fieldkit_cli-1.0.1.tar.gz", "sdist")],
)
def test_artifact_identity_accepts_exact_successor(name: str, kind: str) -> None:
    assert _release_identity.validate_artifact_identity(name, kind, "fieldkit-cli", "v1.0.1") is None


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("fieldkit_cli-1.0.0-py3-none-any.whl", "wheel"),
        ("fieldkit_cli-1.0.10-py3-none-any.whl", "wheel"),
        ("fieldkit_cli-1.0.1rc1-py3-none-any.whl", "wheel"),
        ("other-1.0.1-py3-none-any.whl", "wheel"),
        ("fieldkit_cli-1.0.0.tar.gz", "sdist"),
    ],
)
def test_stale_or_foreign_artifacts_fail_closed(name: str, kind: str) -> None:
    with pytest.raises(ValueError, match="artifact filename does not bind"):
        _release_identity.validate_artifact_identity(name, kind, "fieldkit-cli", "v1.0.1")


@pytest.mark.parametrize(
    "schema_name",
    ["public-tree-policy", "public-tree-manifest", "release-bundle-provenance", "release-consumer-evidence"],
)
@pytest.mark.parametrize("tag", ["v1.0.0", "v1.0.1", "v2.0.0", "v01.0.1", "v1.0.1rc1", "v1.0.1+local", "v1.0.1\n"])
def test_tag_schemas_agree_with_runtime(schema_name: str, tag: str) -> None:
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    path = Path(__file__).parents[1] / "docs/release-readiness" / f"{schema_name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))["properties"]["planned_tag"]
    errors = list(Draft202012Validator(schema).iter_errors(tag))
    try:
        version = _release_identity.tag_version(tag)
    except ValueError:
        assert errors
    else:
        assert version == tag[1:]
        assert errors == []
