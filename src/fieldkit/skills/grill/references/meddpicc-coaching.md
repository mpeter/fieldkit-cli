# MEDDPICC Coaching Reference

Companion to `/grill`'s "Stage evidence against question IDs" step. Use this when
framing the "one concise question testing missing evidence" for a given element —
it does not define a score, a gate, or a qualification verdict.

**This is generic methodology, not your org's exact wording.** ClosePlan/TSPC
templates are configured per Salesforce org: category names, question count,
tier text, and maxima all vary. Always read the live template through
`fieldkit sf meddpicc <opp_id> --json` before quoting a question back to the
operator or citing "what ClosePlan says" — never assume this file's example
language matches the deployed template.

## What MEDDPICC answers

Eight elements answering one question: does the evidence support forecasting
this deal with confidence, or is a gap being smoothed over? A `0`/unanswered
element is not neutral — it is something a competitor who has the answer can
use against you.

## How a ClosePlan category is typically structured

A TSPC/ClosePlan category usually combines:

- one graduated tier question (often 3 tiers, lowest to highest confidence),
- one or more binary checklist sub-items ("Have the risks been identified?"),
- a free-text **Notes** field, separate from the tier selection.

The category rollup blends the tier selection and the checklist state — selecting
the lowest tier with no checklist items done does not read as the lowest possible
percentage, because the checklist items still contribute. Per-question maxima are
frequently uneven across elements (see `docs/guides/pipeline-workflow.md`'s
`SF meddpicc` section for the exact read contract): do not assume equal weighting
the way a generic 0-3-per-element scale would.

Selecting the highest tier is a UI action, not evidence. The tier text alone does
not enforce that a customer actually confirmed anything — that gap is exactly
what staged evidence with a source and date is for.

## The eight elements

### Metrics

**Definition:** The quantified business impact of solving the customer's
problem — their numbers, in their language, not your ROI model.

**Red flags:**

- "They said it's strategically important" — strategic is not a metric.
- Impact is stated in technical terms (servers migrated) instead of business
  terms ($, %, time).
- The only numbers on the table came from your own calculator, unconfirmed by
  the customer.

**Coaching questions:**

- "What does success look like for this project in 12 months?"
- "How is [economic buyer] measuring the success of this initiative?"
- "What's the cost of not solving this?"
- "If you put a number on the problem, what would it be?"

### Economic Buyer

**Definition:** The person who controls the budget and can say yes or no —
not the project sponsor, not the technical champion.

**Red flags:**

- Nobody on the deal team has met them.
- The champion says "I'll handle the EB" and that gets accepted at face value.
- The EB is a committee with no single accountable decision-maker.
- The EB surfaces for the first time during procurement.

**Coaching questions:**

- "Who ultimately approves the budget for an initiative like this?"
- "Have you talked to [name] about funding this?"
- "What does approval look like above your level?"

### Decision Criteria

**Definition:** The stated and unstated factors the customer uses to evaluate
vendors.

**Red flags:**

- Nobody has asked what the customer is evaluating vendors on.
- The criteria were set by a competitor and the deal team doesn't know it.
- The pitch leads with features the customer never said they cared about.

**Coaching questions:**

- "If you scored vendors on a scorecard, what would be on it?"
- "What does the ideal solution look like to you?"
- "What would disqualify a vendor here?"

### Decision Process

**Definition:** The steps, people, and timeline from verbal agreement to
signed contract.

**Red flags:**

- The close-date estimate assumes legal and procurement take two weeks with
  no evidence for that.
- Nobody knows who is involved in vendor approval.
- A required security or compliance review surfaces late, after the forecast
  already assumed it wouldn't exist.

**Coaching questions:**

- "Walk me through how a decision like this gets made — from verbal yes to
  signed paper."
- "Who else is involved beyond your team?"
- "Have you gone through a procurement process with a vendor like us before?
  How long did that take?"

### Identify Pain

**Definition:** The specific, compelling business problem driving urgency —
not a general desire for improvement.

**Red flags:**

- "They want to modernize" is a goal, not a pain.
- The pain is felt only by technical staff, not the economic buyer.
- There is no deadline; the customer can wait indefinitely.

**Coaching questions:**

- "What's driving this now, versus six months ago?"
- "What happens if this isn't solved by [date]?"
- "Who else in the organization feels this pain?"

Look for the triggering event that created urgency — a cost increase notice, an
executive directive, an audit finding, competitive pressure, new leadership with
a mandate. A pain with no trigger event tends to slip indefinitely.

### Champion

**Definition:** Someone inside the account who actively advocates for you when
you are not in the room — not just a friendly contact.

**Red flags:**

- The "champion" has never coached the deal team on internal politics.
- They have no access to the economic buyer.
- They agree with everything said to them — that's a coach, not a champion.
- The relationship is single-threaded; one person leaving kills the deal.

**Coaching questions (tests for a real champion):**

1. Have they shared non-public information?
2. Have they coached the deal team on internal politics?
3. Have they advocated in a meeting the deal team wasn't in?
4. Do they have credibility with the economic buyer?
5. Have they defended the deal when it was challenged?

### Competition

**Definition:** Who else is in the deal, and what their position is.

**Red flags:**

- "We're not aware of any competition" — there is always competition,
  including doing nothing.
- A competitor has an executive relationship the deal team doesn't know about.
- The deal is being fought on features instead of outcomes.

**Coaching questions:**

- "Who else are you evaluating?"
- "Have you worked with [competitor] before? How did that go?"
- "What other options are on the table?"

Competition types worth naming explicitly: a named vendor, an internal
build-it-yourself option, an incumbent being displaced, and status quo / do
nothing.

### Paper Process

**Definition:** The path from verbal agreement to signed contract — where
deals die after the team thought they'd won.

**Red flags:**

- Nobody knows whether a master agreement already exists with this customer.
- Legal review timeline is unknown.
- A required security review hasn't been scoped.
- Procurement has a competing priority likely to deprioritize this deal.

**Coaching questions:**

- "Have you worked with us on a contract before? Is there a master agreement
  in place?"
- "Who's involved in contract review on your side?"
- "What's the typical timeline from decision to signed paper for an
  engagement like this?"

## Native qualification policy

This reference does not define stage gates or minimum scores. Native
qualification policy has not been separately ratified — see
`docs/guides/pipeline-workflow.md`'s "Step 3: Advance a pursuit" section for
current behavior. Do not derive a pass/fail threshold from the tier or
checklist language above.
