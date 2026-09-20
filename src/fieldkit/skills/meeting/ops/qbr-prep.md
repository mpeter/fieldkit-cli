# QBR Prep

Build a complete QBR package for a customer account. Leads with their outcomes,
not a vendor's products. Pulls Backstory signals to surface risks and expansion
angles you may not find in meeting notes alone.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Do not advance deal stages without running the gate check first**
- **Always surface generated output for review before any external send**

## Inputs

Required:
- Account name
- Quarter (e.g. Q2 2026)

Optional (will ask if not provided):
- QBR date and time
- Attendees (names and titles)
- Themes to emphasize or avoid

## Step: Fetch pursuit narrative from GDocs if configured

Read pursuit frontmatter. If `gdoc_narrative` is set and non-empty:
- Fetch narrative: `gws docs documents get --params '{"documentId":"<gdoc_narrative value>"}'`
- Use this as the deal background and relationship context in the QBR
- Cite the GDoc URL in the output: https://docs.google.com/document/d/<id>/edit

If absent or empty, fall through to reading the pursuit markdown file as before.

---

## Execution

Routes needed: **fieldkit-sales** (Backstory MCP exception), `tvly` (web research), and `chrome-use` (optional intranet research). Account files are read directly and the QBR is written directly.

### Step 1: Load account context

```
1. read accounts/<account>/account.md
   → stakeholder map, strategic context, open pursuits, Salesforce linkage, and
     explicitly documented qualification evidence needs

2. ls accounts/<account>/qbrs/
   → prior QBR notes for continuity and open commitments

3. ls -t accounts/<account>/meetings/*.md | head -3
   → recent interactions and commitments made
```

### Step 2: Pull Backstory signals

```
1. backstory__backstory__find_account(<account name>)
   → peopleai_account_id

2. backstory__backstory__get_account_status(peopleai_account_id)
   → health score, risks flagged, next steps, trending topics

3. backstory__backstory__get_recent_account_activity(peopleai_account_id)
   → what's been discussed in last 30 days across all contacts

4. backstory__backstory__account_company_news(peopleai_account_id)   [public companies only]
   → business pressures, leadership changes, strategic shifts

5. backstory__backstory__ask_sales_ai_about_account(
     peopleai_account_id,
     "What are the key themes, open risks, and expansion opportunities
      I should address in this QBR?"
   )
   → AI synthesis of account health and whitespace
```

### Step 2b: Search Slack for account mentions

Search for recent themes and escalations across Slack:

```
1. slackcli search messages "<account name>" --limit 20
   → recent mentions across all channels

2. For any unknown Slack users in the results:
   slackcli search people "<name or email>"
   → resolve to name/email for stakeholder attribution
```

Fold Slack signals into the QBR sections:
- Customer asks surfaced in Slack → Their Stated Priorities
- Team coordination threads → Open Items / Value Delivered context

### Step 2c: The Source Research (optional)

If thesource session is valid (`~/.config/thesource-mcp/cookies.txt` < 10h old):

1. Use `thesource search_content` with account industry, product areas in scope, and "QBR" as query terms
2. Use your configured knowledge source to see what your organization is currently focused on internally.
3. Include up to 3 relevant results in a **What We Are Saying Internally** section:
   ```
   From The Source:
   - "AI Platform Roadmap Q3 2026" — configured knowledge-source URL
   - "Virtualization Strategy Update" — configured knowledge-source URL
   ```
4. If session is missing/expired: skip and note "thesource session not active — internal context excluded"

This section helps the AE understand current organizational messaging before speaking to a customer.

### Step 3: Build the QBR

Frame everything problem-first. Open with what changed in their world and
what your team delivered toward their outcomes. Expansion goes last — and
only after establishing value.

### Step 4: Save output

Write the file directly:

```
accounts/<account>/qbrs/<YYYY>-Q<N>.md
```

**File write guardrails:**
- The QBR file is a presentation document, not a factual record. Backstory synthesis sections (`Business Context`, `Relationship Health`, `Expansion Opportunities`) must be clearly labeled as sourced from Backstory — prefix each with `> *[Source: Backstory — verify before presenting as fact]*`.
- Do NOT backport any content from this QBR into pursuit files, pursuit frontmatter, or `account.md`. If the QBR surfaces new stakeholder or deal context worth keeping, surface it to the user and let them decide.
- `ask_sales_ai_about_account` output may appear in the QBR draft only — never in frontmatter or stakeholder maps.

---

## Output Format (see also: `ops/qbr-prep-output-template.md`)

```markdown
# QBR: [Account] — [Quarter]

**Date:** [QBR date]
**Attendees:** [Names and roles]
**Prepared by:** {{name}}, {{company}}

---

## Executive Summary

[2–3 sentences: What they achieved this quarter, what your team contributed,
and what's coming. Lead with their outcome, not our delivery.]

---

## Their World This Quarter

### Business Context
[Company news, industry pressures, leadership changes — from Backstory + web.
Frame as: "Here's what's changed in your environment since we last met."]

### Their Stated Priorities
[What Backstory activity signals show they're focused on.
What has dominated recent conversations?]

---

## Value Delivered

| Initiative | Outcome | Business Impact |
|------------|---------|-----------------|
| [Project / engagement] | [What was delivered] | [$ / % / time saved — or DATA NEEDED] |

---

## Relationship Health

| Stakeholder | Role | Engagement Signal | Notes |
|-------------|------|-------------------|-------|
| [Name] | [Title] | [Active / Quiet / New from Backstory] | |

**Backstory summary:** [Overall engagement level and sentiment from account signals]

---

## Open Items from Last QBR

| Commitment | Owner | Status |
|------------|-------|--------|
| [Action item from prior QBR] | [Our team / Customer] | [Closed / In progress / Overdue] |

---

## Risks to Address

[Backstory-flagged risks + any from account.md or recent meetings.
Surface these before the customer does.]

- [Risk 1] — [Recommended response]
- [Risk 2] — [Recommended response]

---

## Looking Ahead: [Next Quarter]

### Their Priorities
[Based on Backstory activity + account.md strategic context]

### Where We Can Help
[Problem-first framing — their need first, then the capability]

---

## Expansion Opportunities

[Whitespace from ask_sales_ai + playbooks/expansion.md.
Frame each as: their problem → our capability → expected outcome.]

| Opportunity | Their Need | Our Capability | Proposed Next Step |
|-------------|------------|-------------------|-------------------|
| | | | |

---

## Proposed Next Steps

1. [Action — Owner — Date]
2. [Action — Owner — Date]

---

## Discovery Questions to Ask in the Room

1. [Question about their upcoming priorities]
2. [Question about a gap or risk surfaced by Backstory]
3. [Question to open an expansion conversation]
```

---

## Related Skills

- `ops/account-pulse.md` in this skill — Quick pre-QBR signal check (run this first if time is short)
- The main `meeting` skill (this skill's root) — Use the QBR package as context to prep for the meeting itself
- **grill** — Qualify any expansion opportunity surfaced during QBR prep
