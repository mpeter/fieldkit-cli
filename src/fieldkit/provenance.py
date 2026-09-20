"""Derived-doc provenance marker (implementation change).

Single home for the caste marker stamped onto every generated / summary
document, so a reader — agent or human — can tell derived exhaust from a
system of record without external context. The marker is plain frontmatter
plus a body banner, both inside the file content, so any byte-for-byte copy
or export carries it unchanged.

Vocabulary (see openspec/changes/derived-doc-exhaust-marker/design.md D2):
  - ``derived``: mechanically regenerable from committed repo state.
  - ``summary``: pipeline-emitted rollup over runtime data.
Systems of record carry no ``caste`` key at all.

Placement: top-level single-file module (same pattern as circuit_breaker.py)
because both ``scripts/`` generators and ``fieldkit.commands`` writers import
it; rendering goes through the sanctioned ``render_raw_key_value()`` /
``_yaml_scalar()`` path so a hostile source ref cannot inject a YAML document
separator (historic regression class).
"""

from collections.abc import Sequence
from typing import Literal

from fieldkit.pursuit.io import render_raw_key_value

DerivedCaste = Literal["derived", "summary"]

_BANNER_TEXT = (
    "> **Derived document — generated exhaust, not a system of record.** "
    "Verify facts against the sources listed in `derived_from`."
)


def derived_doc_marker(*, caste: DerivedCaste, derived_from: Sequence[str], generated_by: str) -> str:
    """Render the provenance frontmatter block for a derived doc.

    Returns the full ``---``-delimited block, ending with a trailing newline,
    ready to prepend to the document body.

    Args:
        caste: ``"derived"`` (regenerable from repo state) or ``"summary"``
            (point-in-time rollup over runtime data).
        derived_from: Source refs the doc summarizes — repo-relative paths or
            stable command/system refs. Must be non-empty: a caste marker
            without sources defeats the point (issue #1275 acceptance).
        generated_by: The exact regeneration entry point (script or CLI
            command) — doubles as the "how do I refresh this" pointer.

    Raises:
        ValueError: derived_from is empty.
    """
    if not derived_from:
        raise ValueError("derived_doc_marker: derived_from must name at least one source")
    lines: list[str] = []
    lines.extend(render_raw_key_value("caste", caste))
    lines.extend(render_raw_key_value("derived_from", list(derived_from)))
    lines.extend(render_raw_key_value("generated_by", generated_by))
    return "---\n" + "\n".join(lines) + "\n---\n"


def derived_doc_banner() -> str:
    """Return the human-readable exhaust banner blockquote (no trailing newline).

    Placed in the document body so the caste stays visible in renderers that
    hide frontmatter.
    """
    return _BANNER_TEXT
