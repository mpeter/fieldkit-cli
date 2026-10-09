### Make Tach test selection deterministic (#73)

Tach impact selection now uses non-overlapping source roots, so local test selection is deterministic. Hook import boundaries remain enforced by a dedicated check in local and hosted gates.

Hosted PR checks now run the whole test suite in parallel instead of an impact selection, so a selection gap can no longer skip tests while the check passes. Tach selection remains the fast local loop.
