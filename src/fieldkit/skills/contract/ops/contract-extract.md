# Contract Extract

Parse PDF contract documents and produce a structured `contracts.md` file
capturing agreement hierarchy, obligations, commercial terms, rate cards,
legal constraints, and operational guardrails.

Repeatable — run on any account, re-run when documents are updated.

## Gotchas

- **Trigger overlap with similar skills** — check skill names carefully; e.g. this skill vs adjacent skills with similar names
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## Inputs

Required: account name (e.g., `<account-slug>`, `acme-corp`, `shieldins`).

Optional: specific PDF filename(s) to parse. Defaults to all PDFs in
`accounts/<account>/artifacts/`.

## Execution

Groups needed: none — existing contracts and artifacts are read directly and the output is written directly (native file reads/writes).

### Step 1: Discover documents

```
1. ls accounts/<account>/artifacts/   (include any attachments)
2. read accounts/<account>/contracts.md — if present, for delta comparison
3. List documents found — confirm with user before proceeding if > 10 files
```

### Step 2: Read and classify each PDF

For each PDF, use the Read tool (which supports PDF files). Read in batches
of 20 pages per request for large documents.

Classify each document into one of:
- **MSA** — Master Services Agreement
- **NDA** — Non-Disclosure Agreement
- **SOW** — Statement of Work
- **SLMA** — Service Level Master Agreement
- **Amendment** — Amendment to an existing agreement
- **Order Form** — Purchase order, order form, or similar
- **Global Agreement** — Parent/umbrella agreement (e.g., IBM Global Agreement)
- **Other** — Describe the type

Record for each document:
- Filename
- Document type
- Parties
- Effective date
- Expiration / renewal terms
- Parent agreement (if amendment or SOW)

### Step 3: Extract structured intelligence

For each document, extract the following sections where applicable:

**Commercial Terms**
- Rate cards, labor categories, hourly/daily rates
- Travel & expense policies
- Payment terms (Net-30, Net-45, etc.)
- Currency and invoicing requirements
- Minimum/maximum commitment values

**Provider obligations**
- Delivery obligations and acceptance criteria
- Insurance requirements
- Reporting and audit rights granted to customer
- Data handling and security obligations
- Background check requirements
- Subcontracting restrictions

**Obligations — Customer**
- Access and resource commitments
- Decision timelines
- Payment obligations
- Information and materials to be provided

**Legal Constraints**
- Liability caps (per-incident, aggregate, carve-outs)
- Indemnification terms (mutual vs. one-way, IP, data breach)
- Limitation of liability exclusions
- Warranty disclaimers and limitations
- Governing law and jurisdiction
- Dispute resolution (arbitration, mediation, litigation)

**IP and Confidentiality**
- Work product ownership (who owns deliverables)
- Pre-existing IP treatment
- License grants (to customer, from customer)
- Confidentiality duration and exceptions
- Data return/destruction obligations

**Change and Termination**
- Change order mechanics (how to modify scope)
- Termination for convenience (notice period, wind-down)
- Termination for cause (cure period, triggers)
- Survival clauses (what persists after termination)

**Operational Guardrails**
- Key personnel requirements
- Subcontractor approval process
- Customer site policies and access
- Security clearance or certification requirements
- Approved communication channels

### Step 4: Build agreement hierarchy

Map how documents relate:
```
Global Agreement (IBM)
└── MSA (provider ↔ customer)
    ├── Amendment 1 — modifies Section X
    ├── SLMA — service levels
    └── SOW(s) — individual engagements
NDA — standalone
```

### Step 5: Flag high-impact terms

Apply RED / YELLOW / GREEN classification:

- **RED — Hard constraint:** Terms that block or strictly limit what we can
  propose. Must be honored exactly. Examples: liability caps, subcontracting
  prohibitions, IP assignment clauses, non-compete restrictions.

- **YELLOW — Caution required:** Terms that don't block but shape how we
  scope and price. Deviation requires legal review. Examples: insurance
  minimums, background check timelines, rate card ceilings, change order
  approval chains.

- **GREEN — Standard/favorable:** Terms that align with our typical operating
  model. No special handling needed.

### Step 6: Generate SOW drafting checklist

From the extracted terms, produce a checklist of items any new SOW must
address or reference:

```markdown
## SOW Drafting Checklist

- [ ] Reference MSA Section X for liability cap ($X aggregate)
- [ ] Include rate card from Amendment 1 Exhibit B
- [ ] Add background check clause per MSA Section Y
- [ ] Use change order form from SLMA Appendix C
- [ ] Insurance certificates due 30 days before start
```

### Step 7: Write contracts.md

Write the file `accounts/<account>/contracts.md` directly, with this structure:

```markdown
---
account: <account>
last_extracted: YYYY-MM-DD
documents_parsed:
  - filename: "doc.pdf"
    type: MSA
    effective_date: YYYY-MM-DD
---

# <Account Name> — Contract Intelligence

## Agreement Hierarchy
[from Step 4]

## High-Impact Terms
[RED/YELLOW/GREEN flags from Step 5]

## Commercial Terms
[consolidated from Step 3]

## Provider obligations
[consolidated from Step 3]

## Obligations — Customer
[consolidated from Step 3]

## Legal Constraints
[consolidated from Step 3]

## IP and Confidentiality
[consolidated from Step 3]

## Change and Termination
[consolidated from Step 3]

## Operational Guardrails
[consolidated from Step 3]

## SOW Drafting Checklist
[from Step 6]

## Source Documents
[table of all parsed documents with type, dates, parties]
```

### Step 8: Cross-reference with SOW language guide

Read `playbooks/sow-language-guide.md` and flag any terms in the contract
that interact with the language guide's avoidance list. Add a section:

```markdown
## Language Guide Interactions
- MSA Section X uses "ensure" — our SOWs must use "provide reasonable assurance" per language guide
- SLMA uses "best practices" — define explicitly per language guide template
```

## Output

- `accounts/<account>/contracts.md` — full structured extract
- Terminal summary of RED flags and document count

## Related Skills

- `/grill` — Feeds Paper Process element
