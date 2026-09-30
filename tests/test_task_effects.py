"""Task provenance binds source positions and decisions without binding user edits."""

from dataclasses import replace

import pytest

from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.tasks.effects import TaskEffect, prepare_effect, read_task_markers
from fieldkit.util.owned_markdown import OwnedMarker

pytestmark = pytest.mark.unit


def _effect() -> TaskEffect:
    return TaskEffect("source-1", 0, ClassifiedItem("Send proposal", ItemClass.MY_TASK, "Alice", "Assigned", "Acme"))


def test_marker_round_trip_and_user_edits_preserve_identity() -> None:
    prepared = prepare_effect(_effect(), meeting_title="Meeting", meeting_date="2026-09-27")
    markers = read_task_markers("## Active\n" + prepared.line + "\n")
    assert markers == {prepared.identity: OwnedMarker(prepared.fingerprint, "Active")}
    edited = prepared.line.replace("- **[Acme]** Send proposal", "- [x] Sent revised proposal")
    assert read_task_markers("## Active\n" + edited) == markers


@pytest.mark.parametrize("field", ["text", "cls", "pursuit_label"])
def test_changed_decision_keeps_identity_but_changes_fingerprint(field: str) -> None:
    effect = _effect()
    changed = replace(effect.item, **{field: ItemClass.WAITING_ON if field == "cls" else "Changed"})
    original = prepare_effect(effect, meeting_title="Meeting", meeting_date="2026-09-27")
    revised = prepare_effect(replace(effect, item=changed), meeting_title="Meeting", meeting_date="2026-09-27")
    assert revised.identity == original.identity
    assert revised.fingerprint != original.fingerprint


def test_source_and_position_distinguish_identical_tasks() -> None:
    effect = _effect()
    original = prepare_effect(effect, meeting_title="", meeting_date="")
    other_source = prepare_effect(replace(effect, source_id="source-2"), meeting_title="", meeting_date="")
    other_position = prepare_effect(replace(effect, position=1), meeting_title="", meeting_date="")
    assert len({original.identity, other_source.identity, other_position.identity}) == 3


def test_ambient_source_identity_round_trip() -> None:
    effect = replace(_effect(), source_id="ambient:" + "a" * 64)
    prepared = prepare_effect(effect, meeting_title="Meeting", meeting_date="2026-09-27")
    assert read_task_markers("## Active\n" + prepared.line) == {
        prepared.identity: OwnedMarker(prepared.fingerprint, "Active")
    }


@pytest.mark.parametrize("field", ["meeting_title", "meeting_date"])
def test_waiting_on_context_is_bound(field: str) -> None:
    effect = replace(_effect(), item=replace(_effect().item, cls=ItemClass.WAITING_ON))
    original = prepare_effect(effect, meeting_title="Meeting", meeting_date="2026-09-27")
    context = {"meeting_title": "Meeting", "meeting_date": "2026-09-27"}
    context[field] = "Changed"
    revised = prepare_effect(effect, **context)
    assert revised.identity == original.identity
    assert revised.fingerprint != original.fingerprint


def test_active_owner_context_and_rationale_do_not_change_rendered_meaning() -> None:
    effect = _effect()
    original = prepare_effect(effect, meeting_title="Meeting", meeting_date="2026-09-27")
    revised = prepare_effect(
        replace(effect, item=replace(effect.item, owner="", rationale="Confirmed")),
        meeting_title="Other",
        meeting_date="",
    )
    assert revised == original


def test_waiting_on_owner_changes_saved_meaning() -> None:
    effect = replace(_effect(), item=replace(_effect().item, cls=ItemClass.WAITING_ON))
    original = prepare_effect(effect, meeting_title="Meeting", meeting_date="2026-09-27")
    revised = prepare_effect(
        replace(effect, item=replace(effect.item, owner="Bob")), meeting_title="Meeting", meeting_date="2026-09-27"
    )
    assert revised.identity == original.identity
    assert revised.fingerprint != original.fingerprint


@pytest.mark.parametrize("position", [-1, True, 2000])
def test_invalid_original_position_is_rejected(position: int) -> None:
    with pytest.raises(ValueError, match="Invalid task position"):
        prepare_effect(replace(_effect(), position=position), meeting_title="", meeting_date="")


