## 1. Ratchet

- [x] 1.1 Add `scripts/check_exit_sites.py`: AST scan of `src/fieldkit/` for `raise SystemExit`/`Exit`, `sys.exit()`, `ctx.exit()`, `os._exit()` and builtin `exit()`/`quit()`, excluding the two boundary modules and `__main__` guards
- [x] 1.2 Commit `.exit-sites-baseline.json` (231 sites in 70 command files) and a `--write-baseline` mode that only records removals
- [x] 1.3 Add `tests/test_check_exit_sites.py`: every exit form, boundary exclusion, added site, new file, domain site, stale baseline, refusal to record growth, lowering after migration, and the repository against its baseline
- [x] 1.4 Add the `exit-sites` stage to `make pr-check` and `make quality-full`

## 2. Documentation

- [x] 2.1 Update the exit and `print()` rules in `AGENTS.md`
- [x] 2.2 Amend ADR 0003 with the process-exit boundary and the ratchet
- [x] 2.3 Note the remaining direct exits in `docs/reference/exit-codes.md`
- [x] 2.4 Contributor-only change: use the `skip-changelog` label

## 3. Follow-up

- [ ] 3.1 (#134) Migrate the remaining sites per domain (auth, sf, pursuit, watch, pipeline, companion, ingest, others), lowering the baseline in each change
