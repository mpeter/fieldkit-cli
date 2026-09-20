# Stakeholder Mapping Skill

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Purpose

Build a buying-committee map: who they are, what they care about, how they feel
about us, and where our coverage gaps are. Output is actionable — every entry
leads to a next step.

## Data Sources (use all available)

Routes needed: `gws gmail` (Gmail threads), **fieldkit-sales** (Backstory MCP exception), `gws calendar` (meeting attendees), and `tvly` (web research). Account files are read and edited directly on disk.

1. **`account.md`** — read `accounts/<account>/account.md` — existing stakeholder info
2. **Gmail threads** (`gws gmail`) — search for contacts at the account domain
3. **Backstory** (fieldkit-sales) — relationship health scores, last contact, engagement depth
4. **Google Calendar** (`gws calendar`) — past meeting attendees at the account
5. **Slack** — `slackcli` search for account mentions
6. **Web search** (`tvly search`) — LinkedIn profiles, org announcements, executive bios
7. **Salesforce** (via `fieldkit sf opportunity <id>`) — contact roles on the opportunity

Prioritize Backstory MCP + Gmail over Tavily for current status.
Use the `tvly` CLI to fill in bio/background on new contacts.

### Slack Intelligence

Search Slack for account and stakeholder mentions:

```
1. slackcli search messages "<stakeholder name>" --limit 20
   → mentions of specific contacts — informal context, team assessments, relationship signals

2. For any unknown users:
   slackcli search people "<name or email>"
   → resolve to name/email for matching to stakeholder records
```

Slack surfaces context that doesn't appear in formal emails: team impressions
of a stakeholder's stance, informal notes about who's blocking or championing.

---

## Stakeholder Roles to Map

Roles align with Salesforce Contact Roles (which feed Backstory/People.AI data).
For each stakeholder, assign one or more of these roles:

| Role                    | SF Contact Role    | Definition                                                       |
| ----------------------- | ------------------ | ---------------------------------------------------------------- |
| **Economic Buyer (EB)** | Economic Buyer     | Controls the budget. Final yes/no authority.                     |
| **Technical Buyer**     | Technical Buyer    | Evaluates technical fit. Can veto but not approve alone.         |
| **Champion**            | Champion           | Advocates for us internally. Has credibility + EB access.        |
| **Influencer**          | Influencer         | Shapes the decision but doesn't own budget or technical veto.    |
| **End User**            | End User           | Will use or manage the solution day-to-day.                      |
| **Adoption Lead**       | Adoption Lead      | Drives internal adoption and change management post-sale.        |
| **Procurement**         | Procurement        | Handles purchasing, vendor onboarding, PO creation.              |
| **Legal**               | Legal              | Reviews contracts, terms, security/compliance requirements.      |
| **Partner Sponsor**     | Partner Sponsor    | Internal sponsor from partner side (IBM, etc.).                  |
| **Accounts Payable**    | Accounts Payable   | Handles invoicing and payment processing.                        |

One person can hold multiple roles (e.g., Champion + Technical Buyer).

### Supplemental Tags (not SF Contact Roles, but useful for deal strategy)

- **Coach** — Gives us insider info but may not advocate openly.
- **Blocker** — Actively working against us or the initiative.
- **Neutral** — Has influence but hasn't engaged.

Use these as supplemental labels in the Support Level column, not as primary roles.

---

## For Each Stakeholder, Capture

```
Name: [Full name]
Title / Function: [title, department]
Role in Deal: [EB / Technical Buyer / Champion / etc.]
Influence Level: [High / Medium / Low]
Support Level: [Strong Supporter / Supporter / Neutral / Skeptic / Blocker]
Last Contact: [date and method — email, meeting, call]
Contact Frequency: [weekly / monthly / sporadic / none]
Key Priorities: [what they care about — in their language, not ours]
Key Concerns: [objections or risks they've raised]
Relationship Owner: [which colleague owns this relationship]
Coverage Gap: [yes/no — are we underweight here?]
Next Step: [specific action to advance this relationship]
```

---

## Output Format

Produce two outputs:

### 1. Stakeholder Table (quick reference)

```
# Stakeholder Map — [Account] — [Opportunity or "Account-Wide"]
Last Updated: YYYY-MM-DD

| Name | Title | Role | Influence | Support | Last Contact | Owner | Gap? |
|------|-------|------|-----------|---------|--------------|-------|------|
| ...  | ...   | ...  | ...       | ...     | ...          | ...   | Y/N  |
```

### 2. Individual Profiles (one per stakeholder)

```
## [Name] — [Title]

**Role in Deal:** [role]
**Influence:** [High/Med/Low]   **Support:** [level]

**Background:** [2–3 sentences — career, tenure, what they're known for]

**What They Care About:** [their top 2–3 priorities in their language]

**Our Position with Them:** [where we stand — honest assessment]

**Risks / Watch-Outs:** [anything that could go wrong]

**Next Step:** [specific action, owned by whom, by when]
```

---

## Coverage Gap Analysis

After mapping all stakeholders, produce a gap summary:

```
## Coverage Gaps

**Economic Buyer:** [name or "NOT IDENTIFIED"] — [last contact or "NO CONTACT"]
**Champion Status:** [name or "NO CHAMPION"] — [strength assessment]
**Uncontacted High-Influence Stakeholders:** [list]
**Single-Threaded Risk:** [yes/no — if yes, who is the single thread and what's the risk]

**Priority Actions to Close Gaps:**
1. [action]
2. [action]
3. [action]
```

---

## After Mapping

1. Update the stakeholder map section by editing the `## Stakeholder Map`
   body section of `accounts/<account>/account.md` in place (a targeted edit
   replacing the old section with the updated map). This is an operator-authored
   body section — `fieldkit sf account` only manages the `sf_*` frontmatter and
   dashboard region, so the map is preserved across regeneration. Never hand-edit
   the frontmatter.
2. For pursuit-specific maps, edit `accounts/<account>/pursuits/<opp>.md` directly.
3. Flag any EB or Champion gap to the user immediately — these are deal blockers.
