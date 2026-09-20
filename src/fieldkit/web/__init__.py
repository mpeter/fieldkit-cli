"""fieldkit.web — Local web dashboard and API over fieldkit data.

Serves the morning brief, pipeline health, watcher alerts, and a
fieldkit-context chat endpoint as a localhost web app (PWA-installable).

Architectural position: this is a *presentation* domain. It reads data
three ways, none of which cross the tach boundary into ``commands/``:

1. Direct file reads of operator-owned artifacts (briefs, watcher alerts).
2. Subprocess calls to ``fieldkit <cmd> --json`` — the CLI's machine
   output is the stable data contract.
3. ``fieldkit.llm`` for chat synthesis (same two-layer model as brief).

The threat-model "no listener" claim (docs/threat-model.md §5) is scoped
to the default CLI behavior: nothing listens unless the operator runs
``fieldkit web serve`` explicitly, and the default bind is 127.0.0.1.
"""

from fieldkit.web.data import DataSource as DataSource
from fieldkit.web.server import create_app as create_app
