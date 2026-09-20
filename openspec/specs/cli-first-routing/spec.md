# cli-first-routing Specification

## Purpose
Define the current behavioral contract for cli-first-routing, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: CLI-covered services route through CLI first

The shipped tool-routing guidance SHALL select an installed CLI for every capability
with live CLI parity and SHALL NOT require mcpjungle for those operations.

#### Scenario: Google Workspace operation

- **WHEN** an agent needs Gmail, Drive, Docs, Sheets, Calendar, Contacts, Slides,
  Tasks, Forms, Chat, or Apps Script
- **THEN** the routing guidance selects the corresponding `gws` service
- **AND** it may select a purpose-built `fieldkit` command first when that command
  provides the requested account-centric workflow

#### Scenario: Browser and common external tools

- **WHEN** an agent needs browser automation, Tavily research, Slack, GitHub,
  ordinary public code search, or ordinary vault retrieval
- **THEN** the guidance selects `chrome-use`, `tvly`, `slackcli`, `gh`/Git, or native
  files/Git/`qmd` respectively
- **AND** no gateway preflight is required

#### Scenario: Cross-repository public code-pattern search

- **WHEN** an agent needs grep.app's cross-repository pattern index rather than an
  ordinary GitHub operation or code search
- **THEN** the guidance may select direct global `gh_grep`
- **AND** does not require an mcpjungle preflight

### Requirement: MCP-only routes carry a necessity boundary

Every retained MCP route SHALL identify the exact capability unavailable through the
installed CLI set and SHALL scope MCP use to that capability.

Each retained route SHALL use a machine-checkable `MCP-NECESSITY` marker. Source
tests SHALL reject missing or duplicate markers, root-skill routes outside the
approved exception set, blanket gateway prerequisites, retained routes outside
their approved capability files, or CLI-covered MCP instructions anywhere in the
shipped skill tree.

#### Scenario: Gateway and direct routes

- **WHEN** an agent needs Backstory/Product Pages, Rover/Snowflake analytics,
  authenticated Jira data, Brave's independent index, Context7's curated corpus, or
  grep.app's cross-repository code-pattern index
- **THEN** the guidance names the relevant gateway group or direct global MCP
- **AND** states why the available CLI routes are not equivalent
- **AND** requires mcpjungle preflight only for `fieldkit-sales` and
  `fieldkit-dataverse`

#### Scenario: Retired vault graph query

- **WHEN** an agent needs backlinks, outlinks, orphan detection, or a connection
  path
- **THEN** the guidance states that no maintained graph backend exists
- **AND** does not route to the removed `fieldkit-markdown` group
- **AND** ordinary vault reads, writes, search, and history remain routed through
  native files, Git, or `qmd`

### Requirement: Routing regressions are evaluated

The tool-routing eval corpus SHALL fail when representative CLI-covered prompts
select an MCP route and SHALL retain positive coverage for justified MCP-only
exceptions.

#### Scenario: CLI-first behavioral evaluation

- **WHEN** the behavioral eval corpus judges representative CLI-covered and
  MCP-only prompts
- **THEN** each assertion distinguishes the primary CLI route from the narrow
  exception route

#### Scenario: MCP exception inventory validation

- **WHEN** the root skill adds, removes, or renames a retained MCP route
- **THEN** the source test compares gateway groups and direct global servers to the
  approved five-route exception set
- **AND** requires exactly one matching `MCP-NECESSITY` marker for every route

#### Scenario: Linked instruction-tree validation

- **WHEN** any shipped skill, linked reference, operation, workflow, or eval file
  contains an executable MCP route for a CLI-covered service
- **THEN** the whole-tree source test fails
- **AND** stale guidance cannot survive behind a corrected root table

#### Scenario: Capability-scoped retained route

- **WHEN** a shipped instruction or eval references one of the five retained MCP
  routes
- **THEN** the whole-tree source test requires that reference to occur in its
  approved capability file set
- **AND** ordinary web search routes through `tvly`, including generated
  contact-enrichment handoffs
