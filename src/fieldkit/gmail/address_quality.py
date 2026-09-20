"""Conservative address-quality classification for Gmail contact discovery."""

import re
from collections.abc import Mapping, Sequence
from typing import TypeAlias

BlindspotRecord: TypeAlias = tuple[str, str, int, int]

SUSPECTED_MASKED_REASON = "account-domain-four-character-suffix"

_SUFFIX_RE = re.compile(r"^[a-z0-9]{4}$")
_NAME_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def account_domains(config: Mapping[str, object], account: str) -> tuple[str, ...]:
    """Return normalized configured domains for *account*, or no domains for malformed config."""
    accounts = config.get("accounts")
    if not isinstance(accounts, Mapping):
        return ()
    account_config = accounts.get(account)
    if not isinstance(account_config, Mapping):
        return ()
    domains = account_config.get("domains")
    if not isinstance(domains, list):
        return ()
    return tuple(domain.strip().lower() for domain in domains if isinstance(domain, str) and domain.strip())


def is_suspected_masked_address(email: str, domains: Sequence[str]) -> bool:
    """Return whether *email* matches the narrow account-domain masking heuristic."""
    local, separator, domain = email.rpartition("@")
    configured_domains = {value.strip().lower() for value in domains if value.strip()}
    if not separator or domain.lower() not in configured_domains:
        return False

    segments = local.split(".")
    if len(segments) < 3 or any(not segment for segment in segments):
        return False
    suffix = segments[-1]
    if _SUFFIX_RE.fullmatch(suffix) is None or not any(character.isalpha() for character in suffix):
        return False
    return all(
        _NAME_SEGMENT_RE.fullmatch(segment) is not None and any(character.isalpha() for character in segment)
        for segment in segments[:-1]
    )


def partition_suspected_masked(
    records: Sequence[BlindspotRecord], domains: Sequence[str]
) -> tuple[list[BlindspotRecord], list[BlindspotRecord]]:
    """Partition *records* into ordinary and suspected-masked lists while preserving order."""
    ordinary: list[BlindspotRecord] = []
    suspected: list[BlindspotRecord] = []
    for record in records:
        target = suspected if is_suspected_masked_address(record[0], domains) else ordinary
        target.append(record)
    return ordinary, suspected
