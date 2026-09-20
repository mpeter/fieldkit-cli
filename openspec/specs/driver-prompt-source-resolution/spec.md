## Purpose

Ensure the driver can execute directory-based OpenSpec and Speckit prompt sources
immediately after they merge, even before the shared checkout refreshes.

## Requirements

### Requirement: Directory sources resolve artifacts available on origin/main

The driver MUST resolve an OpenSpec or Speckit directory-source candidate when the
candidate is a regular file in the shared checkout or a blob at the corresponding
path in `origin/main`. It SHALL preserve the configured primary-before-fallback
candidate order and existing allowed-root traversal guard.

#### Scenario: OpenSpec tasks file has merged but the shared checkout is stale
- **GIVEN** an issue references an OpenSpec change directory inside the allowed root
  and its `tasks.md` is absent from the shared checkout but is a blob in `origin/main`
- **WHEN** the driver resolves the prompt source
- **THEN** it returns the `tasks.md` path for the execution worktree

#### Scenario: Speckit tasks file has merged but the shared checkout is stale
- **GIVEN** an issue references a Speckit directory inside the allowed root and its
  `tasks.md` is absent from the shared checkout but is a blob in `origin/main`
- **WHEN** the driver resolves the prompt source
- **THEN** it returns the `tasks.md` path for the execution worktree

#### Scenario: Primary candidate is unavailable but fallback is available remotely
- **GIVEN** a valid directory source whose primary candidate is absent from both the
  shared checkout and `origin/main`, while its fallback is a blob in `origin/main`
- **WHEN** the driver resolves the prompt source
- **THEN** it returns the fallback path

### Requirement: Prompt source resolution rejects unsafe or unavailable sources

The driver MUST reject directory references that escape their allowed root. For a
safe directory reference, it MUST defer only after neither the primary nor fallback
candidate is a regular file locally or a blob in `origin/main`.

#### Scenario: Safe directory has no local or remote candidate
- **GIVEN** a directory reference within its allowed root and neither candidate
  exists in the shared checkout or as a blob in `origin/main`
- **WHEN** the driver resolves the prompt source
- **THEN** it returns no prompt source and retains the existing deferral behavior
