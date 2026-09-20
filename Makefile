.PHONY: verify test lint bootstrap contributor-hooks pr-check install reinstall install-health artifact-check release-check release-policy-check release-workflow-policy-check public-tree-safety quality quality-full gazepy gaze-baseline gaze-report audit-check hooks update-schema eval-skills install-skills sync-claude docs docs-site changelog changelog-preview mutation-report

# Broad pytest targets use four workers by default so a developer workstation retains
# memory for its interactive services. Override deliberately for a larger host, e.g.
# `make quality-full PYTEST_XDIST_WORKERS=8`.
PYTEST_XDIST_WORKERS ?= 4

# bootstrap — create the complete locked contributor environment and checkout-local hooks.
bootstrap:
	uv sync --frozen --all-extras --dev
	$(MAKE) contributor-hooks

# contributor-hooks — install repository checks without changing a global fieldkit tool.
# Maintainers can opt into the auto-reinstalling post-commit hook with `make hooks`.
contributor-hooks:
	uvx pre-commit==4.6.1 install -f
	uvx pre-commit==4.6.1 install -f --hook-type pre-push
	uvx pre-commit==4.6.1 install -f --hook-type commit-msg
	@echo "contributor-hooks: pre-commit, pre-push, and commit-msg hooks installed"

# pr-check — the canonical bounded local readiness gate.
pr-check: quality

# install — install fieldkit as a global uv tool, then sync AE skills to workspace.
# Workspace path is read from ~/.config/fieldkit/config.yaml (fieldkit_home key).
# Skills in .opencode/skills/ (dev tools) are excluded — only src/fieldkit/skills/ ships.
install: install-skills

install-skills:
	uv tool install ".[all]" --reinstall --force --python 3.11
	$(eval WORKSPACE := $(shell uv run python3 -c "import shlex; from fieldkit.config import get_fieldkit_home; print(shlex.quote(str(get_fieldkit_home())))" 2>/dev/null))
	@if [ -z "$(WORKSPACE)" ]; then \
		echo "ERROR: install-skills: no fieldkit workspace configured (get_fieldkit_home() failed — check ~/.config/fieldkit/config.yaml)" >&2; \
		exit 1; \
	elif [ ! -d "$(WORKSPACE)/.opencode" ] && [ ! -d "$(WORKSPACE)/.claude" ]; then \
		echo "ERROR: install-skills: $(WORKSPACE) has neither .opencode/ nor .claude/ — skill sync cannot proceed" >&2; \
		exit 1; \
	else \
		echo "Syncing skills to workspace: $(WORKSPACE)"; \
		cd "$(WORKSPACE)" && fieldkit skill install --tool opencode --tool claude-code --all; \
	fi

# reinstall — alias for install
reinstall: install-skills

# install-health — verify the active global fieldkit runtime includes Chrome auth.
install-health:
	python3 scripts/check_install_health.py

# artifact-check — build once, then inspect the exact wheel and sdist users receive.
artifact-check:
	uv build --out-dir dist
	uv run python scripts/check_artifacts.py dist/fieldkit_cli-*.whl dist/fieldkit_cli-*.tar.gz
	uv run python scripts/check_public_identity.py --artifact dist/fieldkit_cli-*.whl --artifact dist/fieldkit_cli-*.tar.gz
	uv run python scripts/smoke_artifact.py dist/fieldkit_cli-*.whl
	uv run python scripts/smoke_artifact.py dist/fieldkit_cli-*.tar.gz

# release-check — build exactly one retained wheel/sdist pair from an explicit verified public export.
# The output directory must be absent: a stale candidate must never be overwritten or promoted by accident.
PUBLIC_CANDIDATE_OUTPUT ?= build/public-candidate
RELEASE_MANUAL_EVIDENCE ?=

release-check:
	@if [ -z "$(PUBLIC_CANDIDATE_REVISION)" ]; then \
		echo "ERROR: release-check requires PUBLIC_CANDIDATE_REVISION=<full commit SHA>" >&2; \
		exit 2; \
	fi
	uv run python scripts/release_check.py \
		--repo . \
		--revision "$(PUBLIC_CANDIDATE_REVISION)" \
		--output-dir "$(PUBLIC_CANDIDATE_OUTPUT)" \
		--output "$(PUBLIC_CANDIDATE_OUTPUT).json" \
		$(if $(RELEASE_MANUAL_EVIDENCE),--manual-evidence "$(RELEASE_MANUAL_EVIDENCE)")

