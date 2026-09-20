# skill-install Specification

## Purpose
Define the current behavioral contract for skill-install, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Successful installs record per-tool ownership

The installer SHALL maintain a versioned manifest under each selected tool's registered skill root. It SHALL add only skill names successfully written for that tool, preserve ownership from prior partial selections, and update the manifest atomically under a lock. A dry run SHALL create neither manifest nor lock file.

#### Scenario: partial selection preserves prior ownership

- **GIVEN** a tool manifest owns `brief`
- **WHEN** a later install successfully writes only `meeting`
- **THEN** the manifest owns both `brief` and `meeting`

#### Scenario: failed write is not claimed

- **GIVEN** a selected skill write fails
- **WHEN** installation completes with an error
- **THEN** that skill is absent from newly recorded ownership

### Requirement: Pruning targets only proven stale ownership

`--prune` SHALL consider a skill stale only when its validated name is present in the selected tool's manifest and absent from the bundled skill corpus. The command SHALL derive its target path from the tool registry and SHALL reject any name or resolved path that violates containment.

#### Scenario: workspace-local skill is preserved

- **GIVEN** a target contains a local skill absent from both the bundle and ownership manifest
- **WHEN** pruning inventories the target
- **THEN** the local skill is reported as untracked and is not a prune candidate

#### Scenario: tampered ownership entry cannot escape the tool root

- **GIVEN** a manifest contains a path-like or otherwise invalid skill name
- **WHEN** pruning inventories the manifest
- **THEN** the command reports an error and deletes nothing for that tool

### Requirement: Pruning requires explicit confirmation

`--prune` without `--confirm` SHALL print owned stale candidates and leave targets and ownership unchanged. `--prune --confirm` SHALL remove each existing candidate using its registered target format and remove reconciled names from the manifest. `--dry-run` SHALL remain non-mutating even when `--confirm` is present.

#### Scenario: preview retains stale skill

- **GIVEN** a manifest-owned installed skill is no longer bundled
- **WHEN** installation runs with `--prune`
- **THEN** the command prints the candidate and leaves both its target and ownership record present

#### Scenario: confirmed directory prune removes only owned target

- **GIVEN** one stale manifest-owned directory skill and one untracked local directory skill
- **WHEN** installation runs with `--prune --confirm`
- **THEN** the owned directory and its ownership entry are removed while the local directory remains

#### Scenario: install failure suppresses pruning

- **GIVEN** stale ownership exists and a selected install write fails
- **WHEN** installation runs with `--prune --confirm`
- **THEN** the stale target remains and the command reports that pruning was skipped

### Requirement: Installed content has a trusted per-tool baseline

The installer SHALL store a deterministic digest of each successfully installed target in its versioned per-tool ownership manifest. Directory target digests SHALL cover every installed regular file and its relative path. Flat target digests SHALL cover the installed file. Version 1 ownership SHALL remain readable without inventing a digest.

#### Scenario: successful install records actual target content

- **GIVEN** a selected skill is written successfully
- **WHEN** installation completes
- **THEN** its manifest digest matches the installed target content

#### Scenario: dry run records no baseline

- **GIVEN** a selected skill has no recorded digest
- **WHEN** installation runs with `--dry-run`
- **THEN** neither a digest nor a manifest lock file is created

### Requirement: Local modifications block the full install plan

Before writing any selected target, the installer SHALL compare each existing target with its recorded install digest and the would-be rendered output. If a target differs from its recorded digest and from the would-be output, or lacks a recorded digest and differs from the would-be output, the command SHALL list it and exit nonzero without writing any selected target unless `--force` is present.

#### Scenario: one divergent skill prevents partial installation

- **GIVEN** two selected skills where the later target contains a local edit
- **WHEN** installation runs without `--force`
- **THEN** both targets remain unchanged and the divergent skill is listed

#### Scenario: forced overwrite refreshes the baseline

- **GIVEN** a selected target differs from its recorded install digest
- **WHEN** installation runs with `--force`
- **THEN** the target is overwritten, the output marks the forced overwrite, and the manifest records the new installed digest

#### Scenario: version 1 target with drift is untrusted

- **GIVEN** a version 1 manifest owns an existing target without a digest and that target differs from the current rendered bundle
- **WHEN** installation runs without `--force`
- **THEN** the target is left unchanged and the command requires explicit force

### Requirement: Identical targets are unchanged

When an existing target already matches the would-be rendered output, the installer SHALL skip the target write, report it as unchanged, and exclude it from the updated count. A non-dry-run SHALL record its current digest when the baseline is missing. A dry run SHALL predict the same unchanged classification without mutating state.

#### Scenario: repeated install preserves the target

