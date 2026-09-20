"""fieldkit.gmail.constants — Shared constants for Gmail domain classification.

Governance criterion for AUTOMATION_NOISE_DOMAINS:
    A domain qualifies for this set if and only if it is a third-party SaaS
    vendor that sends automated transactional mail on behalf of *any* customer
    organization (not just one deployment's own infrastructure). Domains that could represent
    an org's own internal tooling MUST NOT be added without explicit
    justification.
"""

# ---------------------------------------------------------------------------
# Automation noise domains
# ---------------------------------------------------------------------------

# Task 0.2 judgment call: google.com / googlemail.com were added alongside
# service-now.com in the fallback set (commit 829394a9) — not alongside
# org-identity domains. Evidence: no git blame entry ties them to a specific
# org; they appear in the same block as automation noise. Defaulted to
# AUTOMATION_NOISE_DOMAINS per task 0.2 (ambiguous → noise constant).
AUTOMATION_NOISE_DOMAINS: frozenset[str] = frozenset(
    {
        # Generic Gmail automation noise — calendar notifications, bounce addresses
        # (task 0.2: no org-identity evidence; defaulted to noise constant)
        "google.com",
        "googlemail.com",
        # ITSM / CRM automation
        "service-now.com",
        "servicenow.com",
        "salesforce.com",
        "sfdctest.com",
        "force.com",
        "exacttarget.com",
        # Contract / document automation noise
        "docusign.com",
        "docusign.net",
        "adobesign.com",
        "echosign.com",
        "hellosign.com",
        # Calendar / meeting automation noise
        "calendly.com",
        "zoom.us",
        # AI tool notification noise
        "gemini.google.com",
        "bard.google.com",
        # Generic automation / notification noise
        "noreply.github.com",
        "notifications.github.com",
        "mailchimp.com",
        "constantcontact.com",
        "sendgrid.net",
        "mailgun.org",
    }
)