# release-policy-check — validate immutable release identity and support policy without network access.
release-policy-check:
	uv run python scripts/check_release.py policy

# release-workflow-policy-check — enforce the least-privilege workflow contract without calling GitHub.
release-workflow-policy-check:
	uv run python scripts/release_workflow_policy.py

public-tree-safety:
	uv run python scripts/check_public_tree_safety.py --repo .

# sync-claude — best-effort mirror of .opencode/agents/ and .opencode/commands/ into .claude/
# Run after adding or modifying agents or commands so Claude Code users see the same
# surface as OpenCode users. The post-commit hook calls this automatically when
# .opencode/agents/ or .opencode/commands/ changes.
sync-claude:
	uv run python scripts/sync_claude_dir.py || true

# verify — run the pytest suite; contract scripts run under quality targets
# Used by CI and as a pre-session sanity check.
verify:
	uv run pytest tests/ -q -n $(PYTEST_XDIST_WORKERS)

# test — alias for verify (pytest-only, no contract scripts)
test:
	uv run pytest tests/ -q -n $(PYTEST_XDIST_WORKERS)

# lint — mypy on all typed scopes
lint:
	uv run mypy src/fieldkit/ hooks/*.py --no-error-summary

# docs — regenerate auto-generated docs from live repo state
docs:
	uv run python scripts/generate_cli_docs.py
	uv run python scripts/generate_dep_map.py

# docs-examples — execute fixed automated owners; manual/live proofs remain explicit pending evidence.
docs-examples:
	uv run python scripts/check_documentation_examples.py

# docs-site — build exactly the public documentation surface; strict warnings fail.
docs-site:
	uv run python scripts/check_documentation_contract.py
	$(MAKE) docs-examples
	uv run mkdocs build --strict --site-dir build/site
	uv run python scripts/check_public_docs.py build/site

# changelog — fold changelog.d/*.md fragments into CHANGELOG.md, then delete them.
# PRs never edit CHANGELOG.md directly; they add a fragment (changelog.d/README.md).
# That is what keeps concurrent branches from colliding on the [Unreleased] block.
changelog:
	uv run python scripts/build_changelog.py

# changelog-preview — same assembly, printed to stdout; writes nothing.
changelog-preview:
	uv run python scripts/build_changelog.py --dry-run

# quality — bounded PR/developer validation. Full enforcement lives in quality-full.
# QUALITY_BASE must identify the reviewed base revision; never silently compare to an arbitrary ref.
QUALITY_BASE ?= $(shell git merge-base HEAD origin/main 2>/dev/null)

define RUN_QUALITY_STAGE
	@uv run python scripts/quality_stage.py --label "$(1)" $(2) -- $(3)
endef

define RUN_FULL_QUALITY_STAGE
	@uv run python scripts/quality_stage.py --label "$(1)" --full-enforcement $(2) -- $(3)
endef

quality:
	@if [ -z "$(QUALITY_BASE)" ]; then \
		echo "ERROR: quality requires QUALITY_BASE or origin/main" >&2; \
		exit 2; \
	fi
	$(call RUN_QUALITY_STAGE,ruff-check,--quality-base "$(QUALITY_BASE)",uv run ruff check .)
	$(call RUN_QUALITY_STAGE,ruff-format,--quality-base "$(QUALITY_BASE)",uv run ruff format --check .)
	$(call RUN_QUALITY_STAGE,markdown-links,--quality-base "$(QUALITY_BASE)",uvx pre-commit==4.6.1 run markdown-link-check --all-files)
	$(call RUN_QUALITY_STAGE,docs-site,--quality-base "$(QUALITY_BASE)",make docs-site)
	$(call RUN_QUALITY_STAGE,claude-sync,--quality-base "$(QUALITY_BASE)",uv run python scripts/sync_claude_dir.py --check)
	$(call RUN_QUALITY_STAGE,mypy,--quality-base "$(QUALITY_BASE)",uv run mypy src/fieldkit/ hooks/*.py --no-error-summary)
	$(call RUN_QUALITY_STAGE,tach,--quality-base "$(QUALITY_BASE)",uvx tach check)
	$(call RUN_QUALITY_STAGE,dependency-profiles,--quality-base "$(QUALITY_BASE)",uv run python scripts/check_dependency_profiles.py)
	$(call RUN_QUALITY_STAGE,compatibility-policy,--quality-base "$(QUALITY_BASE)",uv run python scripts/check_compatibility_policy.py)
	$(call RUN_QUALITY_STAGE,public-identity,--quality-base "$(QUALITY_BASE)",uv run python scripts/check_public_identity.py)
	$(call RUN_QUALITY_STAGE,public-tree-safety,--quality-base "$(QUALITY_BASE)",make public-tree-safety)
	$(call RUN_QUALITY_STAGE,workflow-security,--quality-base "$(QUALITY_BASE)",uv run python scripts/check_workflow_security.py)
	$(call RUN_QUALITY_STAGE,release-workflow-policy,--quality-base "$(QUALITY_BASE)",uv run python scripts/release_workflow_policy.py)
	$(call RUN_QUALITY_STAGE,supply-chain-policy,--quality-base "$(QUALITY_BASE)",uv run python scripts/check_supply_chain_policy.py policy)
	$(call RUN_QUALITY_STAGE,release-policy,--quality-base "$(QUALITY_BASE)",uv run python scripts/check_release.py policy)
	$(call RUN_QUALITY_STAGE,quality-contract,--quality-base "$(QUALITY_BASE)",uv run pytest tests/test_quality_contract.py -q -n 0)
	$(call RUN_QUALITY_STAGE,impact-pytest,--quality-base "$(QUALITY_BASE)" --skip-when-docs-only,uv run pytest tests/ --tach --tach-base "$(QUALITY_BASE)" -q -n 0)

# quality-full — complete merge/scheduled enforcement. pytest runs once and writes coverage.json;
# Gazepy reads it (no second pytest run). Do not move individual gates out of this target.
quality-full:
	$(call RUN_FULL_QUALITY_STAGE,ruff-check,,uv run ruff check .)
	$(call RUN_FULL_QUALITY_STAGE,ruff-format,,uv run ruff format --check .)
	$(call RUN_FULL_QUALITY_STAGE,markdown-links,,uvx pre-commit==4.6.1 run markdown-link-check --all-files)
	$(call RUN_FULL_QUALITY_STAGE,docs-site,,make docs-site)
	$(call RUN_FULL_QUALITY_STAGE,mypy,,uv run mypy src/fieldkit/ hooks/*.py --no-error-summary)
	$(call RUN_FULL_QUALITY_STAGE,tach,,uvx tach check)
	$(call RUN_FULL_QUALITY_STAGE,dependency-profiles,,uv run python scripts/check_dependency_profiles.py)
	$(call RUN_FULL_QUALITY_STAGE,compatibility-policy,,uv run python scripts/check_compatibility_policy.py)
	$(call RUN_FULL_QUALITY_STAGE,public-identity,,uv run python scripts/check_public_identity.py)
	$(call RUN_FULL_QUALITY_STAGE,public-tree-safety,,make public-tree-safety)
	$(call RUN_FULL_QUALITY_STAGE,workflow-security,,uv run python scripts/check_workflow_security.py)
	$(call RUN_FULL_QUALITY_STAGE,release-workflow-policy,,uv run python scripts/release_workflow_policy.py)
	$(call RUN_FULL_QUALITY_STAGE,supply-chain-policy,,uv run python scripts/check_supply_chain_policy.py policy)
	$(call RUN_FULL_QUALITY_STAGE,release-policy,,uv run python scripts/check_release.py policy)
	$(call RUN_FULL_QUALITY_STAGE,skill-integrity,,uv run python scripts/check_skill_integrity.py --report reports/skill-integrity.json)
	$(call RUN_FULL_QUALITY_STAGE,claude-sync,,uv run python scripts/sync_claude_dir.py --check)
	$(call RUN_FULL_QUALITY_STAGE,click-params,,uv run python scripts/check_click_params.py src/fieldkit/commands/)
	$(call RUN_FULL_QUALITY_STAGE,cli-docs,,uv run python scripts/generate_cli_docs.py --check)
	$(call RUN_FULL_QUALITY_STAGE,dependency-map,,uv run python scripts/generate_dep_map.py --check)
	$(call RUN_FULL_QUALITY_STAGE,documentation-contract,,uv run python scripts/check_documentation_contract.py)
	$(call RUN_FULL_QUALITY_STAGE,schema-sync,,uv run python scripts/check_schema_sync.py)
	$(call RUN_FULL_QUALITY_STAGE,changelog-fragment,,uv run python scripts/check_changelog_fragment.py)
	$(call RUN_FULL_QUALITY_STAGE,skillsaw,,uv run python scripts/check_skillsaw.py)
	$(call RUN_FULL_QUALITY_STAGE,pytest-coverage,,uv run pytest tests/ --cov --cov-report=term-missing --cov-report=json:coverage.json -q -n $(PYTEST_XDIST_WORKERS))
	# Gaze CRAP + contract-coverage gate: re-enabled on gaze-py 0.8.2, which fixed the
	# bare-function-name test-target pairing bug (source_names = {fn.function for fn
	# in source_functions} in pairing.py matched any two functions sharing a name
	# across files/classes, e.g. GHIssueStore.add_note vs. a new top-level add_note,
	# misattributing contract coverage and producing false-positive CRAP regressions
	# unrelated to the actual diff — confirmed via reproducible repro during D1 Wave 3
	# PR1). Baseline (.gaze/baseline.json) regenerated from lean coverage on 0.9.3
	# after its assertion-owner fix corrected cross-function attribution (gaze-py #82).
	# TWO calls, deliberately. Keeping the regression baseline and absolute ceiling
	# independently visible makes both policies explicit and avoids baseline/ceiling
	# flag interaction.
	# The second call must NOT get --baseline: gaze-py also auto-discovers
	# <project_root>/.gaze/baseline.json, and project_root here resolves to
	# src/fieldkit/ (no .gaze/ there), which is what leaves the ceiling active.
	$(call RUN_FULL_QUALITY_STAGE,gazepy-baseline,,uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --baseline .gaze/baseline.json)
	$(call RUN_FULL_QUALITY_STAGE,gazepy-ceiling,,uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 67)
	$(call RUN_FULL_QUALITY_STAGE,gazepy-contract-coverage,,uv run gazepy quality src/fieldkit/ --min-contract-coverage 50)
	# agentready: assess a clean HEAD worktree (no .venv/worktree pollution) then
	# check gate — matches CI exactly. See scripts/agentready_assess.py.
	$(call RUN_FULL_QUALITY_STAGE,agentready-assess,,uv run python scripts/agentready_assess.py agentready==2.49.0)
	$(call RUN_FULL_QUALITY_STAGE,agentready-check,,uv run python scripts/check_agentready.py)
	# Behavioral eval smoke test (NO_LLM=1 — no API calls, verifies harness wiring).
	$(call RUN_FULL_QUALITY_STAGE,behavioral-skill-eval,--env NO_LLM=1,uv run fieldkit skill eval --behavioral --all)
	# D3 flag contract: hard gate. Rules 1-2 (declared external needs --confirm,
	# declared workspace needs --dry-run) plus the --json coverage ratchet all
	# block. Naming individual commands that lack --json stays advisory; only the
	# floor is enforced, so rollout stays incremental but cannot regress.
	$(call RUN_FULL_QUALITY_STAGE,flag-contract,,uv run python scripts/check_flag_contract.py)

# coverage.json — generate JSON coverage report (prerequisite for make gazepy)
# Not .PHONY: make will skip regeneration if coverage.json is newer than all sources.
coverage.json: $(shell find src/fieldkit/ -name '*.py')
	uv run pytest tests/ --cov=fieldkit --cov-report=json:coverage.json -q

# audit-check — verify codebase against audit-derived standards (8 checks, A01-A08)
# Run independently or as part of a pre-PR checklist.
audit-check:
	uv run python scripts/audit_check.py

# gazepy — the full Gaze gate. `make quality-full` includes it; run this target independently
# when only the Gaze checks are needed.
# Two gates, two independent AST parses (~30s each; gazepy 0.8.2 has no combined pass):
#   1. crap: CRAP regression vs the committed baseline (.gaze/baseline.json), plus a
#      --max-crapload 67 runaway backstop (current crapload = 58 on gaze-py 0.9.1's
#      per-function line coverage; 0 = "no limit"). Two tool measurement bugs have
#      moved this number, both fixed upstream, neither a change in this codebase:
#      0.8.2 fixed bare-function-name test-target pairing (misattributed coverage
#      across same-named functions in different files/classes), taking the count
#      to a ceiling of 24; 0.9.0 fixed line coverage being attributed per FILE and
#      applied to every function in it, taking it to 84. Baseline regenerated from
#      CI's lean coverage on 0.9.1. Cleanup then lowered it 84 -> 67 (constitution
#      1.7.0, 2026-08-02): five tests-only PRs clearing the contact/_enrich_helpers,
#      config/_loader, driver/github, issue/gh_store and pipeline/quota clusters.
#      Track A decomposition then took the measured count 67 -> 58 (4b2c436c); the
#      ceiling stays at 67 until a constitution amendment ratchets it down.
#      Ratchet this down further as flagged functions (see `recommended_actions` in
#      `gazepy crap --format json`) get tested/decomposed.
#      Of the 58, Track A lists 15 as decomposition candidates — one row per
#      function in openspec/changes/crap-track-a-decomposition/design.md. Its
#      `c` column is gaze's own cyclomatic complexity. Do NOT size that work
#      with ruff's mccabe: the two counters disagree, and ruff reads LOWER —
#      on _cmd_install ruff said 10 where gaze said 16, then 7 where gaze said
#      10. Under-reporting is the dangerous direction, because a function ruff
#      calls a comfortable 10 can still be flagged at gaze 15+. Get the gate's
#      own number; `crap` prints it per function, and complexity (unlike the
#      crapload) is coverage-independent, so a local run reads it exactly:
#        uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 67 | grep <fn>
#      The gate counts crap >= 15.0 inclusive and crap == complexity at 100%
#      coverage, so these 15 cannot be cleared by tests alone. The other 43 can.
#      NOT gated: the same run also reports gaze_crapload = 80 — gaze-py's own
#      complexity counting, which is not ruff's mccabe. It has its own separate
#      --max-gaze-crapload flag that this repo never passes, so it defaults to
#      0 = "no limit". Whether leaving it ungated is intentional has never been
#      recorded; starting to gate it would be a new gate and needs an amendment.
#      Do not read gate output as if the two numbers were one.
#   2. quality: average contract coverage >= 50%.
# Depends on coverage.json — reuses it when newer than sources (no re-test), so this is
# the fast gate-only path: run it directly instead of the full `make quality` while
# iterating on gaze findings.
gazepy: coverage.json
	# TWO crap calls — see the note on the `quality-full` target. Keep the regression
	# baseline and absolute ceiling as independently visible gates. Do not merge them.
	uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --baseline .gaze/baseline.json
	uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 67
	uv run gazepy quality src/fieldkit/ --min-contract-coverage 50

# gaze-baseline — regenerate the committed lean CRAP baseline (.gaze/baseline.json).
# `gazepy crap --format json` emits a ~4MB payload; the baseline comparator only reads
# target + crap + gaze_crap, so we trim to those fields to keep the tracked file ~440KB.
# Run after deliberately lowering CRAP (to lock the gain in), then commit .gaze/baseline.json.
# Known blind spot: gaze-py keys matches on `package:function`, so the 5 same-named
# function pairs in one file (e.g. shadowbot/client.py:query) can mismatch; the
# --max-crapload backstop still catches either twin crossing CRAP 15.
#
# ENVIRONMENT SENSITIVITY — read before committing a regenerated baseline.
# CRAP is a function of coverage, so the baseline encodes the environment that
# measured it. Data-dependent tests (`needs_data`, and
# tests/integration/test_pipeline.py, which skips without an accounts config)
# run on an operator machine but skip in CI, so a locally generated baseline
# reads *higher* coverage than CI can reproduce and lands as hundreds of
# phantom sub-0.1 "regressions" on the next PR.
# The committed baseline is therefore pinned to the LEAN (CI) environment.
# NOTE: a data-rich local run mostly reports improvements, but NOT only
# improvements — the skew is not uniformly in one direction. Measured against
# the 2026-08-02 baseline: 17 improvements but also 2 regressions,
# _run_people_index_step and _load_accounts_yaml, because local coverage of
# commands/datasync/cli.py is *lower* than CI's (86.34% vs 87.98%) — some tests
# run in CI and skip locally, the opposite of the assumption above. So a red
# local `make quality` at the baseline step does NOT by itself mean you broke
# something; reproduce against CI's coverage artifact before believing it.
# To regenerate, prefer CI's own coverage over local coverage.json:
#   gh run download <run-id> -n coverage-json
#   uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --format json | <trim, as below>
# Regenerating from a local run is only safe on a machine with no fieldkit-data
# and no accounts config.
gaze-baseline: coverage.json
	@mkdir -p .gaze
	uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --format json | \
	  uv run python -c "import json,sys; d=json.load(sys.stdin); json.dump({'summary': d['summary'], 'results': [{'target': r['target'], 'crap': r.get('crap'), 'gaze_crap': r.get('gaze_crap')} for r in d['results']]}, sys.stdout, indent=1, sort_keys=True)" > .gaze/baseline.json
	@echo "gaze-baseline: wrote .gaze/baseline.json (crapload backstop unchanged; review the diff before committing)"

# gaze-report — advisory AI-synthesized CRAP narrative via `gazepy report` and the
# `ai:` block in .gaze.yaml (Vertex ADC). On-demand only: NOT part of `make quality`
# or CI, so it never spends tokens on the gate path. Two degradation cases differ:
#   - ai: block empty/unconfigured  → prompt-only mode: emits the JSON payload, exit 0.
#   - ai: configured but ADC/API fails (expired token, 400, network) → gazepy RAISES;
#     the `|| true` keeps this target non-blocking, but NO narrative is emitted.
# Refresh creds with `gcloud auth application-default login` if you want the narrative.
# CI has no Vertex auth, so a CI narrative would be prompt-only. Scope to a module
# (e.g. `gazepy report src/fieldkit/pursuit/`) to cut both wall-clock and tokens.
gaze-report: coverage.json
	uv run gazepy report src/fieldkit/ --coverprofile coverage.json || true

# update-schema — show the Pydantic-generated schema for manual review.
# After adding a new sf_* field to PursuitFrontmatter, run this to see the new field's
# JSON schema shape, then manually add it to src/fieldkit/_data/pursuit-frontmatter.schema.json.
# The committed schema is hand-crafted (allows "" for monetary fields that SF sync writes as null);
# do not blindly replace it with the Pydantic output.
update-schema:
	@echo "=== Live Pydantic schema (for reference) ==="
	uv run python -c "import json, sys; sys.path.insert(0, 'src'); from fieldkit.pursuit.models import PursuitFrontmatter; print(json.dumps(PursuitFrontmatter.model_json_schema(), sort_keys=True, indent=2))"
	@echo ""
	@echo "=== Missing fields (run check_schema_sync.py for details) ==="
	uv run python scripts/check_schema_sync.py || true

# hooks — install all git hooks (pre-commit + pre-push branch protection)
# Run after cloning or after 'uvx pre-commit install --hook-type pre-push' overwrites
# .git/hooks/pre-push with the pre-commit wrapper.
HOOK_INSTALL_LOCK_TIMEOUT_SECONDS ?= 30
HOOK_INSTALL_COMMAND_TIMEOUT_SECONDS ?= 120
HOOK_INSTALL_KILL_AFTER_SECONDS ?= 5
export HOOK_INSTALL_LOCK_TIMEOUT_SECONDS
export HOOK_INSTALL_COMMAND_TIMEOUT_SECONDS
export HOOK_INSTALL_KILL_AFTER_SECONDS

hooks:
	@uv run python -m scripts.install_git_hooks
	@echo "hooks: pre-commit, pre-push, and post-commit hooks installed"
	@echo "hooks: direct push to main is now blocked"
	@echo "hooks: fieldkit will auto-reinstall after commits touching src/fieldkit/"

# eval-skills — run LLM-graded behavioral evals against all skills (live run)
# Cheap run: FIELDKIT_LLM_MODEL=vertex_ai/claude-haiku-4-5@20251001 make eval-skills
# Note: uses 'uv run fieldkit' — no 'make install' required; the contributor sync is sufficient.
eval-skills:
	uv run fieldkit skill eval --behavioral --all

# mutation-report — sensor only, NOT part of quality/verify. Reports per-file
# mutation kill rate for the three modules configured in [tool.mutmut] (pyproject.toml).
mutation-report:
	uv run mutmut run
	uv run python scripts/mutation_kill_rate.py
