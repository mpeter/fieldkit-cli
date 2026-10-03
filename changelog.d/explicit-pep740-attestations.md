### Publish PEP 740 attestations by explicit release policy

Releases published to TestPyPI or PyPI attach a PEP 740 publish attestation to
every wheel and sdist, signed for the release workflow's Trusted Publishing
identity. This was already the publishing action's default; the release
workflow now sets it explicitly, and the release workflow policy fails if
either publication job omits or disables it.
