# Contract Check

Review a draft SOW, proposal, or scoping document against the account's
contract intelligence (`contracts.md`) and the SOW language guide. Flags
deviations, missing references, and compliance risks.

Designed to run as a gate before finalizing any customer-facing document
that creates contractual obligations.

## Gotchas

- **Trigger overlap with similar skills** — check skill names carefully; e.g. this skill vs adjacent skills with similar names
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## Inputs

Required: account name + document to check (file path or inline text).

Optional: specific concern areas to focus on (e.g., "rate card compliance",
"liability language", "IP terms").

## Execution

Groups needed: none — account contract and playbook files are read directly from disk (native file reads).

### Step 1: Load contract intelligence

```
1. read accounts/<account>/contracts.md
   — If missing, tell user to run /contract-extract first
2. read playbooks/sow-language-guide.md
3. Read the document to be checked
```

### Step 2: Check commercial compliance

Compare the draft against contract commercial terms:

- **Rate card:** Do proposed rates match or fall within contracted rates?
  Flag any rate that exceeds the ceiling in the master rate card.
- **Payment terms:** Does the SOW specify payment terms consistent with
  the MSA? Flag deviations (e.g., SOW says Net-60, MSA says Net-30).
- **T&E policy:** Are travel and expense terms consistent?
- **Minimum/maximum values:** Does the deal size fall within any
  contractual minimums or caps?

### Step 3: Check legal compliance

- **Liability language:** Does the SOW attempt to set liability terms
  that conflict with the MSA cap? Flag any liability clause that differs
  from the master agreement.
- **Indemnification:** Are indemnification terms consistent?
- **IP ownership:** Does the SOW's IP clause align with the MSA's
  work product ownership terms?
- **Warranty:** Does the SOW make warranty commitments beyond what
  the MSA allows?
- **Termination:** Are termination provisions consistent with the MSA?

### Step 4: Check obligation compliance

- **Provider obligations:** Does the SOW commit the provider to obligations
  not supported by the MSA framework? Flag new obligations.
- **Customer obligations:** Does the SOW include necessary customer
  obligations (access, resources, decisions) per MSA requirements?
- **Subcontracting:** If the SOW involves subcontractors, does it
  comply with MSA subcontracting approval requirements?
- **Insurance:** Does the SOW reference required insurance certificates?
- **Background checks:** Are background check requirements addressed?

### Step 5: Check language guide compliance

Run every sentence through the SOW language guide avoidance list:

- Flag any banned word/phrase with the recommended replacement
- Flag any promise of outcomes vs. delivery of services
- Flag exhaustive vs. non-exhaustive list usage (provider obligations
  must be exhaustive; customer obligations should be non-exhaustive)

### Step 6: Check structural completeness

Compare against the SOW Drafting Checklist from `contracts.md`:

- Is each checklist item addressed in the draft?
- Are MSA/SLMA section references correct?
- Is the change order mechanism referenced?

### Step 7: Generate compliance report

Output a structured report:

```markdown
# Contract Compliance Report
**Account:** <account>
**Document:** <filename or description>
**Date:** YYYY-MM-DD
**Contracts.md version:** <last_extracted date>

## Summary
X RED flags | Y YELLOW flags | Z GREEN items

## RED — Must Fix Before Sending
1. [Section X] Rate of $275/hr exceeds contracted ceiling of $250/hr
   in Amendment 1 Exhibit B. → Reduce rate or obtain customer approval.
2. [Section Y] "The provider ensures all deliverables..." — "ensures" creates
   unintended obligation. → Replace with "The provider will provide reasonable
   assurance that..."

## YELLOW — Review Recommended
1. [Section Z] SOW does not reference MSA change order process.
   → Add reference to MSA Section 14.2.
2. [Section W] Subcontractor mentioned but no approval clause.
   → Add per MSA Section 8.1.

## GREEN — Compliant
- Rate card for Senior Consultant matches Amendment 1 Exhibit B
- Payment terms (Net-30) match MSA Section 6
- IP ownership clause references MSA Section 12 correctly

## Missing Checklist Items
- [ ] Insurance certificate timeline not specified
- [ ] Background check clause not included
```

Present the report to the user. Do not modify the original document —
the user decides which flags to act on.

## Output

- Compliance report (displayed in terminal)
- No files written — this is an advisory check, not a document modifier

## Related Skills

- `contract` skill's `ops/contract-extract.md` — run contract-extract first to build contracts.md
- `/humanizer` — can clean up language after compliance fixes
