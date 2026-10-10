## 1. Domain definition

- [ ] 1.1 Add `RESERVED_PURSUIT_SLUGS` and `is_reserved_pursuit_path()` to `src/fieldkit/pursuit/io.py`, deriving `NON_PURSUIT_FILES` from the slugs so there is one source
- [ ] 1.2 Add parameterized unit tests for the predicate: `template.md`, `gmail-intel.md`, `gmail-intel-rollout.md`, `real-deal.md`, and a path under an account named `gmail-intel-co`

## 2. Refuse reserved names

- [ ] 2.1 [P] Reject reserved slugs in `commands/pursuit/create_cmd.py` before any write; exit 3, JSON error under `--json`
- [ ] 2.2 [P] Reject reserved `--to` slugs in `commands/pursuit/rename_cmd.py` before any write or watcher-state update; exit 3
- [ ] 2.3 [P] Add happy-path and failure-path tests for create (including `--dry-run`) and rename, asserting no file or state changes on refusal

## 3. Replace duplicated skip rules

- [ ] 3.1 [P] `pursuit/utils.py` `iterate_pursuits()`: use the predicate instead of substring matching
- [ ] 3.2 [P] `pursuit/stale.py` and `pursuit/projects.py`: use the predicate
- [ ] 3.3 [P] `commands/pursuit/audit.py` (both sites) and `commands/pursuit/archive_cmd.py`: use the predicate
- [ ] 3.4 [P] `commands/sf/sync.py` and `commands/sf/account.py` (both sites): use the predicate
- [ ] 3.5 [P] `commands/sf/frontmatter.py`: use the predicate for the file name and keep the `.template/` directory rule
- [ ] 3.6 Add a test that fails if any module under `src/fieldkit/` contains a `"gmail-intel.md"` or `"template.md"` literal outside `pursuit/io.py`
- [ ] 3.7 Add a regression test reproducing #85: create `template`, `gmail-intel`, and `real-deal`, and confirm forecast, health, and audit all show `real-deal`

## 4. Disclosure

- [ ] 4.1 Add a `reserved` path list to `ReportAssessment` in `pursuit/io.py` and populate it in `scan_report_inputs()`
- [ ] 4.2 Render reserved skips in forecast, health, and audit diagnostics (stderr for text, the diagnostics field for JSON) without changing exit status
- [ ] 4.3 Add tests for the disclosure in text and JSON modes

## 5. Finish

- [ ] 5.1 [P] Add `changelog.d/85-reserved-pursuit-names.md` describing the refusal and the brief/pipeline/quota corrections
- [ ] 5.2 [P] Update the pursuit guide to list the reserved names
- [ ] 5.3 Run `make pr-check` and `make docs`
- [ ] 5.4 Verify constitution alignment: refusal and disclosure are machine-parseable under `--json` (III); predicate and commands tested with `tmp_path` and fictional data only (IV)
