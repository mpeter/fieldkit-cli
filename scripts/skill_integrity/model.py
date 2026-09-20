"""Shared model and repository context for skill-integrity validation."""

from dataclasses import dataclass, field
from pathlib import Path

# Shared noun vocabulary — the governance mandate (D1 skill taxonomy).
# Every top-level dir under src/fieldkit/skills/ must appear here.
# A skill is either a noun-root, a noun-verb under a root (future: nested dirs),
# or explicitly listed in SKILL_ROOTS_PENDING for prove-before-subtract evaluation.
#
# To add a new skill: add its name here AND justify in the PR why it belongs
# as a noun-root or judgment skill. Orphan top-level skills are a CI error (N001).
#
# Categories:
#   noun-root: the natural-language face of a CLI noun (Layer 1)
#   judgment:  LLM-only skill with no CLI equivalent (Layer 2b)
#   system:    session/routing infrastructure
#   pending:   awaiting prove-before-subtract or cli-ux-redesign rename
SKILL_ROOTS: frozenset[str] = frozenset(
    {
        # --- noun-roots (Layer 1, mapped to CLI nouns) ---
        "grill",  # pursuit verb (async adjudication)
        "pursuit-auditor",  # pursuit verb
        # pursuit-review: folded into grill (D1 Wave 0.4)
        # pursuit-advance: KEPT by operator ruling (D1 Wave 5). No longer an archive
        # candidate, and not pending anything. Kept *despite* carrying the strongest
        # absorption case in the evidence packet — `fieldkit pursuit advance` is a live
        # CLI command doing nearly the same job. That overlap was weighed and the skill
        # retained anyway; do not re-derive the absorption argument and assume it was
        # overlooked.
        "pursuit-advance",  # pursuit verb
        "sf-sync",  # sf verb
        # brief absorbed week-start, week-end and update as on-demand ops under
        # brief/ops/ (D1 Wave 5, executing the fold Wave 2 only claimed). Their
        # top-level dirs are deleted per R25 — no shim, no alias.
        "brief",  # brief root (renamed from pulse, D1 Wave 2 PR3)
        "pipeline",  # pipeline root
        "contact",  # contact root
        "contract",  # contract root
        "meeting",  # meeting root
        "post-meeting",  # meeting verb (KEPT by operator ruling, D1 Wave 5 — not an archive candidate)
        "task-sync",  # task verb
        "task-management",  # task verb (pending fold)
        # watch-control: archived D1 Wave 2 PR3 (docs/archive/d1-skill-taxonomy-20260720/) —
        # absorbed into `watch --help`/`watch status`.
        "slack-digest",  # watch verb (pending CLI absorption)
        "ingest",  # ingest root (revive; absorbs gmail-refresh, D1 Wave 4 PR3)
        # companion: RETAINED (D1 Wave 5, reversing the earlier "approved archive" call).
        # The supersession rationale ("superseded by /loop") was never verifiable — `/loop`
        # has zero hits anywhere in this repo, and `loop` is in fact a *subcommand of
        # companion itself* (`companion loop`), not a successor to it. The skill is the
        # natural-language face of the live `companion` CLI noun (feed/allowed/run/loop).
        "companion",  # companion root
        # --- judgment skills (Layer 2b, no CLI noun) ---
        "draft-review",  # draft verb (judgment)
        "humanizer",  # draft verb (judgment, pending fold)
        "followup-draft",  # draft verb (judgment)
        "create-cli",  # CLI interface design judgment
        # proposal-generate, create-asset: archived D1 Wave 5 — genuinely unused, NOT
        # absorbed. Pre-mortem finding C4 (docs/skill-premortem-2026-07-18.md) retired
        # the "sweep absorbed them" framing as fiction; no absorbing capability exists.
        # --- system / session ---
        "always-on-guidance",  # session discipline (always active, not slash-invoked)
        "start",  # session bootstrap
        "tool-routing",  # agent routing infrastructure (absorbs managing-google-workspace, D1 Wave 4 PR3)
        "memory-management",  # session memory
        # proctor: unshipped 2026-07-25 — dev-only review pipeline, lives in
        # .opencode/skills/proctor/ (OpenCode-only), never installed to the workspace.
        "handoffs",  # session handoff (write)
        "pickup",  # session handoff (resume)
        # --- kept by operator ruling (D1 Wave 5), no longer archive candidates ---
        # These closed the "4 undecided ops" enactment by ruling rather than by running
        # it; win-loss in particular was flagged as a possible seasonal trap, and the
        # ruling resolves that in favour of keeping.
        "win-loss",  # pursuit verb
        "workstream-discover",  # pursuit verb
    }
)

