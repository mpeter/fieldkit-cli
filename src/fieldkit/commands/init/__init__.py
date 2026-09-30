"""fieldkit init — first-run configuration wizard.

Guides a new user through identity, account, data directory, and credential
setup. Writes:
  - ~/.config/fieldkit/config.yaml
  - <data_dir>/config/identity.yaml
  - <data_dir>/config/accounts.yaml  (scaffold)
  - <data_dir>/accounts/<account>/account.md  (stub per account)
  - <data_dir>/.env  (optional OAuth credentials)

Safe to re-run: prompts show current values as defaults and unrelated operator
files are preserved. ``--answers`` supports unattended initialization.
"""
