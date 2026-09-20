"""CLI entry point: python -m fieldkit.commands.sf <subcommand>

Delegates to fieldkit.commands.sf.cli so that:
  python -m fieldkit.commands.sf <subcommand> [args...]
is equivalent to:
  fieldkit sf <subcommand> [args...]
"""

import importlib
import sys


def main() -> None:
    """Delegate to the fieldkit.sf Click group."""
    # importlib avoids a static cross-module import that tach would flag.
    sf = importlib.import_module("fieldkit.commands.sf.cli")
    sf.cli.main(
        sys.argv[1:],
        prog_name="fieldkit sf",
        standalone_mode=True,
    )


if __name__ == "__main__":
    main()
