#!/usr/bin/env python3
"""Check the consolidated effective consulting ACV contract."""

from __future__ import annotations

import sys
from pathlib import Path

_HELPER = "effective_net_consulting_acv"


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read {path}: {exc}")
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: check_acv_contract.py COMPONENTS ACCOUNT FORECAST QUOTA TESTS")
        return 1
    components, account, forecast, quota, tests = (_read(path) for path in argv)
    if any(text is None for text in (components, account, forecast, quota, tests)):
        return 1
    assert components is not None and account is not None and forecast is not None
    assert quota is not None and tests is not None

    failures: list[str] = []
    if components.count(f"def {_HELPER}(") != 1:
        failures.append("components must define the helper exactly once")
    for label, text in (("account", account), ("forecast", forecast), ("quota", quota), ("tests", tests)):
        if _HELPER not in text:
            failures.append(f"{label} does not reference the helper")
    if "_effective_consulting_acv" in account:
        failures.append("legacy account adapter remains")
    if "acv_keys" in forecast:
        failures.append("inline forecast acv_keys block remains")
    if '"fixed_price"' in quota:
        failures.append("inline quota fixed_price literal remains")
    if "pytest.approx(_GROSS)" not in tests:
        failures.append("gross fallback assertion is missing")
    if "test_effective_net_consulting_acv_fixed_price_acv_none_falls_back_to_gross" not in tests:
        failures.append("fixed-price fallback test is missing")
    if failures:
        for failure in failures:
            print(f"ACV contract: {failure}")
        return 1
    print("ACV contract: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
