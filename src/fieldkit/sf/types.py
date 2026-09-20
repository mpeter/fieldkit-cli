"""TypedDict definitions for Salesforce API response types.

These replace ``dict[str, Any]`` return types in ``sf/client.py``
so that mypy can catch key-access mismatches at type-check time.
"""

from dataclasses import dataclass
from typing import Any, Literal, TypedDict


class OpportunityRecord(TypedDict, total=False):
    """Typed dict for a Salesforce Opportunity in the listview-compatible format.

    Keys match the dict returned by ``SalesforceClient.search_opportunities()``.
    All fields are optional (``total=False``) and may be ``None`` when the
    Salesforce record does not carry that field.
    """

    opportunity_id: str | None
    name: str | None
    stage: str | None
    close_date: str | None
    arr: float | None
    acv: float | None
    consulting_acv: float | None
    training_acv: float | None
    owner: str | None
    next_steps: str | None
    account_id: str | None
    account_name: str | None
    sf_opportunity_number: str | None


@dataclass(frozen=True)
class OpportunitySearchResult:
    """Mapped opportunities plus whether Salesforce may have truncated SOSL."""

    records: list[OpportunityRecord]
    capped: bool


class DealSplitRecord(TypedDict):
    """Typed dict for a deal split record returned by ``fetch_deal_splits()``."""

    offering_group: str
    services_pct: float


class _SoslAttributes(TypedDict, total=False):
    """Salesforce ``attributes`` block present on every sObject in SOSL results."""

    type: str
    url: str


class _SoslAccountRef(TypedDict, total=False):
    """Nested Account reference returned in SOSL Opportunity results.

    Present when ``Account.Id`` or ``Account.Name`` is included in the
    RETURNING clause.
    """

    Id: str
    Name: str


class SoslRecord(TypedDict, total=False):
    """Typed dict for a single entry in the ``searchRecords`` list from ``sosl_search()``.

    All keys are optional (``total=False``) because SOSL only returns the fields
    explicitly listed in the RETURNING clause, and callers use different clauses.

    Keys are the raw Salesforce API names as returned by the REST search endpoint.
    """

    attributes: _SoslAttributes
    Id: str
    Name: str
    StageName: str
    CloseDate: str
    Amount: float | None
    Consulting_Total_USD__c: float | None
    Training_Total_USD__c: float | None
    OpportunityNumber__c: str | None
    Account: _SoslAccountRef | None
    AccountId: str | None
    Territory2Id: str | None  # implementation note: territory-scoped closed-won queries


class _SFOwnerRef(TypedDict, total=False):
    """Nested Owner reference on an Opportunity sObject."""

    Name: str
    Email: str


class _OppAccountRef(TypedDict, total=False):
    """Nested Account reference on an Opportunity sObject."""

    Id: str
    Name: str
    Industry: str | None


class OpportunitySObject(TypedDict, total=False):
    """Typed dict for a Salesforce Opportunity record returned by ``fetch_record()``.

    Keys enumerate every Salesforce field actually read by fieldkit callers.
    ``total=False`` keeps fields missing in partial fetches (caller-chosen field
    lists) legal at type-check time.

    Callers request fields via ``_OPP_FIELDS`` in ``commands/sf/opportunity.py``.
    """

    Id: str
    Name: str
    StageName: str
    CloseDate: str | None
    IsClosed: bool
    Owner: _SFOwnerRef | None
    Account: _OppAccountRef | None
    ACV_Opportunity_USD__c: float | None
    ARR_Opportunity_USD__c: float | None
    Consulting_Total_USD__c: float | None
    Training_Total_USD__c: float | None
    Application_Services_Total_USD__c: float | None
    Services_Total_USD__c: float | None
    Next_Steps__c: str | None
    Customer_Pain_Point__c: str | None
    Identify_Pain_Long__c: str | None
    Decision_Criteria__c: str | None
    Main_Competitor__c: str | None
    Closed_Lost_Reason__c: str | None
    Probability: float | None
    OpportunityNumber__c: str | None


class AccountResolution(TypedDict, total=False):
    """Typed dict for the shape resolved by ``fetch_account_by_id()``.

    Wraps the Account sObject fields requested via ``_ACCOUNT_SOSL_FIELDS`` in
    ``client.py``.  ``total=False`` because field presence depends on the
    caller-supplied ``fields`` parameter.
    """

    Id: str
    Name: str
    Industry: str | None
    Owner: _SFOwnerRef | None
    BillingCity: str | None
    BillingState: str | None
    Account_Segment__c: str | None


@dataclass(frozen=True)
class UIAPIRecordCollection:
    """UI API records plus evidence that the returned collection is complete."""

    records: list[dict[str, Any]]
    reported_count: int | None
    complete: bool
    issues: list[str]


