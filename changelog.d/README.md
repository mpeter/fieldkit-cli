# Changelog fragments

Pull requests describe user-visible changes in one Markdown fragment under this
directory. Do not edit `CHANGELOG.md` directly; release preparation assembles
fragments under its `Unreleased` section.

## Name the outcome

Use a short descriptive slug that remains meaningful without access to a private
tracker:

```text
changelog.d/fix-missing-config-message.md
changelog.d/add-pipeline-json-output.md
changelog.d/document-fedora-install.md
```

A public GitHub issue number may be included when one exists, but it is not
required. Do not use private issue IDs, customer names, usernames, email
addresses, or organization-internal references in the filename or content.

## Write for users

Start with a level-three heading, then explain the observable result and any
action a user must take:

```markdown
### Fix the missing-configuration error (#123)

`fieldkit doctor` now identifies the missing key and exits with the documented
configuration error code instead of emitting a traceback.
```

Use `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, or `Security` as a
level-four subheading only when a longer fragment needs structure. Do not add a
leading or trailing horizontal rule; the assembler inserts separators.

## Verify and assemble

```console
make changelog-preview
make changelog
```

The preview writes nothing. Assembly is a release-maintainer operation: it folds
fragments into `CHANGELOG.md` and removes the consumed files.

Documentation-only, test-only, refactor-only, and CI-only changes may omit a
fragment when they have no user-visible effect. The `skip-changelog` pull-request
label records an explicit waiver for other exceptional cases.
