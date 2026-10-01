### Update oauthlib to 4.0.0 for Google sign-in

Installations with the Google integration now lock oauthlib 4.0.0, which
requests-oauthlib and google-auth-oauthlib use for the browser sign-in that
`fieldkit auth google` starts. The release's two breaking changes affect only
OAuth servers (JSONP removed from token revocation, and grant validation
reordered). The sign-in flow, its PKCE protection and token refresh continue to
work, and no action is needed.