- **GIVEN** a selected target already matches the rendered bundle
- **WHEN** installation runs again
- **THEN** the target modification time is preserved and the summary increments unchanged rather than updated

#### Scenario: force does not rewrite identical content

- **GIVEN** a selected target already matches the rendered bundle
- **WHEN** installation runs with `--force`
- **THEN** it remains an unchanged no-op

### Requirement: Force does not bypass independent safety controls

`--force` SHALL authorize only overwrites classified as locally modified or untrusted. It SHALL NOT bypass path containment, invalid names, missing sources, dry-run non-mutation, or prune confirmation.

#### Scenario: force cannot authorize path escape

- **GIVEN** a selected target resolves outside its registered project root
- **WHEN** installation runs with `--force`
- **THEN** the command reports the containment error and writes nothing outside the project

### Requirement: global installation is explicit and bounded

`fieldkit skill install --global` SHALL require an explicit tool, SHALL accept only `opencode` and
`claude-code`, and SHALL resolve their destinations from the registered global target paths. An install
SHALL require explicit selection of `handoffs` and/or `pickup`; global `--all` and other skill names
SHALL be rejected. A prune-only invocation MAY omit skill selection.

#### Scenario: install an OpenCode skill globally

- **GIVEN** `--global --tool opencode --skill handoffs`
- **WHEN** installation succeeds
- **THEN** the rendered skill directory is written beneath `~/.agents/skills`

#### Scenario: install a Claude Code skill globally

- **GIVEN** `--global --tool claude-code --skill handoffs`
- **WHEN** installation succeeds
- **THEN** the rendered skill directory is written beneath `~/.claude/skills`

#### Scenario: unsupported or implicit global tool

- **GIVEN** global mode omits `--tool` or selects Cursor or Gemini
- **WHEN** request validation runs
- **THEN** the command exits nonzero before detection, prompting, or filesystem mutation

#### Scenario: global all or unrelated skill requested

- **GIVEN** global mode uses `--all` or selects a skill other than `handoffs` or `pickup`
- **WHEN** request validation runs
- **THEN** the command exits nonzero before filesystem mutation

### Requirement: global targets preserve install safety controls

Global installation SHALL use the existing render, ownership baseline, full-plan preflight, pre-write
recheck, dry-run, force, and prune contracts within the registered global skill root. Candidate paths
MUST resolve inside that root.

#### Scenario: template variables render globally

- **GIVEN** a selected bundled skill contains a configured template variable
- **WHEN** it is installed globally
- **THEN** the installed Markdown contains the rendered value

#### Scenario: global path escapes its registered root

- **GIVEN** a candidate path or existing symlink resolves outside the registered global skill root
- **WHEN** preflight or the pre-write check runs
- **THEN** installation exits nonzero without writing outside the root even with `--force`

#### Scenario: registered global root is redirected outside its allowlisted parent

- **GIVEN** a registered global root is a symlink outside its allowed parent and is not the approved Claude-to-OpenCode alias
- **WHEN** global target resolution runs
- **THEN** installation exits nonzero before preflight or mutation

#### Scenario: registered global parent is a symlink

- **GIVEN** `~/.agents` or `~/.claude` is a symlink
- **WHEN** global target resolution runs
- **THEN** installation exits nonzero before preflight or mutation

#### Scenario: locally modified global target

- **GIVEN** a global target differs from both its ownership digest and would-be rendered content
- **WHEN** installation runs without `--force`
- **THEN** the full plan is refused without writing any selected target

### Requirement: aliased global roots are installed once

When selected global tool roots resolve to the same physical directory, the installer SHALL preflight,
write, record ownership, and count that target once.

#### Scenario: Claude global root links to the OpenCode global root

- **GIVEN** both tools are selected and `~/.claude/skills` resolves to `~/.agents/skills`
- **WHEN** global installation runs
- **THEN** each selected skill is written once and one shared manifest update is recorded

### Requirement: project-local mode excludes the home binary directory

Without `--global`, tool detection and every target SHALL remain relative to CWD. When CWD resolves
to the user home, the installer SHALL neither auto-detect nor explicitly select OpenCode or Claude
Code as a project-local target.

#### Scenario: command runs from the user home without global mode

- **GIVEN** the home contains OpenCode or Claude Code harness directories and no project-local intent
- **WHEN** `skill install` runs without `--global`
- **THEN** the command does not reinterpret the binary directory as a global skill destination

#### Scenario: project-local install outside the home

- **GIVEN** a repository CWD contains a supported tool marker
- **WHEN** `skill install` runs without `--global`
- **THEN** detection, target paths, and installation behavior remain CWD-relative
