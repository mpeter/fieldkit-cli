# Workspace content routes

Workspace Markdown and data files are ordinary local content beneath the
configured fieldkit workspace and data roots. Use native file reads and `rg` for
exact search. Use Git only when the workspace is a repository and history is
actually available.

An optional local index may help with lexical or semantic retrieval when the
operator has configured one, but fieldkit does not install or require an indexer.
Identify the index's coverage and age before treating its result as current.

Respect field ownership and safe writers. Pursuit frontmatter is read and written
through fieldkit's pursuit I/O behavior, multi-writer task state uses its owned
writer, and Salesforce-owned fields change through supported Salesforce commands.
Do not bypass those boundaries with a generic text replacement.

Backlink, outlink, orphan, and connection-path graph operations have no shipped
fieldkit route. Report that limitation instead of inventing a backend. An exact
text search is a different operation and must not be described as graph parity.
