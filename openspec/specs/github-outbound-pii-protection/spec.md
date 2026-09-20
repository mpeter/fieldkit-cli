## Purpose

Protect GitHub publication commands from exposing configured account and personal PII.

## Requirements

### Requirement: Inspectable GitHub publication protection

The system MUST inspect publication text before allowing an unattended command
to create or modify a GitHub pull request, issue, or comment through a
supported `gh` or `fieldkit issue` command. It MUST block a payload containing
a configured account name, personal email address, absolute home path, or
personal Slack user handle. The blocking diagnostic MUST identify only a fixed
violation category and payload source; it MUST NOT reproduce matched text.

#### Scenario: Unsafe inline pull request body
- **GIVEN** a Bash command invokes `gh pr create` with a literal body containing
  a configured account name
- **WHEN** the pre-execution policy evaluates the command
- **THEN** it MUST block the command before GitHub receives it and emit no
  account name in the diagnostic

#### Scenario: Safe issue title and body
- **GIVEN** a Bash command invokes `gh issue create` with placeholder-only
  title and body values
- **WHEN** the pre-execution policy evaluates the command
- **THEN** it MUST allow the command

#### Scenario: Unsafe regular body file
- **GIVEN** a supported command references a readable regular UTF-8 body file
  containing a personal email address
- **WHEN** the pre-execution policy reads the bounded file content
- **THEN** it MUST block the command without echoing the email address

### Requirement: Fail closed for indeterminate publication text

For a recognized supported publication command, the system MUST block execution
when it cannot deterministically inspect every text payload that the command
may publish. This includes shell composition, variable or command expansion,
stdin, interactive/editor/template/fill modes, malformed quoting, and unreadable,
non-regular, non-UTF-8, or oversized body files. The diagnostic MUST state the
uninspectable source without exposing payload content. A readable regular body
file MUST also block after classification because the GitHub CLI would reopen its
mutable path after policy evaluation.

#### Scenario: Unclassified GitHub or fieldkit issue command
- **GIVEN** a Bash command invokes an unclassified `gh pr`, `gh issue`, or `fieldkit issue` action
- **WHEN** the pre-execution policy evaluates the command
- **THEN** it MUST block the command without attempting to infer whether the action publishes text

#### Scenario: Interactive GitHub body
- **GIVEN** an agent invokes `gh issue create` without an inspectable body
  source
- **WHEN** the pre-execution policy evaluates the command
- **THEN** it MUST block rather than permit an interactive editor or prompt

#### Scenario: Shell-composed GitHub body
- **GIVEN** an agent invokes a supported GitHub publication command whose body
  uses shell substitution or variable expansion
- **WHEN** the pre-execution policy evaluates the command
- **THEN** it MUST block before the shell evaluates the composition

### Requirement: Runtime parity across supported agent harnesses

Claude Code and OpenCode MUST invoke the same authoritative publication policy
before supported Bash commands execute. OpenCode enforcement MUST use a
project-local plugin's `tool.execute.before` hook and MUST abort the Bash call
by throwing on a policy block. The TypeScript bridge MUST NOT duplicate PII
patterns or parse account configuration itself.

#### Scenario: OpenCode blocks unsafe comment
- **GIVEN** OpenCode is loading the project-local plugin and a Bash call invokes
  `gh pr comment` with unsafe inspectable text
- **WHEN** `tool.execute.before` runs
- **THEN** it MUST throw before the Bash executor runs and the exception MUST
  not reveal matched text

#### Scenario: Claude Code blocks unsafe issue body
- **GIVEN** Claude Code passes a Bash event with a supported unsafe GitHub issue
  publication command to `outbound_gate.py`
- **WHEN** the hook evaluates its stdin payload
- **THEN** it MUST return the established blocking exit code before command
  execution

### Requirement: Publication policy isolation

The publication policy MUST be testable without GitHub, a configured operator
workspace, or an OpenCode server. Tests MUST inject account names rather than
using real account configuration and MUST assert returned decisions before
derived diagnostic assertions. The JSON policy entry point MUST reject malformed,
incomplete, oversized, or trailing-input requests without emitting command or
payload content.

#### Scenario: Empty account configuration
- **GIVEN** the account-name accessor returns no configured account names
- **WHEN** the policy evaluates a supported command with safe placeholder text
- **THEN** it MUST evaluate the static PII categories and permit the command
  when no violation is found

#### Scenario: Malformed checker request
- **GIVEN** the JSON policy entry point receives invalid JSON, a non-object value,
  missing or wrongly typed required fields, an oversized request, or trailing bytes
- **WHEN** the checker evaluates the request
- **THEN** it MUST return its rejection exit code and emit no policy decision or
  request content
