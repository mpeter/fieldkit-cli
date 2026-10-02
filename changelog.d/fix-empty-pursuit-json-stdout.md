### Keep empty pursuit JSON output parseable

Empty audit, health, and forecast results now send their diagnostic to stderr while preserving exit status 3 and leaving JSON stdout empty.
