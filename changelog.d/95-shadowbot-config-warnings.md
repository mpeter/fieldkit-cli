### Warn about misplaced ShadowBot configuration keys (#95)

`fieldkit doctor` and `fieldkit doctor shadowbot` now warn when a `shadowbot:` key is unrecognized or a top-level `shadowbot_*` key (such as `shadowbot_chrome_cookies_path`) is ignored, and name the expected `shadowbot.<key>` location. `doctor --json` gains a `warnings` array; health and exit status are unchanged. Chrome recovery errors now name the Chrome profile directory and whether it came from configuration or the default, and no longer print absolute paths.
