---
name: contract
description: >
  Contract lifecycle work for an account — extract terms from a signed agreement,
  validate a draft against contract constraints before it goes out, generate CU
  redemption paperwork, or look up pricing and margin for an engagement. Trigger
  with "extract the contract", "parse this SOW", "check this contract", "validate
  the SOW", "is this compliant", "contract review", "redemption form", "generate
  the redemption form", "CU Redemption", "deal desk", "pricing for [role]",
  "what's the rate for [role]", "margin on this deal", "price this engagement",
  "approval needed?", or "how much will this cost?".
metadata:
  opencode/slash: "true"
  category: product
---

# Contract Skill

Contract lifecycle work for an account: parse a signed agreement into structured
intelligence, validate a draft against those terms before it goes out, generate
Consulting Units redemption paperwork, or look up pricing and margin for an
engagement.

Groups needed: none for extract/check/redemption (native file reads/writes); the
`docx` skill's `python-docx` tooling for redemption form generation.

## Folded Ops

This skill absorbs 4 previously-standalone skills as on-demand references. Read the
relevant file when the request matches:

- `ops/contract-extract.md` — parsing signed PDF contracts into `contracts.md`
- `ops/contract-check.md` — validating a draft SOW/proposal against `contracts.md`
- `ops/contract-redemption-form.md` — generating a CU Redemption Form
- `ops/deal-desk.md` — pricing lookup and margin analysis against the rate card

## Gotchas

- **Trigger overlap with adjacent skills** — contract-extract parses signed documents; contract-check validates a draft before sending. Confirm which op matches before proceeding
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

---

Folded from contract-check, contract-extract, contract-redemption-form, deal-desk
(D1 skill taxonomy, Wave 4, PR2).