@pytest.mark.parametrize("value", ["", "../source", "private\nsource", "s" * 257])
def test_invalid_source_identity_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid task source identity"):
        prepare_effect(replace(_effect(), source_id=value), meeting_title="", meeting_date="")


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\x00b", "a\x1bb", "<!-- forged -->", "fieldkit-task:v2:bad"])
@pytest.mark.parametrize("field", ["text", "owner", "pursuit_label", "meeting_title", "meeting_date"])
def test_rendered_fields_cannot_inject_structure(field: str, value: str) -> None:
    effect = _effect()
    context = {"meeting_title": "Meeting", "meeting_date": "2026-09-27"}
    if field in context:
        context[field] = value
    else:
        effect = replace(effect, item=replace(effect.item, **{field: value}))
    with pytest.raises(ValueError, match="Invalid task rendering input"):
        prepare_effect(effect, **context)


@pytest.mark.parametrize(
    "mutation", ["duplicate", "orphan", "version", "malformed", "suffix", "double", "blank", "checkbox"]
)
def test_ambiguous_markers_fail_closed(mutation: str) -> None:
    prepared = prepare_effect(_effect(), meeting_title="", meeting_date="")
    line = prepared.line
    marker = line[line.index("<!--") :]
    content = {
        "duplicate": line + "\n" + line,
        "orphan": marker,
        "version": line.replace(":v1:", ":v2:"),
        "malformed": line.replace(prepared.identity, "bad"),
        "suffix": line + " trailing",
        "double": line + " " + marker,
        "blank": "-  " + marker,
        "checkbox": "- [x] " + marker,
    }[mutation]
    with pytest.raises(ValueError, match="Invalid or ambiguous task provenance"):
        read_task_markers("## Active\n" + content)


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_code_example_cannot_claim_task_ownership(fence: str) -> None:
    prepared = prepare_effect(_effect(), meeting_title="", meeting_date="")
    with pytest.raises(ValueError, match="Invalid or ambiguous task provenance"):
        read_task_markers(f"## Active\n{fence}\n{prepared.line}\n{fence}\n")


def test_indented_code_cannot_claim_task_ownership() -> None:
    prepared = prepare_effect(_effect(), meeting_title="", meeting_date="")
    with pytest.raises(ValueError, match="Invalid or ambiguous task provenance"):
        read_task_markers("## Active\n\n    " + prepared.line)


@pytest.mark.parametrize("opening,closing", [("<!--", "-->"), ("<pre>", "</pre>"), ("<div>", "</div>")])
def test_hidden_html_cannot_claim_task_ownership(opening: str, closing: str) -> None:
    prepared = prepare_effect(_effect(), meeting_title="", meeting_date="")
    with pytest.raises(ValueError, match="Invalid or ambiguous task provenance"):
        read_task_markers(f"## Active\n{opening}\n{prepared.line}\n{closing}\n")


@pytest.mark.parametrize("prefix", ["", "## Other\n", "## Active\n# Other\n", "## Active\n## Other\n"])
def test_marker_outside_owned_section_is_rejected(prefix: str) -> None:
    prepared = prepare_effect(_effect(), meeting_title="", meeting_date="")
    with pytest.raises(ValueError, match="Invalid or ambiguous task provenance"):
        read_task_markers(prefix + prepared.line)


@pytest.mark.parametrize("mutation", ["inline", "escaped", "continuation", "nested"])
def test_marker_must_be_a_real_first_line_html_token(mutation: str) -> None:
    prepared = prepare_effect(_effect(), meeting_title="", meeting_date="")
    line = prepared.line
    marker = line[line.index("<!--") :]
    content = {
        "inline": line.replace(marker, f"`{marker}`"),
        "escaped": line.replace("<!--", r"\<!--"),
        "continuation": "- Task\n  " + marker,
        "nested": "- Parent\n  " + line,
    }[mutation]
    with pytest.raises(ValueError, match="Invalid or ambiguous task provenance"):
        read_task_markers("## Active\n" + content)


def test_ordinary_markdown_without_provenance_is_ignored() -> None:
    assert read_task_markers("## Active\n<!-- instructions -->\n- Unrelated task\n") == {}


@pytest.mark.parametrize("heading", ["## Active\n", "Active\n------\n", "## Active\n### Today\n"])
@pytest.mark.parametrize("bullet", ["-", "*", "+"])
def test_semantic_section_and_bullet_edits_preserve_provenance(heading: str, bullet: str) -> None:
    prepared = prepare_effect(_effect(), meeting_title="", meeting_date="")
    content = heading + bullet + prepared.line[1:]
    assert read_task_markers(content) == {prepared.identity: OwnedMarker(prepared.fingerprint, "Active")}
