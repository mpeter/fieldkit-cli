### Read the CLI contract from the code checkout in `create-cli`

The installed `create-cli` skill linked to `AGENTS.md` and the exit-code
reference by paths that left the skill, so after `fieldkit skill install` those
links pointed at nothing. It now reads both documents from the fieldkit code
checkout, found with `get_fieldkit_root()`, and reports a missing checkout
instead of drafting a command contract without them.
