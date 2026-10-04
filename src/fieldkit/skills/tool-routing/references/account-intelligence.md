# Account intelligence capabilities

Backstory and Product Pages may be available through a configured authorized MCP
route. Discover that route in the active harness and verify the selected tool's
availability before use; a fixed group name is not guaranteed.

Use Backstory only for account-level intelligence:

- `backstory__find_account`
- `backstory__get_account_status`
- `backstory__get_recent_account_activity`
- `backstory__account_company_news`
- `backstory__find_record_by_crm_id` for account records only

Do not call opportunity-level Backstory tools or request opportunity records;
attribution is unreliable. Salesforce remains the deal system of record. Label
Backstory evidence as account intelligence rather than opportunity truth.

Use Product Pages for the product capabilities actually exposed by the configured
route. If the required tool is absent, report it as unavailable. Salesforce or web
search can be offered as a labeled alternative, never as equivalent source data.
