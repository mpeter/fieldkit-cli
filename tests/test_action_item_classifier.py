"""Tests for lib.action_item_classifier."""

import pytest

from fieldkit.tasks.classifier import (
    ClassifiedItem,
    ItemClass,
    classify_action_item,
    classify_action_items,
)

pytestmark = pytest.mark.unit

USER_NAME = "Test AE"
USER_EMAIL = "testae@example.com"  # pii-guard: ignore
STAKEHOLDERS = ["Alice Customer", "Bob Stakeholder", "Carol Smith", "Carol"]
INTERNAL_TEAM = ["Dave Rh", "Eve Rh", "Frank Rh", "Grace Rh"]
LABEL = "<account> / RHOAI"


def classify(item: str) -> ClassifiedItem:
    return classify_action_item(
        item,
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )


# ---------------------------------------------------------------------------
# MY_TASK cases
# ---------------------------------------------------------------------------


# ── TestMyTask (flattened) ──────────────────────────────────────────────────


def test_my_task_full_name_prefix():
    r = classify("Test AE: Draft SOW for next 6-12 months")
    assert r.cls == ItemClass.MY_TASK
    assert r.owner == "Test AE"


def test_my_task_first_name_only():
    r = classify("Test to confirm MVP1 scope with Dave")
    assert r.cls == ItemClass.MY_TASK


def test_my_task_email_local_part():
    # local part of email derived from USER_EMAIL
    r = classify("testae: send calendar invite to Alice")
    assert r.cls == ItemClass.MY_TASK


def test_my_task_action_label_format():
    r = classify("Action: Test AE — schedule follow-up with Alice")
    assert r.cls == ItemClass.MY_TASK


# ---------------------------------------------------------------------------
# WAITING_ON cases
# ---------------------------------------------------------------------------


# ── TestWaitingOn (flattened) ───────────────────────────────────────────────


def test_waiting_on_customer_stakeholder_with_approval():
    r = classify("Alice Customer: confirm budget approval with Finance team")
    assert r.cls == ItemClass.WAITING_ON
    assert r.owner == "Alice Customer"


def test_waiting_on_customer_with_scheduling():
    r = classify("Bob Stakeholder to schedule the MVP1 planning meeting")
    assert r.cls == ItemClass.WAITING_ON


def test_waiting_on_customer_with_docusign():
    r = classify("Carol Smith: submit DocuSign for Phase 3 SOW")
    assert r.cls == ItemClass.WAITING_ON


def test_waiting_on_customer_no_keyword_drops():
    # Customer owner with no gating keyword → DROP (delivery noise)
    r = classify("Carol: think about the timeline")
    assert r.cls == ItemClass.DROP


def test_waiting_on_rh_member_involving_customer_and_gate():
    # Dave (RH) + Alice (customer) + high-confidence keyword → WAITING_ON
    r = classify("Dave Rh: Follow up with Alice on the SOW approval by Friday")
    assert r.cls == ItemClass.WAITING_ON


def test_waiting_on_rh_member_involving_customer_no_high_confidence_drops():
    # Dave (RH) + Alice (customer) but only scheduling keyword → DROP
    r = classify("Dave Rh: Follow up with Alice on Friday meeting to plan production transition timeline")
    assert r.cls == ItemClass.DROP


def test_waiting_on_unknown_owner_with_urgency():
    r = classify("Confirm PO submission before July 1 deadline — critical")
    assert r.cls == ItemClass.WAITING_ON


def test_waiting_on_unknown_owner_with_contract_keyword():
    r = classify("Get signed SOW back from customer before close date")
    assert r.cls == ItemClass.WAITING_ON


def test_waiting_on_first_name_stakeholder_match():
    # "Carol" is in stakeholder list
    r = classify("Carol: coordinate PO submission with procurement")
    assert r.cls == ItemClass.WAITING_ON


# ---------------------------------------------------------------------------
# DROP cases
# ---------------------------------------------------------------------------


# ── TestDrop (flattened) ────────────────────────────────────────────────────


def test_drop_jira_team():
    r = classify("Team: Put tasks in Jira assigned to CA and Val for tracking")
    assert r.cls == ItemClass.DROP


