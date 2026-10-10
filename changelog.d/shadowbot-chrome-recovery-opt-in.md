### Make ShadowBot Chrome cookie recovery opt-in

ShadowBot no longer reads your Chrome cookie store unless you enable it. Previously, `fieldkit auth shadowbot`, `fieldkit doctor`, and every `shadowbot` command silently decrypted Chrome session cookies whenever a stored refresh token was rejected and the `chrome-auth` dependencies were installed, including through the `all` profile.

**Action:** if you relied on automatic Chrome recovery, add `chrome_recovery: true` under `shadowbot:` in `config.yaml`. Otherwise an expired refresh token now exits 2 and asks for a new one. `fieldkit doctor` warns when `chrome_cookies_path` is set without the opt-in.
