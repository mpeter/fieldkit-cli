# Competitive Intelligence

Research competitors active in a deal or account and produce an actionable
battlecard. Not a feature matrix — a tool for winning conversations.

## Gotchas

- **Backstory availability** — check `mcpjungle` only immediately before selecting the `fieldkit-sales` exception; public research uses `tvly`
- **Trigger overlap with similar skills** — check skill names carefully; e.g. this skill vs adjacent skills with similar names
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## Inputs

Required: competitor name(s) or account context (will pull competitors from pursuit file).

Optional: specific product/service area to focus comparison.

## Execution

Routes needed: **fieldkit-sales** (Backstory) and `tvly search` (public web research). Account/pursuit files are read from disk and the battlecard is written directly (native file reads/writes).

### Step 1: Gather context

```
1. read accounts/<account>/pursuits/<opp>.md — competitive context
2. read accounts/<account>/account.md — account priorities
3. backstory__backstory__find_account(<account>) → get peopleai_account_id
4. backstory__backstory__ask_sales_ai_about_account(peopleai_account_id,
   "What competitive threats or mentions have come up for this account?")
```

### Step 2: Research our position

Web search for recent company news relevant to this deal area:
- Product releases, certifications, partnership announcements
- Customer success stories in this vertical
- Analyst positioning (Gartner, Forrester, IDC)

### Step 3: Research each competitor

For each competitor, web search for:
- Product features and recent releases
- Pricing models (public info only)
- Customer reviews and satisfaction signals
- Recent news, funding, M&A, leadership changes
- Hiring patterns (signals strategic direction)
- Known weaknesses from analyst reports and reviews

### Step 4: Cross-reference with internal signals

- Backstory — account-level mentions of competitor in recent conversations
- Slack — `slackcli search messages "[competitor name]"` for field intel
- Past pursuit files — win/loss history against this competitor

### Step 5: Build the battlecard

Read `competitive-intel-battlecard-template.md` for output format.

Key sections:
- Comparison matrix (feature/capability/positioning)
- Talk tracks for different buyer personas (EB, technical, user)
- Landmine questions to naturally expose competitor weaknesses
- Objection handling for competitor claims
- Win/loss patterns from history

## Output Options

- **Markdown battlecard** — default, saved to account artifacts

Save by writing the file `accounts/<account>/artifacts/battlecard-[competitor]-YYYY-MM-DD.md` directly.

**File write guardrail:** Do not write Backstory-derived competitive signals into
pursuit frontmatter, `account.md`, or native ClosePlan fields. Reference the
battlecard path as context for `/grill`; Backstory synthesis is not confirmation
and must not become a local score.

## Related Skills

- `meeting` — Feed competitive positioning into meeting briefs
- `/grill` — Feeds Competition element scoring
