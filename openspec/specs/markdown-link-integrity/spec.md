# markdown-link-integrity Specification

## Purpose
Define the current behavioral contract for markdown-link-integrity, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Public repository and shipped Markdown links are in scope

The relative-link checker SHALL cover root `AGENTS.md`, reviewed scoped domain
`AGENTS.md` guides, public documentation Markdown, and shipped skill Markdown.

#### Scenario: Shipped skill contains a broken relative link

- **WHEN** a Markdown link under `src/fieldkit/skills` resolves to no repository file
- **THEN** the relative-link check fails

#### Scenario: Public contributor guidance contains a broken link

- **WHEN** root `AGENTS.md` links to a repository path that does not exist
- **THEN** the relative-link check fails

### Requirement: Pull requests cannot bypass relative-link validation

Bounded and full local quality SHALL execute the relative-link checker. Every pull request SHALL execute it in an existing required status context, including pull requests whose changes are classified as documentation-only.

#### Scenario: Developer runs bounded quality

- **WHEN** `make quality` runs
- **THEN** it executes the expanded relative-link check as a timed stage

#### Scenario: Documentation-only pull request has a broken link

- **WHEN** a pull request changes only covered Markdown and introduces a broken relative link
- **THEN** the required `Lint (ruff)` context fails

#### Scenario: Documentation-only pull request has valid links

- **WHEN** a pull request changes only covered Markdown and all relative links resolve
- **THEN** the link step passes
- **AND** Python dependency synchronization and code-only checks remain skipped

### Requirement: Link validation remains deterministic and local

The gate SHALL use the pinned upstream Markdown parser and pinned runtime toolchain. HTTP and HTTPS targets SHALL remain excluded from validation, and inline code spans SHALL remain outside the link contract.

#### Scenario: Document contains an external URL

- **WHEN** a covered Markdown file links to an HTTP or HTTPS target
- **THEN** the relative-link gate does not make a network request for that target

#### Scenario: Inline code resembles a path

- **WHEN** a covered Markdown file contains a backticked repository path that is not a Markdown link
- **THEN** this gate does not treat it as a link target
