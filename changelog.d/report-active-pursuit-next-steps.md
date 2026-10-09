### Report missing actions across active pursuit stages (#71)

`fieldkit pursuit audit` now warns once when any active pursuit lacks a recorded
next action, independently of its close date. Canonical `sf_next_steps` presence
wins over legacy fallback; non-text values produce an error. Pre-pipeline and
terminal stages are excluded. Local snapshot and qualification-unavailable
semantics remain unchanged; JSON audit writes no report or pursuit changes.
