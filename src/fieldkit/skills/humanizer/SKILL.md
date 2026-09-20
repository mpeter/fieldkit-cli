---
name: humanizer
description: >
  AI-generated text needs to go out to a customer but it still sounds robotic, over-hedged,
  or formulaic. Detects and rewrites 29 common AI patterns so the text reads like a real
  person wrote it before it lands in someone's inbox.
  Trigger with "humanize this", "remove AI patterns", "make this sound human",
  "this sounds like AI", "polish this text", "clean up this draft", "de-robot this",
  "this is too robotic", "make this more natural", "rewrite this to sound human".
version: "1.0"
metadata:
  opencode/slash: "true"
  category: ops
---

# Humanizer

Remove AI-generated writing patterns from text so it reads like a real person wrote it.
Detects and fixes 29 common patterns that mark text as machine-generated.

## When to Use

- You pasted in AI output and want it to sound natural before sending
- A draft feels robotic, over-hedged, or weirdly formal
- You want a quick scan for tell-tale AI patterns before publishing

## Gotchas

1. **Removing AI patterns does not guarantee the text sounds human** — Eliminating the 29 anti-patterns catches the most egregious AI signals, but the resulting text may still feel mechanical if the underlying ideas are thin or the structure is over-engineered. Review holistically after pattern removal.
2. **Some "AI patterns" are contextually appropriate** — "In conclusion" belongs in a formal essay; numbered lists belong in a how-to. Don't apply pattern removals blindly — apply them where they reflect AI behavior, not where they reflect legitimate structure for the document type.
3. **Em-dash overuse detection requires whole-document counting** — Checking a paragraph in isolation won't catch overuse across the full document. Scan the entire text for em-dash density before flagging individual paragraphs.

## Constraints

- Apply patterns selectively based on context — not every pattern appears in every text, and not every instance is an AI-ism
- Do not alter technical terms, proper nouns, or quoted material while removing patterns
- Return only the revised text unless the user asks for a diff or explanation of changes made

## The 29 AI Patterns

### Tone and Register

1. **Opening with "I" or "Certainly"** — AI defaults to "Certainly!" or "I'd be happy to…" before answering anything. Cut it.
2. **Hollow affirmations** — "Great question!", "Absolutely!", "Of course!" Add no meaning; delete immediately.
3. **Overly formal register** — "I hope this message finds you well", "I trust you are doing well". No human writes this to a colleague.
4. **"As an AI language model" disclaimers** — Self-identifying as AI in every response. Remove entirely.
5. **Unsolicited caveats and disclaimers** — Warnings no one asked for: "I should mention...", "Please consult a professional before...". Remove unless genuinely required.

### Structure Overuse

6. **Numbered lists for everything** — AI turns every answer into a numbered list even when prose flows better. Prose > lists for most explanations.
7. **Excessive bullet points** — Bullets for bullets' sake. Consolidate or rewrite as sentences when there are fewer than 3 related items.
8. **Rhetorical questions as section openers** — "What does this mean for your business?" Answer the question; don't ask it.
9. **"In conclusion" / "In summary" / "To summarize"** — Signals the end of an essay no one asked for. Delete and fold into the final paragraph naturally.
10. **Restating the question before answering** — "You asked about X. X is an interesting topic because…" Just answer.

### Filler and Padding

11. **Excessive hedging** — "It's worth noting that", "It's important to understand that", "One might argue that". Pick a lane and say it.
12. **Unnecessary preamble** — Three sentences of setup before the answer. Start with the answer.
13. **Filler transitions** — "Furthermore", "Moreover", "Additionally" back-to-back. Use one transition per section at most, or none.
14. **Padding phrases** — "It goes without saying that", "Needless to say", "As you may already know". If it goes without saying, don't say it.
15. **Orphaned context** — "As mentioned above", "As previously stated", "As noted earlier". Rewrite so the point stands on its own, or cut.
16. **Generic closing phrases** — "Let me know if you have any questions", "Feel free to reach out", "Don't hesitate to ask". Cut. It's implied.

### Word Choice

17. **"Utilize" instead of "use"** — Always "use". "Utilize" is never more precise; it's just longer.
18. **"Leverage" as a verb** — "Leverage your strengths" → "Use your strengths". Leverage is a noun.
19. **"Delve into" / "dive into"** — Overused AI-isms. Replace with "explore", "examine", or just say what you're doing.
20. **Hyperbolic claims** — "revolutionary", "game-changing", "unprecedented", "transformative", "groundbreaking". Remove unless specifically supported.
21. **Vague intensifiers** — "very", "quite", "rather", "somewhat", "fairly". Delete or replace with a specific qualifier.
22. **Redundant pairs** — "each and every", "first and foremost", "null and void", "true and accurate". Pick one word.
23. **Excessive synonyms for "said"** — "opined", "elucidated", "posited", "articulated". "Said" or "noted" is almost always right.

### Formatting and Mechanics

24. **Em-dash overuse** — One em-dash per paragraph maximum. Multiple em-dashes signal AI generation.
25. **Over-capitalization of Common Nouns** — "the Platform", "the Framework", "the Solution". Lowercase unless it's a proper name.
26. **Oxford-comma inconsistency** — Pick a style and apply it throughout. AI mixes both.
27. **Fake precision** — "approximately 73% of users", "studies show that 84% of…" without a source. Remove the fake number or cite it.

### Paragraph Structure

28. **Starting paragraphs with "This" or "These"** — "This approach…", "These findings…". Name the specific thing instead.
29. **Passive voice overuse** — "It has been determined that…", "This was found to be…". Use active voice: name who did what.

## How to Apply

1. **Read** the text once for overall tone
2. **Scan** for each of the 29 patterns (you don't need to find all 29 — just what's present)
3. **Rewrite** flagged sentences in place — shorter, plainer, more direct.
   **Preserve substantive meaning**: remove the pattern, never the claim,
   commitment, or fact it was wrapped around.
4. **Read aloud** — if it sounds like a robot, keep going

**Check-only mode:** if the user asks whether text sounds like AI without
wanting a rewrite yet, report the specific named patterns found (not a vague
verdict), leave the text untouched, and offer the rewrite as the next step.

### Output Format

Return the rewritten text with no explanation of what changed unless the user asks.
If asked to explain changes, provide a brief diff-style list: `[removed] → [replacement]`.

## Heuristics for Fast Triage

Scan for these strings first — they catch 80% of AI patterns:

```
Certainly, Absolutely, Of course, Great question, It's worth noting,
It's important to, Furthermore, Moreover, Additionally, In conclusion,
In summary, To summarize, As mentioned, As previously, Needless to say,
It goes without saying, Feel free, Don't hesitate, Let me know if,
utilize, leverage, delve into, dive into, revolutionary, game-changing,
unprecedented, As an AI
```

## Related Skills

- `followup-draft` — Drafts follow-up email; run humanizer before sending
- `draft-review` — The outbound gate; humanizer runs before it
