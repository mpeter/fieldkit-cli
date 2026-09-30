"""Collect local pursuits for the pipeline quota command."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from fieldkit.pursuit import iterate_pursuits
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.io import load_pursuit
from fieldkit.sf.components import effective_net_consulting_acv


def _collect_pursuits_for_quota(
    data_root: Path,
    account_filter: str | None = None,
) -> list[dict[str, object]]:
    """Collect pursuit stage + amount from all (or one account's) pursuit files.

    Args:
        data_root:      Workspace root path.
        account_filter: When set, restricts the scan to
                        ``accounts/<account_filter>/pursuits/``.
    """
    result: list[dict[str, object]] = []

    if account_filter is not None:
        pursuit_paths = sorted((data_root / "accounts" / account_filter / "pursuits").glob("*.md"))
        paths = (p for p in pursuit_paths if ".template" not in str(p) and "gmail-intel" not in str(p))
    else:
        paths = iterate_pursuits(data_root)

    for path in paths:
        try:
            fm, _, _ = load_pursuit(path)
        except (ValueError, yaml.YAMLError, ValidationError):
            continue
        stage = fm.stage
        # Skip closed-lost — they don't count for quota either way
        if stage == Stage.CLOSED_LOST:
            continue
        raw_amount = effective_net_consulting_acv(
            fm.sf_contract_type,
            fm.sf_consulting_acv,
            fm.sf_acv,
            fm.sf_arr,
        )
        result.append({"stage": stage, "sf_amount": raw_amount, "sf_probability": fm.sf_probability, "name": path.stem})
    return result
