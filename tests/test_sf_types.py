"""Tests for src/fieldkit/sf/types.py TypedDict definitions."""

import pytest

from fieldkit.sf.types import (
    AccountResolution,
    DealSplitRecord,
    OpportunityRecord,
    OpportunitySObject,
    SoslRecord,
)


@pytest.mark.unit
def test_opportunity_record_keys() -> None:
    """All documented keys of OpportunityRecord must be accessible."""
    expected_keys = {
        "opportunity_id",
        "name",
        "stage",
        "close_date",
        "arr",
        "acv",
        "consulting_acv",
        "training_acv",
        "owner",
        "next_steps",
        "account_id",
        "account_name",
        "sf_opportunity_number",  # implementation change: join key for Varicent attainment reports
    }
    # OpportunityRecord uses total=False so all keys are optional
    assert expected_keys == set(OpportunityRecord.__annotations__)


@pytest.mark.unit
def test_opportunity_record_from_mock_sf_response() -> None:
    """Cast a mock SF response dict to OpportunityRecord; key access must not raise."""
    mock: OpportunityRecord = {  # type: ignore[typeddict-item]
        "opportunity_id": "006abc123",
        "name": "Acme Corp — Consulting",
        "stage": "Proposal",
        "close_date": "2026-12-31",
        "arr": None,
        "acv": None,
        "consulting_acv": 50000.0,
        "training_acv": None,
        "owner": None,
        "next_steps": None,
        "account_id": "001abc",
        "account_name": "Acme Corp",
    }
    assert mock["opportunity_id"] == "006abc123"
    assert mock["name"] == "Acme Corp — Consulting"
    assert mock.get("arr") is None


@pytest.mark.unit
def test_deal_split_record_keys() -> None:
    """DealSplitRecord must have offering_group and services_pct."""
    assert set(DealSplitRecord.__annotations__) == {"offering_group", "services_pct"}


