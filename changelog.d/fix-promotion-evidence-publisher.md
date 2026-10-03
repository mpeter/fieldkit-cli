### Record the publisher that actually ran in release promotion evidence

Release promotion evidence now names `pypa/gh-action-pypi-publish` v1.14.2,
the revision the release workflow publishes with, instead of the earlier
v1.9.0. The release workflow policy fails when the workflow's attestation or
publishing action differs from the revision the evidence records, so the two
cannot drift apart again.
