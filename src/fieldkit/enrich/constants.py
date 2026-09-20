"""Shared constants for the enrich domain.

Import GARBAGE_NAMES from this module rather than defining it locally.
"""

# Role taxonomy strings that appear in MEDDPICC/stakeholder tables but are
# not real contact names. Used to filter out template rows from contact
# extraction and account.md validation.
GARBAGE_NAMES: frozenset[str] = frozenset(
    {
        "Role",
        "Status",
        "Action Needed",
        "Economic Buyer",
        "Champion",
        "Technical Buyer",
        "Influencer",
        "Procurement",
        "Adoption Lead",
        "End User",
        "Legal",
        "Partner Sponsor",
        "Accounts Payable",
        "Evaluator",
        "Paper Process",
        "Decision Maker",
    }
)
