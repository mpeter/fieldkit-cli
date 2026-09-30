# Deal desk

Prepare a draft engagement estimate from current, operator-authorized commercial
inputs. fieldkit does not supply a rate card, discount policy, approval threshold,
or authority to issue a quote.

## Required inputs

Before calculating, obtain:

- the applicable agreement and approved rate-card revision;
- each role's customer rate, internal cost rate if authorized, currency, and
  billing unit;
- the agreed quantity for each role, including any calendar or utilization
  assumptions;
- the pricing model and any explicitly approved discount or contingency;
- the current approval policy and responsible approver.

Do not infer private rates from a role title, reuse a historical rate card, or
assume that a month contains a fixed number of billable days. Resolve ambiguous
roles, mixed currencies, and incompatible billing units with the operator.

Keep internal costs and margins in the operator's private working material.
Do not include them in a customer-facing draft unless specifically authorized.

## Calculate the draft

For each role, multiply the approved customer rate by the quantity in the same
billing unit to obtain revenue. Calculate internal cost separately from the
authorized cost rate and quantity. Sum revenue and cost across compatible
currency and unit inputs.

When total revenue is positive, blended margin is
`(total revenue - total cost) / total revenue`. It is not the average of the
individual role margins. If costs are unavailable, report margin as unavailable;
if revenue is zero or negative, stop and ask how that commercial case should be
handled rather than dividing or inventing a margin.

Do not apply a default discount. Fixed-price contingency, prepaid-credit
conversion, expiry, and available balances must come from the applicable
agreement or authorized policy, not this skill.

## Review and approval

Show the operator the source revision, assumptions, role quantities, revenue,
and any authorized cost/margin calculations. Mark incomplete inputs explicitly.
Apply only the supplied approval policy; without it, report approval status as
undetermined, never “no approval needed.”

This is a draft, not an approved quote. Do not send it, submit an approval,
change a CRM opportunity, or write account/pursuit files without explicit
authorization for that action. Preserve the operator's existing files.

## Related workflows

Use the [contract check](contract-check.md) to compare a draft with supplied
contract terms. Use the [pipeline forecast](../../pipeline/ops/forecast.md)
for portfolio forecasting; an engagement estimate is not a forecast.
