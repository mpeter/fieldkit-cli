### Sibling skill links resolve in flat install targets

Flat targets such as Cursor install every skill as one `<name>.md` file side by
side, so a link like `../tool-routing/SKILL.md` used to point at a path that does
not exist there. The flat renderer now rewrites links into a sibling skill to
that skill's flat file, or to its bundled-support anchor for a support file.
`fieldkit skill install` also warns when an installed skill links a sibling
skill that the target neither has nor is receiving, so a selective `--skill`
install no longer leaves dead links silently.
