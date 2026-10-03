### Retire the manual release-evidence ledger

`make release-check` no longer accepts a `RELEASE_MANUAL_EVIDENCE` ledger or
reports pending manual gates. It now builds the candidate, validates the public
export, and checks the report against the release-governance policy, exiting 0
when all three pass. The governance policy no longer carries external-control
records, and `check_release_governance.py` exits 0 for a matching candidate.
The release workflow now runs TestPyPI rehearsal and consumer verification
itself, and GitHub and PyPI enforce environment protection and Trusted
Publishing when it runs.
