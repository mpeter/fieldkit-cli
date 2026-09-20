# Security policy

## Supported versions

fieldkit has no supported public release. The unreleased `main` branch receives fixes on a
best-effort basis, but source marked `1.0.0` is not evidence of a public release by itself. Private
pre-public builds are not supported releases.

| Version | Security support |
| --- | --- |
| Unreleased `main` | Best-effort during development |
| Private pre-1.0 builds | Not supported |

## Report a vulnerability privately

Do not open a public issue, discussion, or pull request for a suspected vulnerability.

Use [GitHub private vulnerability reporting](https://github.com/mpeter/fieldkit-cli/security/advisories/new).
If that route is unavailable, do not disclose exploit details publicly. The [roadmap](ROADMAP.md)
records private vulnerability reporting as a required repository-cutover control.

Include, when available:

- the affected fieldkit version or commit;
- the operating system and Python version;
- the vulnerable command or component;
- a minimal reproduction with credentials, customer data, and private hosts removed;
- the impact and conditions required for exploitation; and
- any suggested mitigation or disclosure constraint.

Reports are reviewed on a best-effort basis. The project does not promise an acknowledgement or fix
deadline. Maintainers will coordinate scope, remediation, release, credit, and disclosure with the
reporter when contact is possible.

## What belongs here

Security reports include credential disclosure, authorization bypass, path escape, unsafe handling
of untrusted external content, command or code injection, release/workflow compromise, and material
violations of the documented local trust boundary.

Ordinary setup failures, unsupported environments, expected local-user access to local files, and
feature requests belong in the normal support or issue channels. When uncertain, prefer a private
report; maintainers can redirect it without exposing details.
