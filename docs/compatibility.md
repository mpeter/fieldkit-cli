# Compatibility

fieldkit 1.0 supports its portable core on these tested environments:

| Operating system | Python | Status |
| --- | --- | --- |
| Ubuntu | 3.11, 3.12, 3.13, 3.14 | Supported core |
| macOS | 3.11, 3.12, 3.13, 3.14 | Supported core |
| Fedora/RHEL family | Fedora 43 system Python | Supported core |
| Windows | 3.11 or newer | Experimental |

"Supported core" means a built package passes installation, version/help, minimal initialization,
general diagnostics, offline first success, and sanctioned file-write smoke tests on that matrix.

Integration support can be narrower than core support. For example, systemd units are Linux-only,
and browser credential-store behavior depends on the desktop and operating system. Each integration
guide states its additional prerequisites.

CI also installs the complete `all` optional-dependency profile and imports every advertised
integration dependency on CPython 3.11 for Ubuntu 24.04 and macOS 15. This optional-profile check does
not extend the core's four-version matrix to every integration.

Windows is experimental because the same path, subprocess, and file-write contract has not yet been
proven there. Reports and contributions are welcome, but 1.0 does not claim Windows support.
