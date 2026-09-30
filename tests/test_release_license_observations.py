"""The real bundle CLI classifies bounded nested license input as invalid data."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_bundle
from scripts.runtime_license_inventory import MAX_OBSERVATION_BYTES, OBSERVATIONS_NAME
from tests import release_bundle_support

pytestmark = pytest.mark.unit


def test_bundle_cli_sanitizes_bounded_nested_license_observations(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    observations = b'{"private-sentinel":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}"
    assert len(observations) < MAX_OBSERVATION_BYTES
    (candidate / OBSERVATIONS_NAME).write_bytes(observations)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["license_evidence"]["observations"]["sha256"] = hashlib.sha256(observations).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")

    # Initial-release failure retention remains intact while the CLI verifies
    # the same digest-bound input through the real consumer path.
    with pytest.raises(ValueError, match=r"^runtime license observations contain excessively nested JSON$"):
        release_bundle.materialize(candidate)
    retained = list(candidate.glob(".bundle-*"))
    assert len(retained) == 1
    assert not (candidate / "bundle").exists()
    result = subprocess.run(
        [sys.executable, "-m", "scripts.release_bundle", str(retained[0]), "--candidate-report", str(report_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 3
    assert result.stdout == ""
    assert result.stderr == "Release bundle: ERROR: runtime license observations contain excessively nested JSON\n"
