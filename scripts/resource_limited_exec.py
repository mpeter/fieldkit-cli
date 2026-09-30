"""Set hard child resource limits before replacing this process with a tool."""

import os
import sys
from pathlib import Path


def main() -> int:
    """Fail closed if any resource ceiling or executable cannot be established."""
    try:
        import resource

        memory_limit, file_limit = int(sys.argv[1]), int(sys.argv[2])
        command = sys.argv[3:]
        if memory_limit <= 0 or file_limit <= 0 or not command or not Path(command[0]).is_absolute():
            raise ValueError("invalid child execution policy")
        for kind, requested in (
            (resource.RLIMIT_AS, memory_limit),
            (resource.RLIMIT_FSIZE, file_limit),
            (resource.RLIMIT_CORE, 0),
        ):
            inherited = resource.getrlimit(kind)
            ceiling = min([requested, *(limit for limit in inherited if limit != resource.RLIM_INFINITY)])
            resource.setrlimit(kind, (ceiling, ceiling))
        os.execv(command[0], command)
    except (ImportError, OSError, ValueError, IndexError):
        print("resource-limited child setup failed", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