def test_drop_laptop_personal():
    r = classify("Eve Rh: Contact Apple Care/Genius Bar for laptop issues")
    assert r.cls == ItemClass.DROP


def test_drop_rh_internal_standup():
    r = classify("Dave Rh: Update the Jira board with sprint tasks")
    assert r.cls == ItemClass.DROP


def test_drop_rh_code_review():
    r = classify("Frank Rh: Review the pull request for the RHOAI config")
    assert r.cls == ItemClass.DROP


def test_drop_team_generic_no_gate():
    r = classify("Team: Set up daily calls")
    assert r.cls == ItemClass.DROP


def test_drop_team_slack_channel():
    r = classify("Team: Post update in the Slack channel")
    assert r.cls == ItemClass.DROP


def test_drop_personal_item():
    r = classify("Andrew Sheet: Check with wife on hospital availability")
    assert r.cls == ItemClass.DROP


# ---------------------------------------------------------------------------
# Bulk classification
# ---------------------------------------------------------------------------


# ── TestBulkClassify (flattened) ────────────────────────────────────────────


def test_classify_ae_owner_empty_input_returns_empty_list() -> None:
    """Empty input list returns an empty result list."""
    results = classify_action_items(
        [],
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert results == []


def test_classify_ae_owner_mixed_items() -> None:
    """Mixed items are each classified to the correct ItemClass."""
    items = [
        "Test AE: Draft SOW for next 6-12 months",
        "Alice Customer: confirm extension with Finance team by Wednesday",
        "Team: Put tasks in Jira assigned to CA and Val",
        "Eve Rh: Contact Genius Bar for laptop",
        "Dave Rh: Follow up with Alice on Friday planning meeting for RoAI production timeline",
    ]
    results = classify_action_items(
        items,
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )

    def cls_for(prefix: str) -> ItemClass:
        for r in results:
            if r.text.startswith(prefix):
                return r.cls
        raise KeyError(f"No result starting with: {prefix!r}")

    assert cls_for("Test AE:") == ItemClass.MY_TASK
    assert cls_for("Alice Customer:") == ItemClass.WAITING_ON
    assert cls_for("Team: Put tasks in Jira") == ItemClass.DROP
    assert cls_for("Eve Rh:") == ItemClass.DROP
    assert cls_for("Dave Rh:") == ItemClass.DROP


def test_classify_ae_owner_all_drop_when_no_user_or_customers() -> None:
    """Items with no matching user or customer names are classified DROP."""
    items = ["Team: do standup", "Dave: update Jira"]
    results = classify_action_items(
        items,
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=["Dave Rh"],
        pursuit_label=LABEL,
    )
    assert all(r.cls == ItemClass.DROP for r in results)


def test_classify_ae_owner_pursuit_label_propagated() -> None:
    """The pursuit_label argument is propagated to each classified result."""
    results = classify_action_items(
        ["Test AE: send the proposal"],
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label="<account> / EDA",
    )
    assert results[0].pursuit_label == "<account> / EDA"


# ---------------------------------------------------------------------------
# Task 11.9 — boundary edge-case tests for classify_action_item
# ---------------------------------------------------------------------------


# ── TestClassifyBoundaryEdgeCases (flattened) ───────────────────────────────


def test_classify_ae_owner_empty_item_text_drops():
    """An empty action item string has no owner and no keywords → DROP."""
    r = classify_action_item(
        "",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.DROP


def test_classify_ae_owner_high_confidence_keyword_with_unknown_owner_is_waiting_on():
    """Unknown owner + high-confidence keyword (e.g. 'purchase order') → WAITING_ON."""
    # Use a text where no owner prefix is extractable (starts with lowercase verb)
    r = classify_action_item(
        "purchase order must be submitted before end of quarter",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label=LABEL,
    )
    # "purchase order" is a high-confidence deal keyword; unknown owner → WAITING_ON
    assert r.cls == ItemClass.WAITING_ON


# ---------------------------------------------------------------------------
# Additional boundary tests — targeting CRAP-score reduction on classify_action_item
# ---------------------------------------------------------------------------


# ── TestClassifyAdditionalBoundaries (flattened) ────────────────────────────


def test_classify_ae_owner_unknown_owner_with_internal_mechanics_drops():
    """Unknown owner + internal mechanics keyword → DROP (Rule 2)."""
    r = classify_action_item(
        "Update the Jira board with sprint tasks for next week",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.DROP


def test_classify_ae_owner_team_prefix_with_internal_mechanics_drops():
    """Team: prefix + internal mechanics → DROP (Rule 2)."""
    r = classify_action_item(
        "Team: review the pull request for the RHOAI config",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.DROP


def test_classify_ae_owner_rh_team_member_with_internal_mechanics_drops():
    """Internal team member + internal mechanics → DROP (Rule 3)."""
    r = classify_action_item(
        "Dave Rh: merge the branch and run CI/CD pipeline",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.DROP
    assert r.owner == "Dave Rh"


def test_classify_ae_owner_customer_with_broad_and_date_is_waiting_on():
    """Customer stakeholder + broad keyword + explicit date → WAITING_ON (Rule 4 broad+date)."""
    r = classify_action_item(
        "Alice Customer: send the updated requirements by Wednesday",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.WAITING_ON
    assert r.owner == "Alice Customer"


def test_classify_ae_owner_customer_with_broad_and_urgency_is_waiting_on():
    """Customer stakeholder + broad keyword + urgency marker → WAITING_ON (Rule 4 broad+urgency)."""
    r = classify_action_item(
        "Bob Stakeholder: provide the integration specs — urgent",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.WAITING_ON


def test_classify_ae_owner_customer_with_only_broad_no_urgency_drops():
    """Customer stakeholder + no gating keyword → DROP (Rule 4 fallthrough).

    Uses Carol Smith (no 'cu' substring) and text with no high-confidence or
    broad+urgency signal so the fallthrough DROP branch is exercised.
    """
    r = classify_action_item(
        "Carol Smith: think about the overall plan",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.DROP


def test_classify_ae_owner_unknown_owner_with_urgency_keyword_is_waiting_on():
    """Unknown owner + high-confidence keyword → WAITING_ON (Rule 5).

    Uses a text starting with a verb (no owner prefix extractable → owner=Unknown)
    and includes a high-confidence deal keyword to trigger Rule 5.
    """
    r = classify_action_item(
        "sign the NDA before the kickoff meeting",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label=LABEL,
    )
    # 'sign' and 'nda' are high-confidence deal keywords; owner=Unknown → WAITING_ON (Rule 5)
    assert r.cls == ItemClass.WAITING_ON


def test_classify_ae_owner_rh_team_with_customer_and_high_confidence_is_waiting_on():
    """Internal team member + customer named + high-confidence keyword → WAITING_ON (Rule 6)."""
    r = classify_action_item(
        "Eve Rh: follow up with Alice on the contract signature",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.WAITING_ON


def test_classify_ae_owner_rh_team_without_customer_drops():
    """Internal team member with no customer named → DROP (Rule 6 fallthrough)."""
    r = classify_action_item(
        "Grace Rh: update the deployment runbook for the cluster",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.DROP


def test_classify_ae_owner_no_owner_no_keyword_drops():
    """No recognisable owner and no gating keyword → DROP (Rule 7)."""
    r = classify_action_item(
        "Think about the overall timeline for the project",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.DROP


def test_classify_ae_owner_pursuit_label_propagated_to_classified_item():
    """pursuit_label is stored verbatim on the ClassifiedItem."""
    r = classify_action_item(
        "Test AE: send the proposal",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label="Acme Corp / RHOAI",
    )
    assert r.pursuit_label == "Acme Corp / RHOAI"


def test_classify_ae_owner_financial_keyword_with_unknown_owner_is_waiting_on():
    """Financial keyword (budget) + unknown owner → WAITING_ON (Rule 5 high-confidence)."""
    r = classify_action_item(
        "budget approval is needed before we can proceed",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=[],
        internal_team_names=[],
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.WAITING_ON


def test_classify_ae_owner_ae_email_local_part_matches_my_task():
    """AE identified by email local part → MY_TASK (Rule 1)."""
    r = classify_action_item(
        "testae to send the updated pricing sheet",
        user_name=USER_NAME,
        user_email=USER_EMAIL,
        stakeholder_names=STAKEHOLDERS,
        internal_team_names=INTERNAL_TEAM,
        pursuit_label=LABEL,
    )
    assert r.cls == ItemClass.MY_TASK
