# CU Redemption Form Generator

Generate consulting-unit redemption forms for professional-services engagements using account-specific templates.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today; this skill reads from cached files
- **Trigger overlap with adjacent skills** — check that you need this specific skill and not a closely named one (e.g. contract-check vs contract-extract)

## Constraints

- **Never write to account files without explicit confirmation**
- **Do not modify pursuit frontmatter mid-workflow** — only write at designated save steps
- **Always surface output for review before sending externally**

## Overview

CU Redemption Forms are executed alongside purchase orders for consulting engagements paid via Consulting Units. The form has two main sections:
- **Exhibit A - Redemption Form**: Client info, Redemption Summary table, signatures
- **Appendix 3, Exhibit 3.A - Professional Services**: Scope, task list, T&C boilerplate

## When to Use

- User has a consulting scope of work and needs the redemption paperwork
- User mentions "CU Redemption Form", "instant CU redemption", or "consulting units"
- User references a previous redemption form as a template

## Required Inputs

Ask the user for:

1. **Template document** — Path to a previous redemption form for this account (establishes structure and boilerplate)
2. **Scope of work** — As text, Google Doc URL, or file containing:
   - Staffing plan (roles, hours, names optional)
   - Task list or workstreams
   - Term dates (start/end)
   - Out of scope items (optional)
   - Assumptions/responsibilities (optional)
3. **Reference info** (if available):
   - Quote reference number
   - Redemption form expiration date
   - Opportunity number

## Workflow

### Step 1: Extract Template Structure

Read the template document to understand:
- Document layout and formatting
- Boilerplate text in sections 1-3 and 4.2-4.5
- Client contact information
- Standard tables and signatures

Use the `docx` skill's `python-docx` tooling to read the template structure.

### Step 2: Extract Scope Components

From the scope of work document/text, identify:
- **Staffing table** — roles, quantities, hours
- **Task list** — workstreams, sub-tasks, bullet structure
- **Term dates** — start and end dates for the engagement
- **Travel & Expenses** — dollar amount if mentioned (optional)
- **Out of scope items** — if documented
- **Client responsibilities and assumptions** — if documented

### Step 3: Calculate CUs

Use the **standard CU Redemption Rate table** (from template or use this reference):

| Role | CUs per Hour |
|------|--------------|
| Associate Consultant | 2.11 |
| Consultant | 2.60 |
| Senior Consultant | 3.30 |
| Principal Consultant | 3.89 |
| Senior Principal Consultant | 4.15 |
| Associate Architect | 3.50 |
| Architect | 3.79 |
| Senior Architect | 4.11 |
| Principal Architect | 4.49 |
| Senior Principal Architect | 4.70 |
| Associate Project Manager | 2.11 |
| Project Manager | 2.81 |
| Senior Project Manager | 3.14 |
| Delivery Lead | 3.14 |
| Program Manager | 3.68 |
| Managing Director | 6.10 |

**For each role in staffing:**
```
Total CUs = Hours × CU Rate
```

**For Travel & Expenses:**
If the scope specifies a dollar amount, divide by $80 (standard CU price) to get CU count.

**Example calculation:**
- 480 hours Senior Architect @ 4.11 CU/hr = 1,972.80 CUs
- $37,800 Travel ÷ $80/CU = 472.50 CUs

### Step 4: Generate Document

Use `python-docx` to create the new document. Follow these critical requirements:

**Page setup:**
```python
from docx import Document
from docx.shared import Inches, Pt, Emu
from docx.enum.section import WD_ORIENT

doc = Document()
section = doc.sections[0]
section.page_width = Emu(12240 * 914)   # US Letter
section.page_height = Emu(15840 * 914)
section.top_margin = Inches(1)
section.bottom_margin = Inches(1)
section.left_margin = Inches(1)
section.right_margin = Inches(1)
```

**Tables:**
- Set column widths explicitly via `column.width = Inches(...)` or `Emu(...)`
- Use `cell.paragraphs[0].alignment` for cell text alignment
- Apply shading to header cells via `cell._element` XML manipulation or the `docx` skill's helper
- Set cell margins via cell format properties

**Lists:**
- Use `paragraph.style = 'List Bullet'` or `'List Bullet 2'` for nested bullets
- Never use Unicode bullets directly in text

**What to update:**
1. **Redemption Summary table** — populate from staffing + CU calculations
2. **Task List (section 4.1)** — insert from scope of work

**What to preserve from template:**
1. All boilerplate text (sections 1, 2, 3, "Redemption" prose, CU Redemption Rate table)
2. Sections 4.2-4.5 (Out of Scope, Management, Customer Engagement Report, Client Responsibilities)
3. Client contact information (unless user provides updates)
4. Signature blocks
5. Applicable Agreement and Appendices tables

### Step 5: Validate

After generating, validate the document:

```bash
python ~/.agents/skills/docx/scripts/office/validate.py output.docx
```

If validation fails, unpack, inspect the XML, fix errors, and repack.

### Step 6: Deliver

Save to `accounts/<account>/proposals/<pursuit-name>-cu-redemption-DRAFT.docx`

Present to user with:
- File path
- Total CUs calculated
- Placeholder fields that need updating (Quote #, expiration date, etc.)

## Critical Formatting Rules

From the docx skill documentation:

1. **Never use `\n` for line breaks** — use `doc.add_paragraph()` for each line
2. **Page breaks** — use `doc.add_page_break()` or `paragraph.add_run().add_break(WD_BREAK.PAGE)`
3. **Table widths** — set explicitly via `Inches()` or `Emu()`; do not rely on auto-fit
4. **Use `python-docx` throughout** — do not use Node.js, `docx-js`, or npm packages
5. **Set all format-critical parameters explicitly** — page size, margins, table widths

## Example Staffing Input

```
Role: Delivery Lead, Qty: 1, Hours: 480
Role: Principal Consultant, Qty: 1, Hours: 480  
Role: Senior Architect, Qty: 1, Hours: 480
Role: Senior Consultant, Qty: 4, Hours: 1920
Travel & Expenses: $37,800
```

## Example Output Structure

```
EXHIBIT A - REDEMPTION FORM
├── Client Information table
├── Redemption Summary table (← UPDATED from staffing)
├── Applicable Agreement table
├── Applicable Appendices table
├── Additional Terms
├── Redemption paragraph
├── CU Redemption Rate table
└── Signature blocks

[Page Break]

APPENDIX 3, EXHIBIT 3.A - PROFESSIONAL SERVICES
├── Introduction
├── Section 1: Redemption
├── Section 2: Change Requests
├── Section 3: Travel and Expenses
├── Section 4.1: Task List (← UPDATED from scope)
├── Section 4.2: Out of Scope (← from template)
├── Section 4.3: Management (← from template)
├── Section 4.4: Customer Engagement Report (← from template)
└── Section 4.5: Client Responsibilities (← from template)
```

## Common Mistakes to Avoid

1. **Don't use percentage-based widths** — breaks in Google Docs; always use absolute widths
2. **Don't manually insert bullet characters** — use `'List Bullet'` paragraph styles
3. **Don't modify sections 4.2-4.5** — those are template boilerplate specific to the prior engagement
4. **Don't forget cell padding** — tables without padding look cramped
5. **Don't skip validation** — catches XML errors before delivery

## Files This Skill May Reference

- Template document: Previous CU Redemption Form (user provides path)
- Scope of work: Google Doc, local file, or text
- CU rate table: Standard rates (embedded in this skill)

## See Also

Skills: docx
Playbooks: playbooks/pricing.md (for CU pricing context)