class SalesforceFieldMetadata(TypedDict):
    """Generic field properties proven by an sObject describe response."""

    api_name: str
    type: str | None
    updateable: bool | None
    calculated: bool | None
    precision: int | None
    scale: int | None
    length: int | None
    picklist_values: list[str] | None


class ConcurrencyEvidence(TypedDict):
    """Read-time version evidence supported by Salesforce conditional requests."""

    field: Literal["LastModifiedDate"]
    value: str | None
    conditional_header: Literal["If-Unmodified-Since"]
    strength: Literal["weak_timestamp"]
    mutation_enabled: Literal[False]


class ClosePlanAnswerChoice(TypedDict):
    """One exact native answer choice belonging to a template question."""

    answer_id: str | None
    name: str | None
    text: str | None
    attitude: str | None
    has_text_answer: bool | None
    max_score: float | None
    sort_order: float | None
    sync_text_answer_field: str | None
    sync_value_field_pickval: str | None
    issues: list[str]


class ClosePlanTemplate(TypedDict):
    """Exact native template identity and version metadata."""

    template_id: str
    version: float | None
    version_name: str | None
    template_type: str | None
    status: str | None
    total_maximum: float | None
    issues: list[str]


class ClosePlanTemplateQuestion(TypedDict):
    """Exact native template binding and complete answer-choice evidence."""

    template_question_id: str
    name: str | None
    template_id: str | None
    category_id: str | None
    question_category_id: str | None
    question_type: str | None
    has_text_answer: bool | None
    score_maximum: float | None
    sync_score_field: str | None
    sync_score_ratio_field: str | None
    has_shared_score: bool | None
    answer_model: Literal["choice", "text", "unsupported", "incomplete"]
    answer_choices_reported_count: int | None
    answer_choices_complete: bool
    answer_choices: list[ClosePlanAnswerChoice]
    issues: list[str]


class MeddpiccQuestion(TypedDict):
    """Typed dict for a single TSPC__DealQuestion__c record (historic regression).

    ``category`` and ``answer`` are derived, not raw fields -- the supported
    TSPC__DealQuestion__c schema has no category field or single answer field.
    ``category`` is parsed from the question's own ``Name`` prefix and
    ``answer`` is the first non-empty of ``TSPC__TextAnswer__c`` /
    ``TSPC__RichTextAnswer__c`` (HTML-stripped). See
    :func:`~fieldkit.sf.meddpicc.extract_category` and
    :func:`~fieldkit.sf.meddpicc.extract_answer`.
    Nullable fields distinguish absent native values without dropping contract keys.
    """

    category: str | None
    question_id: str | None
    name: str | None
    score: float | None
    answer: str | None
    raw_score: str | int | float | bool | None
    raw_text_answer: str | None
    raw_rich_text_answer: str | None
    raw_answer: str | None
    has_text_answer: bool | None
    last_modified_date: str | None
    concurrency: ConcurrencyEvidence
    field_metadata: dict[str, SalesforceFieldMetadata]
    question_type: str | None
    score_maximum: float | None
    weight: float | None
    template_question_id: str | None
    template_metadata_status: Literal["complete", "absent", "incomplete"]
    template_question: ClosePlanTemplateQuestion | None
    template_version: float | None
    metadata_gaps: list[str]
    metadata_issues: list[str]


class MeddpiccElement(TypedDict):
    """Normalized canonical MEDDPICC element with question-level evidence."""

    key: str
    label: str
    state: Literal["unpopulated", "answered_unscored", "scored_zero", "scored"]
    complete: bool
    unanswered_question_ids: list[str | None]
    questions: list[MeddpiccQuestion]


class MeddpiccDeal(TypedDict):
    """One exact ClosePlan deal and all questions returned for it."""

    deal_id: str | None
    name: str | None
    score_ratio: str | int | float | bool | None
    total_score: str | int | float | bool | None
    template_id: str | None
    template_deploy_date: str | None
    template_metadata_status: Literal["complete", "absent", "incomplete"]
    template_version: float | None
    template_total_maximum: float | None
    question_maximum_total: float | None
    native_maximums_consistent: bool | None
    last_modified_date: str | None
    concurrency: ConcurrencyEvidence
    questions_reported_count: int | None
    questions_complete: bool
    issues: list[str]
    elements: list[MeddpiccElement]
    gaps: list[str]
    unmapped: list[MeddpiccQuestion]


class MeddpiccReadResult(TypedDict):
    """Mutation-safe native ClosePlan read result for human and JSON consumers."""

    org_url: str
    opportunity_id: str
    status: Literal["complete", "not_found", "ambiguous", "invalid_selection", "incomplete"]
    complete: bool
    selected_deal_id: str | None
    deals_reported_count: int | None
    deals: list[MeddpiccDeal]
    field_metadata: dict[str, SalesforceFieldMetadata]
    metadata_gaps: list[str]
    issues: list[str]