@pytest.mark.unit
def test_deal_split_record_from_mock() -> None:
    """Construct a DealSplitRecord from a mock dict."""
    split: DealSplitRecord = {"offering_group": "Consulting", "services_pct": 0.75}
    assert split["offering_group"] == "Consulting"
    assert split["services_pct"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# SoslRecord
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sosl_record_keys() -> None:
    """SoslRecord must include the SOSL field set consumed by fieldkit callers."""
    expected = {
        "attributes",
        "Id",
        "Name",
        "StageName",
        "CloseDate",
        "Amount",
        "Consulting_Total_USD__c",
        "Training_Total_USD__c",
        "OpportunityNumber__c",  # implementation change: numeric join key for Varicent attainment reports
        "Account",
        "AccountId",
        "Territory2Id",  # implementation note: territory scoping for closed-won queries
    }
    assert expected == set(SoslRecord.__annotations__)


@pytest.mark.unit
def test_sosl_record_from_mock_opportunity_search() -> None:
    """A mock SOSL searchRecords entry for Opportunity search round-trips correctly."""
    mock: SoslRecord = {
        "Id": "006abc456",
        "Name": "Acme Q3 Consulting",
        "StageName": "Proposal",
        "CloseDate": "2026-09-30",
        "Consulting_Total_USD__c": 120000.0,
        "Training_Total_USD__c": None,
        "Account": {"Id": "001abc", "Name": "Acme Corp"},
    }
    assert mock["Id"] == "006abc456"
    assert mock.get("Consulting_Total_USD__c") == pytest.approx(120000.0)
    assert mock.get("Training_Total_USD__c") is None
    acct = mock.get("Account") or {}
    assert acct.get("Id") == "001abc"


@pytest.mark.unit
def test_sosl_record_account_id_variant() -> None:
    """A SOSL result using AccountId (batch resolver variant) is accessible."""
    mock: SoslRecord = {"Id": "006xyz789", "AccountId": "001xyz"}
    assert mock.get("AccountId") == "001xyz"
    # Account field absent — .get() returns None, not KeyError
    assert mock.get("Account") is None


# ---------------------------------------------------------------------------
# OpportunitySObject
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_opportunity_sobject_keys() -> None:
    """OpportunitySObject must declare all fields consumed by opportunity.py callers."""
    expected = {
        "Id",
        "Name",
        "StageName",
        "CloseDate",
        "IsClosed",
        "Owner",
        "Account",
        "ACV_Opportunity_USD__c",
        "ARR_Opportunity_USD__c",
        "Consulting_Total_USD__c",
        "Training_Total_USD__c",
        "Application_Services_Total_USD__c",
        "Services_Total_USD__c",
        "Next_Steps__c",
        "Customer_Pain_Point__c",
        "Identify_Pain_Long__c",
        "Decision_Criteria__c",
        "Main_Competitor__c",
        "Closed_Lost_Reason__c",
        "Probability",
        "OpportunityNumber__c",  # implementation change: numeric join key for Varicent attainment reports
    }
    assert expected == set(OpportunitySObject.__annotations__)


@pytest.mark.unit
def test_opportunity_sobject_from_mock_sf_response() -> None:
    """A representative mock Opportunity sObject round-trips through OpportunitySObject."""
    mock: OpportunitySObject = {
        "Id": "006opp001",
        "Name": "Acme Corp — Consulting 2026",
        "StageName": "Proposal",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Jane AE", "Email": "jane@example.com"},
        "Account": {"Id": "001acct", "Name": "Acme Corp", "Industry": "Technology"},
        "ACV_Opportunity_USD__c": 200000.0,
        "ARR_Opportunity_USD__c": None,
        "Consulting_Total_USD__c": 180000.0,
        "Training_Total_USD__c": 20000.0,
        "Application_Services_Total_USD__c": None,
        "Services_Total_USD__c": 200000.0,
        "Next_Steps__c": "Schedule POC kickoff",
        "Customer_Pain_Point__c": None,
        "Identify_Pain_Long__c": "Legacy infrastructure modernization",
        "Decision_Criteria__c": "OpenShift certification required",
        "Main_Competitor__c": "VMware",
        "Closed_Lost_Reason__c": None,
        "Probability": 60.0,
    }
    assert mock["Id"] == "006opp001"
    assert mock["StageName"] == "Proposal"
    owner = mock.get("Owner") or {}
    assert owner.get("Name") == "Jane AE"
    acct = mock.get("Account") or {}
    assert acct.get("Id") == "001acct"
    assert mock.get("Consulting_Total_USD__c") == pytest.approx(180000.0)
    assert mock.get("Closed_Lost_Reason__c") is None


@pytest.mark.unit
def test_opportunity_sobject_partial_field_set() -> None:
    """OpportunitySObject with only a small field set (e.g. Id,Name,Next_Steps__c) is valid."""
    # Mirrors the fetch in set_next_steps.py: fields="Id,Name,Next_Steps__c"
    mock: OpportunitySObject = {
        "Id": "006min001",
        "Name": "Minimal Fetch",
        "Next_Steps__c": "Kick off POC",
    }
    assert mock.get("Next_Steps__c") == "Kick off POC"
    # All other fields absent — .get() returns None
    assert mock.get("StageName") is None
    assert mock.get("Owner") is None


# ---------------------------------------------------------------------------
# AccountResolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_account_resolution_keys() -> None:
    """AccountResolution must declare all Account sObject fields used by client callers."""
    expected = {
        "Id",
        "Name",
        "Industry",
        "Owner",
        "BillingCity",
        "BillingState",
        "Account_Segment__c",
        # BillingCountry, Type, Account_Subsegment__c, AnnualRevenue, NumberOfEmployees
        # are NOT fetched by _ACCOUNT_SOSL_FIELDS in client.py — removed from TypedDict
        # to avoid a false contract (review finding HIGH-1 on PR #1148).
    }
    assert expected == set(AccountResolution.__annotations__)


@pytest.mark.unit
def test_account_resolution_from_mock_sf_response() -> None:
    """A representative mock Account sObject round-trips through AccountResolution."""
    mock: AccountResolution = {
        "Id": "001acct001",
        "Name": "Acme Corporation",
        "Industry": "Technology",
        "Owner": {"Name": "Jane AE", "Email": "jane@example.com"},
        "BillingCity": "San Francisco",
        "BillingState": "CA",
        "Account_Segment__c": "Enterprise",
        # Only fields from _ACCOUNT_SOSL_FIELDS — no BillingCountry/Type/etc.
    }
    assert mock["Id"] == "001acct001"
    assert mock["Name"] == "Acme Corporation"
    owner = mock.get("Owner") or {}
    assert owner.get("Name") == "Jane AE"
    assert mock.get("Account_Segment__c") == "Enterprise"
