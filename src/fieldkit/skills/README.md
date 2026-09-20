# fieldkit Skills

Bundled agent skills for fieldkit users.

## Conventions

- One skill per subdirectory.
- Each skill must include a `SKILL.md` file.
- Manage skills with the `fieldkit skill` CLI group (`list`, `show`, `install`, `eval`, `variables`).

## Product

| Skill | Description |
| ----- | ----------- |
| `draft-review` | fieldkit's outbound-gate workflow. Reviews customer-facing drafts (email, proposal, deck, Slack) against the banned-word list… |
| `grill` | Read-only ClosePlan qualification coaching and cross-portfolio pipeline review using exact native questions and choices… |
| `meeting` | Build a pre-meeting brief with context, agenda, questions, talking points, and objection handling; also batch-preps tomo… |
| `post-meeting` | Capture a meeting, draft follow-up, and stage evidence against exact native ClosePlan questions without writing qualification state… |
| `pursuit-advance` | Advance a pursuit under current stage policy; score-dependent transitions remain pending until native policy exists… |
| `pursuit-auditor` | Audit pursuit frontmatter and timeline risks without interpreting historical local qualification values… |
| `sf-sync` | Refresh core Salesforce opportunity and account data before a call, review, or native ClosePlan qualification read… |
| `slack-digest` | The fieldkit Slack deal-room monitor. Sweeps each account's Slack channels for recent customer questions, team concerns, an… |
| `win-loss` | Capture a structured win/loss debrief from a closed pursuit. Writes a dated lessons-learned entry to memory/lessons-lear… |
| `workstream-discover` | You have an active delivery relationship and want to surface expansion opportunities or stalled workstreams before an ac… |

## Ops

| Skill | Description |
| ----- | ----------- |
| `always-on-guidance` | Provides portable session guidance for proposing documented next steps, recording friction, and preserving local skill edits. Always active, not manually invoked. |
| `companion` | Run as a sidecar agent beside the operator — poll the fieldkit attention feed, act within the declared permission tier, … |
| `contact` | Contact intelligence and CRM hygiene for accounts — look up a person, research a competitor before a call, or discover a… |
| `contract` | Contract lifecycle work for an account — extract terms from a signed agreement, validate a draft against contract const… |
| `followup-draft` | A customer call just ended and you need to send a follow-up email — just the email, not the full post-meeting workflow. … |
| `handoffs` | A session is ending and work needs to continue later with full context. Captures intent, technical decisions, files tou… |
| `humanizer` | AI-generated text needs to go out to a customer but it still sounds robotic, over-hedged, or formulaic. Detects and rewr… |
| `ingest` | New meeting recordings, transcripts, or Gemini notes have arrived and need to be processed into the vault before they ca… |
| `memory-management` | Agent memory is stale, bloated, or missing key context — names, project codenames, and acronyms aren't being decoded cor… |
| `pickup` | Resuming work from a previous session's handoff file in .planning/handoffs/ — picking a session back up after a break, … |
| `pipeline` | Pipeline and portfolio health checks across your accounts — scan for stale or at-risk deals before a leadership review,… |
| `brief` | fieldkit's "what needs my attention" workflow — daily and weekly orientation. Refreshes live sources (SF, Gmail), collects, renders, syncs tasks, and synthesizes. On-demand ops cover week start, week close-out, mid-session refresh, and brief config.… |
| `start` | You're setting up a fieldkit workspace for the first time, or need to reinitialise it after a fresh install. Creates TASK… |
| `task-management` | You're mid-session and need to add, update, or review pursuit action items and daily commitments tracked in TASKS.md. Ma… |
| `task-sync` | Reconcile the TASKS.md managed region with the Google Tasks "fieldkit" list. Google Tasks is the source of truth (operat… |
| `tool-routing` | You need to call an external service — Google Workspace, Backstory, Tavily, Brave, Drive, Gmail, Slack, or GitHub — and … |
