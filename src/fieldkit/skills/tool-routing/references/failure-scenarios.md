# Routing failure scenarios

## The executable or tool is absent

Stop and name the missing capability. Explain whether it is optional or required
for the requested operation. Do not invent an endpoint, silently choose a
different data source, or claim that a substitute proves the same fact.

## Authentication or identity is unavailable

Use the tool's credential-safe status interface. Report the account or workspace
only when the status output establishes it. Do not print, export, or decrypt a
token as routine diagnosis. Missing authentication leaves the operation pending
and does not authorize another transport.

## Google Workspace discovery fails

Preserve the command's exit status and safe error text. Retry only a documented,
read-only discovery operation. Use `gws schema SERVICE.RESOURCE.METHOD` without
`--resolve-refs`; some CLI versions can abort while recursively resolving schema
references. A nonzero exit, crash, malformed response, or incomplete request
shape means the operation remains pending. Never guess a write payload.

## A read or search is incomplete

Record the requested scope, returned scope, pagination state, limits, and error.
Do not translate a partial page, repeated cursor, rate limit, parse failure, or
timeout into “no results.” Preserve prior usable state when refresh fails.

## The account or resource is ambiguous

Show the bounded candidates and ask the operator to select the exact identity.
Names, titles, recent timestamps, and list order are not unique identifiers.
Do not proceed with a write until the account and resource are resolved.

## A write result is uncertain

Do not retry blindly. First read the resource's current state because the first
request may have succeeded or another writer may have changed it. Report the
approved value, observed value, and remaining uncertainty. Success requires a
matching read-back or the workflow's stronger artifact verification.

## A named integration is unknown

Check fieldkit's current command registry and the environment's documented tool
inventory. If neither exposes the named capability, report it as unavailable.
Do not revive a historical service, private maintainer route, or removed graph
backend from a stale instruction.
