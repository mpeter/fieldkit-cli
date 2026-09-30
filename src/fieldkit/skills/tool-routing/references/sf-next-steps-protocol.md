# Salesforce next-step proposals

Propose a change to an Opportunity's `Next_Steps__c` only when there is a specific,
supported successor action and the current value is empty, completed, or no longer
accurate. Field age alone is insufficient. Do not propose routine next-step changes
for closed opportunities.

## Establish the evidence

Identify the exact Opportunity and obtain its current field value through an
authorized live read. A cached `sf_next_steps` value is not proof of the current
Salesforce value. If the read fails, report the current value as unavailable.

Distinguish commitments made by the operator or in a sent message from unsent
drafts and model suggestions. A draft is a proposal, not proof that its author
made the commitment. Ask for clarification when the source is ambiguous.

Present the current value, source and date, proposed replacement, and rationale.
Use one or two actionable sentences with a person and deadline only when known.
Do not include unsupported facts or private source content in public evidence.

## Preview and operator-controlled write

`fieldkit sf set-next-steps OPP_ID TEXT` fetches the current value and previews
the replacement. It does not PATCH the record, but it does require live access
and valid credentials; it is not an offline operation.

`--confirm` enables the external write. The CLI prompts again only when stdin is
interactive; non-interactive invocation does not supply a second approval step.
The outbound hook blocks selected agent tool-call forms, not every possible
transport. Do not rely on either mechanism as universal authorization enforcement.

Present the exact proposal for the operator to run. Do not autonomously execute
the confirmed write or bypass a hook. Explain the selected record and replacement
text before asking for approval; never hide a mutation inside a preview workflow.

## Verify the result

The command reports the API write result but does not fetch the field afterward.
Require a separate authorized read-back and compare the persisted value with the
approved text before declaring verification complete. If the outcome is uncertain,
read current state before retrying; another writer may have changed the record.

Local pursuit synchronization is a separate workspace write. Do not manually edit
`sf_*` fields to simulate a successful Salesforce update. If synchronization is
requested, inspect the supported `fieldkit sf opportunity` interface and its
destination before running it; it can write local pursuit data.
