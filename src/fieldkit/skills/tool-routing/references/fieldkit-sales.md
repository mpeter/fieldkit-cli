# Sales intelligence MCP exception

`fieldkit-sales` is retained because Backstory and Product Pages are proprietary
services with no installed CLI or public file/API route.

Use Backstory only for account-level intelligence:

- `backstory__find_account`
- `backstory__get_account_status`
- `backstory__get_recent_account_activity`
- `backstory__account_company_news`
- `backstory__find_record_by_crm_id` for account records

Do not use opportunity-level Backstory results; attribution is unreliable.

Use Product Pages for product entities, hierarchy, schedules, people, status
posts, and reports. If the group is absent, stop and report the service as
unavailable rather than substituting Salesforce or web search as equivalent.