# Violation metadata: (severity, fixable)
VIOLATION_META: dict[str, tuple[str, bool]] = {
    "S001": ("error", True),  # partial: name field only
    "S002": ("error", True),
    "S003": ("error", True),
    "S004": ("error", True),
    "R001": ("error", True),
    "R002": ("error", False),
    "R003": ("error", False),
    "R004": ("error", False),
    "R005": ("error", False),
    "R006": ("error", False),
    "R007": ("error", False),
    "G001": ("error", False),
    "G002": ("warning", False),
    "C001": ("error", True),
    "C002": ("error", True),
    "N001": ("error", False),
}

VIOLATION_SUGGESTIONS: dict[str, str] = {
    "S001": "Add the missing frontmatter field",
    "S002": "Set name: to match the directory name",
    "S003": "Add slash: true to frontmatter if the skill should be slash-invocable; omit the field otherwise",
    "S004": "Correct the field type (slash must be a boolean)",
    "R001": "Remove the dead Related Skills entry or create the missing skill",
    "R002": "Add the MCP group to opencode.json mcp section",
    "R003": "Create the missing agent file or remove the hardcoded reference",
    "R004": "Create the missing command file or remove the reference",
    "R005": "Create the missing file or remove the path reference",
    "R006": "Create the missing skill or remove the skills_use() call",
    "R007": "Update the stale command path or mark an intentional invalid example with ignore-next",
    "G001": "Break the cycle by removing one of the circular Related Skills references",
    "G002": "Add slash: true if the skill should be slash-invocable, or reference from an agent/command/skill; delete if dead weight",
    "C001": "Update the coverage floor declaration to match pyproject.toml",
    "C002": "Update the CRAP threshold declaration to match Makefile",
    "N001": "Add the skill to SKILL_ROOTS in scripts/skill_integrity/model.py with a category comment, or remove the orphan directory",
}


@dataclass(frozen=True)
class IntegrityContext:
    """All repository-owned inputs consumed by the integrity validator."""

    repo_root: Path
    project_skills_dir: Path
    fieldkit_skills_dir: Path
    agents_dir: Path
    commands_dir: Path
    packs_dir: Path
    opencode_json: Path
    project_opencode_json: Path
    pyproject_toml: Path
    makefile: Path
    skill_roots: frozenset[str]

    @classmethod
    def from_root(cls, root: Path, *, skill_roots: frozenset[str] = SKILL_ROOTS) -> "IntegrityContext":
        return cls(
            repo_root=root,
            project_skills_dir=root / ".opencode" / "skills",
            fieldkit_skills_dir=root / "src" / "fieldkit" / "skills",
            agents_dir=root / ".opencode" / "agents",
            commands_dir=root / ".opencode" / "commands",
            packs_dir=root / ".opencode" / "uf" / "packs",
            opencode_json=root / "opencode.json",
            project_opencode_json=root / ".opencode" / "opencode.jsonc",
            pyproject_toml=root / "pyproject.toml",
            makefile=root / "Makefile",
            skill_roots=skill_roots,
        )


@dataclass
class Violation:
    """A single integrity violation found in the corpus."""

    code: str
    file: str  # repo-relative path
    line: int | None
    message: str

    @property
    def severity(self) -> str:
        return VIOLATION_META[self.code][0]

    @property
    def fixable(self) -> bool:
        return VIOLATION_META[self.code][1]

    @property
    def suggestion(self) -> str:
        return VIOLATION_SUGGESTIONS[self.code]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "fixable": self.fixable,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class Registry:
    """Symbol tables built from the filesystem corpus."""

    project_skills: dict[str, Path] = field(default_factory=dict)
    fieldkit_skills: dict[str, Path] = field(default_factory=dict)
    agents: set[str] = field(default_factory=set)
    commands: set[str] = field(default_factory=set)
    mcp_groups: set[str] = field(default_factory=set)
    coverage_floor: int = 80
    crap_threshold: int = 1
