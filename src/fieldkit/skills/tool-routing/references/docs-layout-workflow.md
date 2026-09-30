---
name: create-google-doc-with-layout
description: Create and verify a structured Google Doc through an available gws CLI.
---

# Create a Google Doc with layout

This workflow requires a separately installed `gws` CLI, an authenticated Google
account with the necessary scopes, and authorization for the exact document.
Inspect the installed `docs.documents.get`, `docs.documents.batchUpdate`, and
`drive.files.export` leaves before composing arguments.

Use `gws schema docs.documents.get` and
`gws schema docs.documents.batchUpdate` only when those schema reads complete
successfully. Do not add recursive reference expansion. If the CLI cannot return
a usable schema, leave the edit pending rather than guessing request fields.

## Safe edit cycle

1. Read the exact document and record its current tab identifiers, structure,
   indices, and relevant text.
2. Resolve an ambiguous document or tab with the operator.
3. Build a minimal request. Insert heading text separately from body text so a
   paragraph style cannot cascade into unrelated content.
4. Insert a list as one contiguous block, then reset the following paragraph to
   normal text. Include the terminating newline when styling a paragraph.
5. After inserting a table, read the document again before addressing cells;
   earlier insertions change later indices.
6. Show the target and proposed request. Obtain authorization before the update.
7. Apply the update through the documented `gws docs documents batchUpdate`
   leaf, preserving the exit status and response.
8. Read the document again and compare its structure with the approved request.
9. When visual layout matters, export through the documented Drive file-export
   leaf and inspect the resulting PDF before declaring success.

Multi-tab documents require the correct tab identifier in every range. Do not
reuse stale indices, combine differently styled paragraphs into one insertion,
or include literal numbering when applying a numbered-list preset. An API
success without structural read-back and required visual inspection is pending,
not verified.
