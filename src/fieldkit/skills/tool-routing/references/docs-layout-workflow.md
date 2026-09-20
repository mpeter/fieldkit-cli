---
name: create-google-doc-with-layout
description: Use when creating a Google Doc with headings, lists, tables, and body text through gws.
---

# Create a Google Doc with layout

Use the Docs API through `gws`. Inspect each method schema before composing the
request body:

```bash
gws schema docs.documents.get --resolve-refs
gws schema docs.documents.batchUpdate --resolve-refs
```

## Safe edit cycle

1. Read the document with `gws docs documents get` and record current indices.
2. Build a small `batchUpdate` request. Insert heading text separately from body
   text so paragraph styles do not cascade.
3. Apply named paragraph styles only to the intended ranges.
4. Insert a list as one contiguous block, then reset the following paragraph to
   normal text.
5. After inserting a table, re-read the document before addressing cell ranges;
   fill shifting ranges from the end toward the beginning.
6. Run `gws docs documents batchUpdate` with the reviewed request body.
7. Read the document again and confirm the requested structure.
8. Export through Drive and inspect the PDF when visual layout matters.

```bash
gws docs documents get --params '{"documentId":"<document-id>"}'
gws docs documents batchUpdate --params '{"documentId":"<document-id>"}' --json '<requests-json>'
gws drive files export --params '{"fileId":"<document-id>","mimeType":"application/pdf"}' --output <output.pdf>
```

## Index and style rules

- Every insertion changes later indices; never reuse stale positions.
- Include the terminating newline when applying a paragraph style.
- Do not insert heading and body paragraphs in one operation if they need
  different named styles.
- Do not include literal numbering when applying a numbered-list bullet preset.
- Multi-tab documents require the correct tab identifier in each range.
- A successful API response is not visual verification; inspect the exported
  artifact before claiming layout completion.
