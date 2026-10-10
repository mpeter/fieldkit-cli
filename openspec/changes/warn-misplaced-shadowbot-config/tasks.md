## 1. Configuration check

- [ ] 1.1 Add `shadowbot_config_warnings()` to `src/fieldkit/config/_shadowbot.py`, deriving known keys from `_ShadowbotConfig.model_fields`
- [ ] 1.2 Add parameterized unit tests: top-level `shadowbot_chrome_cookies_path`, misspelled in-section key, unrelated top-level key (no warning), correct config (no warning), absent section (no warning)

## 2. Doctor surface

- [ ] 2.1 Add `warnings: tuple[str, ...] = ()` to `DoctorResult` in `commands/doctor/_result.py`, render it in text and JSON, and leave health and exit logic unchanged
- [ ] 2.2 Populate warnings in `check_shadowbot()` for both the configured and not-configured paths
- [ ] 2.3 Add CLI tests for `fieldkit doctor shadowbot --json` and `fieldkit doctor`, asserting the warning text and an unchanged exit status

## 3. Auth messages

- [ ] 3.1 Add `_cookie_db_label()` to `shadowbot/auth.py` and use it in the `login_required`, missing-file, and symlink errors
- [ ] 3.2 Log configuration warnings once at WARNING when resolving the cookie path; reset the once-flag in `clear_config_caches()`
- [ ] 3.3 Add tests asserting each message names the profile directory and source and contains no absolute path, using `tmp_path` profile directories `Default` and `Profile 2`

## 4. Finish

- [ ] 4.1 [P] Add `changelog.d/95-shadowbot-config-warnings.md`
- [ ] 4.2 [P] Update the ShadowBot configuration guide to show the nested key and the new warning
- [ ] 4.3 Run `make pr-check`, and `make docs` if `doctor` help text changes
- [ ] 4.4 Verify constitution alignment: warnings are present in `doctor --json` (III); key checking and message formatting are tested with synthetic mappings and `tmp_path` (IV)
