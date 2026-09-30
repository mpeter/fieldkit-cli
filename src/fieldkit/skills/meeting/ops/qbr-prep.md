# QBR Prep

Build a QBR package for a customer account. Lead with their outcomes, not a
vendor's products. Optional account-intelligence signals can suggest risks and
questions, but they are not verified customer facts.

## Gotchas

- **Stale local signals** — record source dates; a brief preview does not refresh integrations
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Do not advance deal stages without running the gate check first**
- **Always surface generated output for review before any external send**

## Inputs

Required:
- Account name
- Quarter and year selected by the operator

Optional (will ask if not provided):
- QBR date and time
- Attendees (names and titles)
- Themes to emphasize or avoid

## Optional pursuit narrative

If a relevant pursuit is selected, read its frontmatter. When `gdoc_narrative`
is set, the operator authorizes access, and `gws` is configured, fetch that
document and record its URL and retrieval date in private notes. Use it as
attributed deal context, not as independent confirmation of customer outcomes.

If the document is unavailable, use the local pursuit record and disclose its
date. If there is no relevant pursuit, omit deal-specific context.

---

## Execution

Use the configured workspace's selected account files. Optional sources are an
operator-configured account-intelligence provider and authorized public research.
No particular private gateway, search client, or browser harness is required.
A missing integration is an omitted source, not a reason to invent its output.
Bound reads to selected accounts, relevant dates, and approved sources; do not
upload private account material to a research provider automatically.

### Step 1: Load account context

Read the account record for the stakeholder map, strategic context, open
pursuits, Salesforce linkage, and documented qualification evidence needs.
Inspect prior QBRs for open commitments and the most recent dated meeting notes
for current interactions. Use paths under the configured workspace; missing
records are unavailable evidence, not an empty customer history.

### Step 2: Pull Backstory signals when available

If an authorized Backstory client is configured and account intelligence is in
scope, inspect its account-level signals. Otherwise skip this step and mark Backstory-derived
sections unavailable in the draft.

Use the available account-level route described by
[tool routing](../../tool-routing/SKILL.md) to inspect status, recent activity,
and, where appropriate, company news. Record the source date and distinguish
observed activity from a provider's health score or AI synthesis. Treat suggested
risks and expansion opportunities as questions to verify with the customer.

### Step 2b: Search Slack for account mentions

If the operator authorizes Slack access and supplies a configured client, search
selected account-relevant channels within an agreed date and result bound for
recent themes and escalations. Exclude unrelated internal conversations. Resolve
unknown authors before attribution. If Slack is unavailable, omit that source
and say so in private preparation notes.

Use Slack signals to locate customer asks and open items for verification in
primary records. Do not copy internal team discussion into a customer-facing
QBR or present it as a customer commitment.

### Step 2c: Optional internal research

If the operator supplies an authorized internal source, use relevant, dated
material only in private preparation notes. No internal knowledge service is
bundled with fieldkit. Omit this source if unavailable, and do not copy private
organizational messaging into the customer-facing QBR.

### Step 3: Build the QBR

Frame everything problem-first. Open with what changed in their world and
what your team delivered toward their outcomes. Expansion goes last — and
only after establishing value.

### Step 4: Save output

After confirming the destination and write, save under the configured
workspace's `accounts/<account>/qbrs/` directory with a `<YYYY>-Q<N>.md` name.
Validate the account and destination, reject traversal and symlink escapes, and
obtain separate approval before replacing an existing draft. Write UTF-8
atomically, reread the saved draft, and report success only after that readback.
The draft remains private unless the operator separately approves sharing.

**File write guardrails:**
- The QBR file is a presentation draft, not a factual record. Label each provider-derived signal with its source and date, and mark synthesis unverified before presenting it as fact.
- Do NOT backport any content from this QBR into pursuit files, pursuit frontmatter, or `account.md`. If the QBR surfaces new stakeholder or deal context worth keeping, surface it to the user and let them decide.
- Provider synthesis may appear in a private QBR draft only, never in frontmatter or stakeholder maps.

---

## Output format

Use the [QBR output template](qbr-prep-output-template.md). Keep its source
labels and unavailable-data markers in the draft; remove unsupported claims
instead of filling gaps with synthesis.

---

## Related Skills

- [Account pulse](account-pulse.md) — Quick pre-QBR signal check when that integration is available.
- [Meeting skill](../SKILL.md) — Use the QBR package as context for meeting preparation.
- [Grill](../../grill/SKILL.md) — Qualify any expansion opportunity surfaced during QBR prep.
