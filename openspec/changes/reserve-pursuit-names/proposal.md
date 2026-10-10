## Why

Issue #85: `fieldkit pursuit create --name template` and `--name gmail-intel`
exit 0 with `"created": true`, but forecast, health, and audit then skip those
files as non-pursuit files. A staged, valued deal drops out of every portfolio
report with exit 0 and no warning.

The cause is that "which files are pursuits" has no single owner.
`fieldkit.pursuit.io.NON_PURSUIT_FILES` exists (added for #97), but the
following sites carry their own rules:

| Site | Rule | Effect |
| --- | --- | --- |
| `commands/pursuit/audit.py:443`, `:511` | literal `{"gmail-intel.md", "template.md"}` | duplicate of the constant |
| `commands/sf/sync.py:190`, `commands/sf/account.py:297`, `:385` | literal tuple or set | duplicate of the constant |
| `pursuit/stale.py:109` | `p.stem not in ("template", "gmail-intel")` | duplicate of the constant |
| `commands/pursuit/archive_cmd.py:250`, `pursuit/projects.py:164` | `stem == "template"` only | `gmail-intel.md` counted as a pursuit |
| `pursuit/utils.py:70` `iterate_pursuits()` (brief, pipeline, quota) | substring `".template"` or `"gmail-intel"` anywhere in the path | skips `gmail-intel-rollout.md` and any path under an account named `gmail-intel-*`; does **not** skip `pursuits/template.md` |
| `commands/sf/frontmatter.py:973` | `name == "template.md"` or `".template"` substring | skips a validated file inside a `.template/` directory (intended) |

`pursuit create` and `pursuit rename` consult none of these.

## What Changes

1. `fieldkit.pursuit.io` owns one reserved-basename definition and one
   predicate, `is_reserved_pursuit_path(path) -> bool`, matching the exact file
   name only.
2. `pursuit create` and `pursuit rename --to` reject a slug whose file name is
   reserved, with exit 3 and no write.
3. Every portfolio scan uses the predicate. `iterate_pursuits()` stops matching
   substrings, so legitimately named pursuits reappear in brief, pipeline, and
   quota output, and `template.md` stops appearing as a pursuit there.
4. Portfolio reports that skip a reserved file in a `pursuits/` directory
   name it in their diagnostics. A reserved file is never dropped silently.

## Capabilities

### New Capabilities
- `pursuit-reserved-names`: one definition of reserved pursuit file names,
  enforced at creation and rename and disclosed when reports skip them.

### Modified Capabilities
- None. `pursuit-audit-output` and `pursuit-health-exit-policy` keep their
  existing exit semantics; disclosure goes through the existing report
  assessment diagnostics.

### Removed Capabilities
- None.

## Impact

- `src/fieldkit/pursuit/io.py`, `pursuit/utils.py`, `pursuit/stale.py`,
  `pursuit/projects.py`.
- `src/fieldkit/commands/pursuit/{create_cmd,rename_cmd,audit,archive_cmd}.py`,
  `commands/sf/{sync,account}.py`.
- User-visible: two names become invalid for new pursuits, and brief, pipeline
  and quota output changes for workspaces that held either a pursuit whose path
  contains `gmail-intel` or a `pursuits/template.md`.
- Requires a `changelog.d/` fragment.

## Constitution Alignment

Assessed against the Unbound Force org constitution.

### I. Autonomous Collaboration

**Assessment**: PASS

Pursuit files remain the shared artifact. Agents that create pursuits get a
deterministic refusal instead of creating a file that other commands ignore.

### II. Composability First

**Assessment**: N/A

No dependency or installation profile change.

### III. Observable Quality

**Assessment**: PASS

The refusal uses exit 3 with a JSON error when `--json` is set. Skipped
reserved files appear in report diagnostics by path, so omissions are
observable.

### IV. Testability

**Assessment**: PASS

The predicate is a pure function of a path. Command behavior is tested with
`tmp_path` workspaces and fictional accounts.
