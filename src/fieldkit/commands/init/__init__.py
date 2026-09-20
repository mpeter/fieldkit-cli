"""fieldkit init — first-run configuration wizard.

Guides a new user through identity, account, data directory, and credential
setup. Writes:
  - ~/.config/fieldkit/config.yaml
  - <data_dir>/config/identity.yaml
  - <data_dir>/config/accounts.yaml  (scaffold)
  - <data_dir>/config/{clocks,engines,people,watchlist}.json  (empty judgment scaffolds)
  - <data_dir>/accounts/<account>/account.md  (stub per account)
  - <data_dir>/.env  (optional OAuth credentials)

Safe to re-run: prompts show current values as defaults, and existing judgment
files are preserved. ``--answers`` supports unattended initialization.
"""

# ``cfg`` is re-exported so tests can do ``patch.object(init_mod.cfg, …)``.
# wizard.py imports ``cfg`` from fieldkit.config; mirroring it here puts the attribute
# on the ``fieldkit.commands.init`` namespace. The name must remain ``cfg``.
import fieldkit.config as cfg  # noqa: F401

# ``_wizard_write_config`` is re-exported so tests can call it directly via
# ``init_mod._wizard_write_config(…)`` without importing from wizard.py.
from fieldkit.commands.init.wizard import _wizard_write_config as _wizard_write_config
