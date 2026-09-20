"""Tests for Spec 046: MEMORY.md link checker (implementation note).

scripts/check_memory_links.py is importable via sys.path (conftest.py adds
scripts/ to sys.path before tests run).
"""

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# check_memory_links() — core function
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_link_checker_detects_broken_link(tmp_path: Path) -> None:
    """check_memory_links() returns an entry for each broken relative link."""
    from check_memory_links import check_memory_links

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "exists.md").write_text("# Exists\n", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text(
        "# Memory\n\n[Exists](exists.md)\n[Missing](missing.md)\n",
        encoding="utf-8",
    )

    broken = check_memory_links(memory_dir)

    assert len(broken) == 1, f"Expected 1 broken link, got {len(broken)}: {broken}"
    assert "missing.md" in broken[0]


@pytest.mark.unit
def test_memory_link_checker_no_broken_links(tmp_path: Path) -> None:
    """check_memory_links() returns empty list when all relative links resolve."""
    from check_memory_links import check_memory_links

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "ref.md").write_text("# Ref\n", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text(
        "# Memory\n\n[Ref](ref.md)\n[External](https://example.com)\n",
        encoding="utf-8",
    )

    broken = check_memory_links(memory_dir)

    assert broken == []


@pytest.mark.unit
def test_memory_link_checker_skips_external_links(tmp_path: Path) -> None:
    """check_memory_links() ignores http/https links regardless of existence."""
    from check_memory_links import check_memory_links

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "# Memory\n\n[Google](https://google.com)\n[Local](http://localhost:8080)\n[Anchor](#section)\n",
        encoding="utf-8",
    )

    broken = check_memory_links(memory_dir)

    assert broken == [], f"External/anchor links should be skipped, got: {broken}"


@pytest.mark.unit
def test_memory_link_checker_missing_memory_md(tmp_path: Path) -> None:
    """check_memory_links() returns empty list when MEMORY.md does not exist."""
    from check_memory_links import check_memory_links

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    # No MEMORY.md created

    broken = check_memory_links(memory_dir)

    assert broken == []


@pytest.mark.unit
def test_memory_link_checker_multiple_broken_links(tmp_path: Path) -> None:
    """check_memory_links() reports all broken links, not just the first."""
    from check_memory_links import check_memory_links

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "# Memory\n\n[Missing A](user_nicknames.md)\n[Missing B](reference_sf_contact_roles.md)\n",
        encoding="utf-8",
    )

    broken = check_memory_links(memory_dir)

    assert len(broken) == 2, f"Expected 2 broken links, got {len(broken)}: {broken}"
    hrefs = " ".join(broken)
    assert "user_nicknames.md" in hrefs
    assert "reference_sf_contact_roles.md" in hrefs


@pytest.mark.unit
def test_memory_link_checker_flags_traversal_links(tmp_path: Path) -> None:
    """check_memory_links() flags links that escape the memory directory via ../."""
    from check_memory_links import check_memory_links

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "# Memory\n\n[Escape](../../etc/passwd)\n[Tools](../tools/contact-enrich/QUICKSTART.md)\n",
        encoding="utf-8",
    )

    broken = check_memory_links(memory_dir)

    assert len(broken) == 2, f"Expected 2 traversal findings, got {len(broken)}: {broken}"
    combined = " ".join(broken)
    assert "TRAVERSAL" in combined
    assert "etc/passwd" in combined or "tools" in combined


@pytest.mark.unit
def test_memory_link_checker_broken_link_message_format(tmp_path: Path) -> None:
    """Broken link descriptions include BROKEN prefix, link text, and href."""
    from check_memory_links import check_memory_links

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "[My Link](missing_file.md)\n",
        encoding="utf-8",
    )

    broken = check_memory_links(memory_dir)

    assert len(broken) == 1
    assert broken[0].startswith("BROKEN:")
    assert "[My Link]" in broken[0]
    assert "missing_file.md" in broken[0]
