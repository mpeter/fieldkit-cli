.PHONY: verify test lint bootstrap contributor-hooks pr-check install reinstall install-health artifact-check release-check release-policy-check release-workflow-policy-check public-tree-safety quality quality-full gazepy gaze-baseline gaze-report audit-check hooks update-schema eval-skills install-skills docs docs-site changelog changelog-preview mutation-report

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
	uv run python -m scripts.check_documentation_examples

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

quality:
	@uv run python scripts/quality_gate.py run --tier pr --base "$(QUALITY_BASE)" \
		--workers "$(PYTEST_XDIST_WORKERS)" --receipt reports/quality-pr.json

# quality-full — contributor wrapper for the complete fixed-argv plan. Pytest runs once and
# writes coverage.json; Gazepy reads it without a second test run.
quality-full:
	@uv run python scripts/quality_gate.py run --tier full \
		--workers "$(PYTEST_XDIST_WORKERS)" --receipt reports/quality-full.json

# coverage.json — generate JSON coverage report (prerequisite for make gazepy)
# Not .PHONY: make will skip regeneration if coverage.json is newer than all sources.
coverage.json: $(shell find src/fieldkit/ -name '*.py')
	uv run pytest tests/ --cov=fieldkit --cov-report=json:coverage.json -q

# audit-check — verify codebase against audit-derived standards (8 checks, A01-A08)
# Run independently or as part of a pre-PR checklist.
audit-check:
	uv run python scripts/audit_check.py

# gazepy — Gaze-only iteration using the coverage.json prerequisite.
# For fresh same-candidate release evidence, use make quality-full, which always
# runs coverage first. A cached coverage file is not proof of the current tests.
# Keep baseline regression, absolute CRAPload, and contract-coverage checks separate.
# Use Gaze's own complexity measurement; Ruff's counter is not interchangeable.
gazepy: coverage.json
	# TWO crap calls — see the note on the `quality-full` target. Keep the regression
	# baseline and absolute ceiling as independently visible gates. Do not merge them.
	uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --baseline .gaze/baseline.json
	uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 67
	uv run gazepy quality src/fieldkit/ --min-contract-coverage 50

# gaze-baseline — maintenance utility, not a way to make a failed gate pass.
# Baseline changes require explicit maintainer review; release thresholds remain
# frozen. Preserve full target identity while trimming the generated score report.
# Regeneration needs fresh coverage from the same isolated test environment and
# candidate as the comparison run, without operator configuration or runtime data.
# Review every changed target and score. Never re-baseline to hide a regression.
gaze-baseline: coverage.json
	@mkdir -p .gaze
	uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --format json | \
	  uv run python -c "import json,sys; d=json.load(sys.stdin); json.dump({'summary': d['summary'], 'results': [{'target': r['target'], 'crap': r.get('crap'), 'gaze_crap': r.get('gaze_crap')} for r in d['results']]}, sys.stdout, indent=1, sort_keys=True)" > .gaze/baseline.json
	@echo "gaze-baseline: wrote .gaze/baseline.json (crapload backstop unchanged; review the diff before committing)"

# gaze-report — optional advisory narrative, never part of a quality gate.
# External synthesis uses the configured provider and requires authorization.
# An unconfigured/unavailable provider can yield prompt-only JSON with exit 0;
# other provider failures can raise. This convenience target masks nonzero exit
# status, so inspect raw diagnostics and never count its exit as release evidence.
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
