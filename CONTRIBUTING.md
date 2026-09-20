# Contributing to fieldkit

Thank you for helping make fieldkit more useful, reliable, and approachable. Contributions can be
code, tests, documentation, issue triage, reproducible bug reports, or design feedback.

## Find suitable work

Start with an existing issue labeled `good first issue`, `help wanted`, `bug`, or `documentation`.
Comment before beginning so contributors do not duplicate work. For a new feature, CLI change,
persisted-data change, or broad refactor, open a feature proposal first and wait for agreement on the
problem and approach. Small bug fixes, tests, and documentation corrections can go directly to a
pull request when their intent is clear.

Security vulnerabilities do not belong in public issues. Follow [SECURITY.md](SECURITY.md).
Questions that are not defects belong in the route described by [SUPPORT.md](SUPPORT.md).

## Set up a development checkout

You need Git, [uv](https://docs.astral.sh/uv/), Make, and a supported Python version. Python 3.11 is
the canonical contributor environment; compatibility smoke also covers newer supported versions.

Fork the repository, clone your fork, and create a focused branch:

```console
git clone https://github.com/<your-user>/fieldkit-cli.git
cd fieldkit-cli
git remote add upstream https://github.com/mpeter/fieldkit-cli.git
git fetch upstream main
git switch -c fix/short-description
make bootstrap
```

`make bootstrap` installs the locked development environment and repository hooks. It does not
install fieldkit globally, read fieldkit credentials, or require a separate workspace. Run commands
from the checkout with `uv run fieldkit` so you test the branch you are editing.

## Make a change

Keep each pull request focused on one problem. Preserve the architecture documented in the repository:

- command modules parse input and delegate; domain behavior belongs in the corresponding package;
- external input is untrusted, and network/subprocess calls have explicit timeouts;
- credentials and runtime data stay outside the repository;
- file writes use the repository's atomic or locked helpers appropriate to their ownership model;
- authentication failures propagate to the top-level CLI boundary;
- the portable core must continue to work without optional integrations.

Bug fixes and behavior changes need tests that demonstrate the observable contract. Documentation-only
and test-only improvements are welcome and do not need invented runtime changes.

If the CLI surface changes, run `make docs` and include the generated reference update. User-visible
changes need one descriptive Markdown file under `changelog.d/`; documentation-only, test-only,
refactor-only, and CI-only changes may be exempt when the change has no user-facing effect. Follow
[the fragment guide](changelog.d/README.md).

## Verify the pull request

Run the same bounded gate expected before review:

```console
git fetch upstream main
QUALITY_BASE=upstream/main make pr-check
```

The command reports each failing stage separately. You can run focused tests and linters while
iterating, but `make pr-check` is the supported local readiness signal. Complete enforcement and the
supported-platform artifact matrix run in GitHub Actions.

Use conventional commit messages such as `fix(cli): handle missing config` or
`docs: clarify installation`. Do not commit credentials, customer data, personal email addresses,
private service output, absolute home paths, generated runtime files, or private issue identifiers.

## Open the pull request

Explain the problem and user-visible result, link the public issue when there is one, and include the
tests or other evidence appropriate to the change. Call out privacy, security, compatibility, or
persisted-data effects. The pull-request template is intentionally proportional; mark an item not
applicable instead of manufacturing evidence.

Maintainers may request a smaller scope, a design discussion, tests, documentation, or a changelog
fragment. Review focuses on behavior, safety, maintainability, and compatibility rather than who
authored the change. AI-assisted contributions are welcome under the same standard: the contributor
must understand, verify, and take responsibility for everything submitted, and must not expose
sensitive data to a model or service without authorization.

## Contribution terms

fieldkit is licensed under the [Apache License 2.0](LICENSE). Unless you state otherwise, any
intentional contribution you submit for inclusion is licensed under the same terms, consistent with
section 5 of that license. The project does not require a separate contributor license agreement.

Project authority and maintainer roles are described in [GOVERNANCE.md](GOVERNANCE.md).
