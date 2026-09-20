---
name: win-loss
description: Capture a structured win/loss debrief from a closed pursuit. Writes a dated lessons-learned entry to memory/system/lessons-learned.md and updates pursuit frontmatter to closed-won or closed-lost with a transition history entry. For closed-won deals, prompts to create a project file. Trigger with "win/loss on [deal]", "debrief [opportunity]", "close out [pursuit]", "record win [account]", "record loss [account]", "capture win-loss [deal]", or "mark [pursuit] closed".
metadata:
  opencode/slash: "true"
  category: product
---

# Win/Loss Capture

Conduct a structured post-close debrief on a pursuit, write the lessons to
`memory/system/lessons-learned.md`, and update the pursuit frontmatter to the final stage.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Inputs

Required:
- Pursuit file path (e.g. `accounts/acme-corp/pursuits/project-shift.md`)
  OR account name + opportunity name (skill will resolve the path)

Optional:
- Outcome (won / lost) — if omitted, skill asks in debrief

## Execution

Groups needed: none — pursuit and memory files are read, searched, and edited directly on disk (native file reads/edits).

### Step 1: Resolve pursuit file

If a full path was provided, read it directly.

Otherwise, search on disk:
```
rg -l "<opportunity name>" accounts/*/pursuits/
```
Pick the best match and confirm the resolved path with the user before proceeding if ambiguous.

Read the full pursuit file (frontmatter + body) directly to orient the debrief:
- `stage` — current stage
- `transition-history` — full history array
- `legacy_meddpicc` or former `meddpicc` — preserve as historical values from the
  time of close; use a fresh ClosePlan read for current qualification
- `sf_arr` or `arr` — contract value if available

### Step 2: Conduct 5-area structured debrief

Ask the five areas in order. Do not skip — each area feeds a distinct lesson.

**Area 1 — Outcome**
- Won or lost?
- Close date (actual)?
- Final contracted value (ARR or TCV)?

**Area 2 — Why we won / lost**
- In the customer's words, if available — what tipped the decision?
- If lost: competitor win, initiative cancelled, no decision, pricing, or other?

**Area 3 — Deciding MEDDPICC factor**
- Which single MEDDPICC element most influenced the outcome?
- (Metrics / Economic Buyer / Decision Criteria / Decision Process /
  Identify Pain / Champion / Competition / Paper Process)
- What specifically happened with that element?

**Area 4 — What to change**
- One concrete change to the sales motion, qualification, or delivery approach
  that would improve the next similar deal.

**Area 5 — Relationship intelligence**
- Did the Champion hold? Did they advocate at the right levels?
- Economic Buyer relationship — direct access or brokered through others?
- Any surprises in the buying committee?

### Step 3: Synthesize and confirm

Before writing anything, show the user the draft entry:

```markdown
## [YYYY-MM-DD] — [Account] — [closed-won/closed-lost]: [Opportunity Name]

- **Outcome:** [one sentence — what closed and for how much]
- **Deciding MEDDPICC factor:** [element] — [detail from Area 3]
- **Competitive position:** [who we beat / lost to and why, from Area 2]
- **What worked:** [1-2 bullets from Areas 2 and 5]
- **What to change:** [1-2 bullets from Area 4]
- **Relationship note:** [Champion and EB assessment from Area 5]
```

Ask: "Does this capture it accurately? Any corrections before I write it?"

Wait for confirmation. Do not write until the user approves or says "looks good".

### Step 4: Write lessons-learned entry

Append the confirmed entry to `memory/system/lessons-learned.md` directly:

```
# 1. Read the current content of memory/system/lessons-learned.md
# 2. Write it back with the confirmed entry appended to the body
#    (preserve the frontmatter block verbatim)
```

Separate from the previous entry with a blank line and `---` rule.
Do not modify any existing entries.

### Step 5: Update pursuit frontmatter

Update the pursuit file's frontmatter and transition-history directly on disk.
Two edits:

**Edit 1 — Update frontmatter fields:**
```
# 1. Read the current file at <pursuit_file_path>
# 2. Apply a targeted edit replacing the current frontmatter YAML block
#    with the updated frontmatter (leave the body untouched)
```

Update these fields:
1. `stage` → `closed-won` or `closed-lost`
2. `gate-status` → `pass` (won) or `override` (lost)
3. `last-transition` → today's date (ISO 8601: YYYY-MM-DD)

**Edit 2 — Append to transition-history**:
Append a new entry to the `transition-history` YAML array:

```yaml
- date: YYYY-MM-DD
  from: [previous-stage]
  to: closed-won        # or closed-lost
  gate-result: pass     # or "override" for lost
  override-reason: ""   # non-empty for lost — one-line loss reason from debrief
```

Preserve all other content exactly as-is. Do not modify historical qualification
values or write native ClosePlan state.

**Do not write Backstory-derived data to the frontmatter.**

### Step 6: Closed-won — prompt project file creation

If the outcome is `closed-won`, display:

```
Deal closed. Consider converting to a project file:

  accounts/<account>/projects/<opportunity-name>.md

Would you like to create the project file now? (yes / no)
```

If yes, create `accounts/<account>/projects/<opportunity-name>.md` with:

```markdown
---
account: [Account]
name: [Opportunity Name]
status: active
start_date: [close date or contract start if known]
value: [ARR/TCV]
pm: [DATA NEEDED]
---

# [Account] — [Project Name]

[One-sentence description of what was sold.]

## Scope

[DATA NEEDED — fill from SOW when available]

## Key Contacts

[DATA NEEDED — copy from pursuit stakeholder section]

## Delivery Notes

[DATA NEEDED]
```

### Step 7: Confirm and summarize

Display:

```markdown
## Win/Loss Closed

**Deal:** [opportunity name]
**Account:** [account]
**Outcome:** [closed-won / closed-lost]
**Date:** [today]
**Lessons logged:** memory/system/lessons-learned.md
**Pursuit updated:** [pursuit file path]
[If closed-won and project file created] **Project file:** accounts/<account>/projects/<name>.md
```

## Output Format

Lessons-learned entry (appended to `memory/system/lessons-learned.md`):

```markdown
## YYYY-MM-DD — [Account] — [closed-won/closed-lost]: [Opportunity Name]

- **Outcome:** [one sentence]
- **Deciding MEDDPICC factor:** [element] — [detail]
- **Competitive position:** [who we beat/lost to and why]
- **What worked:** [1-2 bullets]
- **What to change:** [1-2 bullets]
- **Relationship note:** [Champion and EB assessment]
```

Pursuit frontmatter changes:
- `stage`: `closed-won` or `closed-lost`
- `gate-status`: `pass` or `override`
- `last-transition`: YYYY-MM-DD
- `transition-history`: new entry appended

## Error Cases

- **Pursuit file not found:** Stop. List available pursuit files in the account folder.
- **Already closed (closed-won or closed-lost):** Stop. Show existing close entry from
  `transition-history`. Ask if the user wants to add a supplemental lessons note only.
- **No memory/system/lessons-learned.md:** Create the file with the standard header before appending.
- **Debrief answers incomplete:** Do not write a partial entry. Ask the missing questions
  before proceeding to Step 3.

## Related Skills

- **pursuit-advance** — Standard stage transitions with gate checks; use before closing
- **grill** — Review exact native ClosePlan questions read-only while the deal is active
- **followup-draft** — Draft a post-close thank-you or transition email to the customer
- **the `pipeline` skill's `src/fieldkit/skills/pipeline/ops/engagement-health.md`** — Review active delivery projects after a closed-won converts
