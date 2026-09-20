# Memory File Formats

## CLAUDE.md (Working Memory, ~50-80 lines)

Use tables for compactness.

```markdown
# Memory

## Me
[Name], [Role] on [Team]. [One sentence about what I do.]

## People
| Who | Role |
|-----|------|
| **Todd** | Todd Martinez, Finance lead |
| **Sarah** | Sarah Chen, Engineering (Platform) |
→ Full list: memory/glossary.md, profiles: memory/people/

## Terms
| Term | Meaning |
|------|---------|
| PSR | Pipeline Status Report |
| P0 | Drop everything priority |
→ Full glossary: memory/glossary.md

## Projects
| Name | What |
|------|------|
| **Phoenix** | DB migration, Q2 launch |
→ Details: memory/projects/

## Preferences
- 25-min meetings with buffers
- Async-first, Slack over email
```

## memory/glossary.md

Complete decoder ring — everyone, every term, every codename.

```markdown
# Glossary

## Acronyms
| Term | Meaning | Context |
|------|---------|---------|
| PSR | Pipeline Status Report | Weekly sales doc |

## Internal Terms
| Term | Meaning |
|------|---------|
| standup | Daily 9am sync in #engineering |

## Nicknames → Full Names
| Nickname | Person |
|----------|--------|
| Todd | Todd Martinez (Finance) |

## Project Codenames
| Codename | Project |
|----------|---------|
| Phoenix | Database migration |
```

## memory/people/{name}.md

```markdown
# Todd Martinez

**Also known as:** Todd, T
**Role:** Finance Lead
**Team:** Finance
**Reports to:** CFO (Michael Chen)

## Communication
- Prefers Slack DM
- Quick responses, very direct

## Context
- Handles all PSRs and financial reporting
- Key contact for deal approvals over $500k
```

## memory/projects/{name}.md

```markdown
# Project Phoenix

**Codename:** Phoenix
**Also called:** "the migration"
**Status:** Active, launching Q2

## What It Is
Database migration from legacy Oracle to PostgreSQL.

## Key People
- Sarah - tech lead
- Todd - budget owner
```

## memory/context/company.md

```markdown
# Company Context

## Tools & Systems
| Tool | Used for | Internal name |
|------|----------|---------------|
| Slack | Communication | - |
| Salesforce | CRM | "SF" or "the CRM" |

## Teams
| Team | What they do | Key people |
|------|--------------|------------|
| Platform | Infrastructure | Sarah (lead) |

## Processes
| Process | What it means |
|---------|---------------|
| Weekly sync | Monday 10am all-hands |
```

## What Goes Where

| Type             | CLAUDE.md (Hot Cache)     | <fieldkit_home>/memory/ (Full Storage)                        |
| ---------------- | ------------------------- | ----------------------------------------------------------- |
| Person           | Top ~30 frequent contacts | `personal/contacts/contact_<name>_<account>.md` (pipeline) |
| Acronym/term     | ~30 most common           | `system/lessons-learned.md` (as relevant context)          |
| Project          | Active projects only      | account pursuit/project files                               |
| Preferences      | All preferences           | -                                                           |
| Historical/stale | Remove                    | Keep in `system/lessons-learned.md`                         |

## Conventions

- **Bold** terms in CLAUDE.md for scannability
- Keep CLAUDE.md under ~100 lines (the "hot 30" rule)
- Filenames: lowercase, hyphens (`todd-martinez.md`)
- Always capture nicknames and alternate names
- Glossary tables for easy lookup
