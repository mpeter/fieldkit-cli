## Context

`_ShadowbotConfig` in `config/_schema.py` already enumerates the section's
keys (`api_base`, `token_endpoint`, `auth_endpoint`, `client_id`,
`assistant_id`, `chrome_cookies_path`) with `extra="ignore"`, and
`_FieldkitConfig` documents that unknown keys stay valid for operator
extensions. `DoctorResult` carries `service`, `healthy`, `configured`, and
`message`, with no place for advisory findings. `shadowbot/auth.py` defaults
to `~/.config/google-chrome/Default/Cookies`, and its errors either omit the
path (`login_required`) or embed it absolutely (missing file, symlink).

## Goals / Non-Goals

### Goals
- Detect the misconfiguration from #95 without breaking the extension policy.
- Make the Chrome-fallback failure diagnosable from its message alone.
- Remove absolute paths from these error messages.

### Non-Goals
- Strict validation of the whole configuration file or of other integrations.
- Auto-migrating misplaced keys, or reading `shadowbot_chrome_cookies_path`
  as an alias. Accepting the alias would create a second spelling to support
  indefinitely.
- Changing Chrome cookie decryption or path validation rules.

## Decisions

1. **Schema-derived key set.** `shadowbot_config_warnings(data: Mapping) ->
   tuple[str, ...]` in `config/_shadowbot.py` compares keys against
   `_ShadowbotConfig.model_fields`. No second list of keys exists.
2. **Warnings beside health, not instead of it.** `DoctorResult` gains
   `warnings: tuple[str, ...] = ()`. Text rendering prints each as an indented
   `warning:` line; JSON adds a `warnings` array. `healthy` and the exit-code
   logic ignore it.
3. **Profile label helper.** `_cookie_db_label(path, source) -> str` in
   `shadowbot/auth.py` returns `"<parent dir name> (<source>)"`. Every
   cookie-related `ShadowbotAuthError` uses it, which covers the
   `login_required`, missing-file, and symlink messages in one place. Raw
   cookie and token material stay excluded, as the existing security
   constraint requires.
4. **Log once per process.** Auth logs configuration warnings once, guarded by
   a module-level flag, so repeated token refreshes in a long-running watcher
   don't flood logs. The new cached state must be reset by
   `clear_config_caches()` so tests stay isolated.

## Risks / Trade-offs

- **Noise for intentional extension keys.** An operator using a
  `shadowbot_*` top-level key for their own tooling will see a warning. That
  is acceptable: the prefix collides with fieldkit's namespace, and the
  warning is non-fatal.
- **Profile names can be personal.** A Chrome profile directory is usually
  `Default` or `Profile N`; custom names are possible but are not home paths,
  usernames, or credentials.
