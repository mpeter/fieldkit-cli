# Browser CLI

Use `chrome-use` for browser automation. It drives the user's logged-in Chrome
profiles without mcpjungle.

Load its version-matched instructions before an unfamiliar operation:

```bash
chrome-use skills get core --full
```

Typical flow:

```bash
chrome-use open <url>
chrome-use snapshot
chrome-use click <selector-or-ref>
chrome-use fill <selector-or-ref> <text>
chrome-use expect <condition>
```

Use `chrome-use browsers` and `--browser <id-or-email>` when profile identity
matters. Verify navigation, stored values, downloads, or other requested outcomes;
a successful click alone is not completion.
