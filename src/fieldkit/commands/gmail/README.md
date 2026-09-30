# fieldkit Gmail command adapters

Use the [Gmail guide](../../../../docs/guides/gmail.md) for authentication,
sync modes, cache operations, and recovery. It is the canonical user guide;
do not maintain a second set of setup commands or storage defaults here.

For a first sync, follow the guide's OAuth setup and run `fieldkit gmail sync`.
That command contacts Google and requires the optional `google` installation
profile. The other commands below do not require that profile. Once a managed
cache exists, `fieldkit gmail query --help` lists the local searches, and
`fieldkit gmail account-tags` maps cached `ref/*` labels to
accounts. `fieldkit gmail enrich-pursuits` writes account reports from the cache.
If you have an older cache, see the guide's `fieldkit gmail import-cache`
procedure before querying it; import requires an explicit source and a fresh
managed destination.

This package exposes the Gmail CLI. Keep command parsing and output here, and
reuse the [Gmail domain](../../gmail/) for shared behavior. The database
structure is defined by the domain's [schema](../../gmail/schema.sql).

The Gmail cache contains message content and contact data. OAuth credentials
are separate sensitive state; neither belongs in a checkout, fixture, or
diagnostic attachment. Use isolated temporary data and fictional messages in
tests. See [privacy guidance](../../../../docs/privacy.md) before working with
real mailbox data.
