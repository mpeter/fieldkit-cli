# Slack search protocol

Use Slack as an attributed source of observations, not as proof of customer intent.
The Slack client is separately installed and authenticated; fieldkit does not
guarantee its path, version, commands, token storage, or workspace permissions.

## Establish access and scope

Confirm the intended workspace, accounts, channels, and date range before reading.
Check the installed client's documented read-only interfaces and authentication
status without printing tokens. Do not extract browser cookies or broaden access
as an automatic recovery step.

Channel membership does not prove a channel is internal-only. Shared channels,
guests, direct messages, and private channels need the same scope and privacy
review as any other source.

## Run a bounded search

Use the client version's supported structured arguments to restrict the query by
account, channel, and date. Set explicit result and page limits plus a finite
timeout. Prefer account-specific channels when their relevance is established;
do not assume organization-specific naming conventions.

Track page or cursor progression. Stop on a repeated cursor, authentication
failure, rate limit, malformed response, or exhausted bound. Preserve partial
status; do not silently interpret incomplete results as a complete empty search.
Fetch thread context only when relevant and within the agreed read scope.

Do not strip arbitrary status lines from supposed JSON or merge stderr into it.
Use a documented machine-readable mode and validate its shape, or treat the
result as text requiring review. A parse failure must not produce an empty
successful result.

## Interpret and retain evidence

Attribute observations to their source and timestamp. A colleague's hypothesis,
a draft, a reaction, and a confirmed customer commitment are different kinds of
evidence. Missing text is not proof that an event is merely a reaction; report
unavailable content unless a permitted follow-up read establishes its type.

An existing cache may be used only with known provenance, coverage, and age.
fieldkit does not guarantee a daily sweep or a complete 48-hour Slack cache.
Refreshing or replacing a cache is a separate approved write; preserve prior
usable data when retrieval is partial or fails.

Keep message bodies, personal identifiers, private channel names, and customer
information out of public issues and release evidence. Retain only the minimum
authorized private context necessary for the user's task.

## Report and write boundaries

Report the query scope, observed time range, completeness, failures, and relevant
findings. Say “no matches in the completed scope” only after a complete read;
otherwise say what was and was not searched.

Searching does not authorize sending, reacting, creating remote drafts, or
editing messages. Present proposed communication for operator review and obtain
explicit authorization for the exact destination and content before any write.
Do not rely on a hook as universal enforcement: the current outbound hook
recognizes selected tool calls and command forms, not every Slack client action.
