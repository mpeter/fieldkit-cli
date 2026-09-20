"""Private pipeline quota configuration accessors."""

from collections.abc import MutableMapping
from copy import copy
from typing import Any

from ruamel.yaml.error import YAMLError as RoundTripYAMLError

from fieldkit.config import _loader
from fieldkit.util.atomic import atomic_round_trip_yaml_update


def get_pipeline_quota() -> dict[str, object] | None:
    """Return the configured pipeline quota projection, if complete."""
    data = _loader._load_raw_config()
    if data is None:
        return None
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict):
        return None
    quota = pipeline.get("quota")
    if not isinstance(quota, dict):
        return None
    target = quota.get("target")
    period = quota.get("period")
    if target is None or period is None:
        return None
    return {"target": int(target), "period": str(period)}


def write_pipeline_quota(target: int, period: str) -> None:
    """Round-trip update the quota while preserving unrelated YAML presentation."""

    def update(data: MutableMapping[str, Any]) -> None:
        existing_pipeline = data.get("pipeline")
        if isinstance(existing_pipeline, MutableMapping):
            pipeline = copy(existing_pipeline)
            data["pipeline"] = pipeline
        else:
            pipeline = {}
            data["pipeline"] = pipeline
        quota = pipeline.get("quota")
        if isinstance(quota, MutableMapping):
            quota = copy(quota)
            pipeline["quota"] = quota
        else:
            quota = {}
            pipeline["quota"] = quota
        quota["target"] = target
        quota["period"] = period

    try:
        atomic_round_trip_yaml_update(_loader.CONFIG_PATH, update)
    except (OSError, TypeError, UnicodeError, RoundTripYAMLError) as exc:
        raise _loader.ConfigError(f"Could not update pipeline quota in {_loader.CONFIG_PATH}: {exc}") from exc
    _loader.clear_config_caches()
