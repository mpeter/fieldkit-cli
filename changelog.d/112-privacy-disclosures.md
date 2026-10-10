### Document Backstory's data destination and ShadowBot's Chrome recovery (#112)

The privacy guide now lists Backstory. The `backstory-health` watcher sends configured account names, through your MCP gateway, to People.ai's fixed endpoint.

The ShadowBot guide now says that `fieldkit auth shadowbot`, `fieldkit doctor`, `fieldkit doctor shadowbot`, and every `shadowbot` command can read your Chrome cookie store when a stored refresh token is rejected, and send the login domain's session cookies to the configured authorization endpoint. This happens whenever the `chrome-auth` dependencies are installed, including through the `all` profile.
