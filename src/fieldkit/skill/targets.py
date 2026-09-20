"""Tool target registry for project-local skill installation.

Defines the supported AI tool targets (OpenCode, Claude Code, Cursor, Gemini
CLI), their detection paths, and the project-local install path template for
each.  Also defines the canonical skill category groupings used by the
interactive ``fieldkit skill install`` prompt.

Import constraint: MUST NOT import from ``fieldkit/`` or ``hooks/``.
Only stdlib, third-party, and declared fieldkit modules are permitted.
Enforced by tach.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ToolTarget:
    """Describes a supported AI tool's project-local skill install location.

    Attributes:
        label:      Human-readable display name shown in the install prompt.
        detect:     Directory name to look for in CWD to auto-detect the tool.
        skill_root: Project-relative directory containing installed skills.
        skill_path: Path template relative to ``skill_root``.
        manifest_name: Installer ownership manifest filename.
        global_skill_root: Optional home-relative global skill directory.
        format:     Install format: ``"directory"`` or ``"flat"``.
    """

    label: str
    detect: str
    skill_root: str
    skill_path: str
    manifest_name: str
    format: Literal["directory", "flat"]
    global_skill_root: str | None = None


@dataclass(frozen=True)
class InstallTarget:
    """A validated install destination with canonical filesystem paths."""

    key: str
    label: str
    skill_root: Path
    manifest_path: Path
    skill_path: str
    format: Literal["directory", "flat"]

    def path_for(self, skill_name: str) -> Path:
        """Return the canonical destination path for *skill_name*."""
        return self.skill_root / self.skill_path.format(name=skill_name)


GLOBAL_SKILL_NAMES: frozenset[str] = frozenset({"handoffs", "pickup"})


TOOL_TARGETS: dict[str, ToolTarget] = {
    "opencode": ToolTarget(
        label="OpenCode",
        detect=".opencode",
        skill_root=".opencode/skills",
        skill_path="{name}/SKILL.md",
        manifest_name=".fieldkit-install-manifest.json",
        format="directory",
        global_skill_root=".agents/skills",
    ),
    "claude-code": ToolTarget(
        label="Claude Code",
        detect=".claude",
        skill_root=".claude/skills",
        skill_path="{name}/SKILL.md",
        manifest_name=".fieldkit-install-manifest.json",
        format="directory",
        global_skill_root=".claude/skills",
    ),
    "cursor": ToolTarget(
        label="Cursor",
        detect=".cursor",
        skill_root=".cursor/rules",
        skill_path="{name}.md",
        manifest_name=".fieldkit-install-manifest.json",
        format="flat",
    ),
    "gemini": ToolTarget(
        label="Gemini CLI",
        detect=".gemini",
        skill_root=".gemini/skills",
        skill_path="{name}/SKILL.md",
        manifest_name=".fieldkit-install-manifest.json",
        format="directory",
    ),
}

# When adding a new skill to fieldkit/skills/, add it to SKILL_CATEGORIES here.
# The test_all_skills_categorised test enforces complete coverage.
SKILL_CATEGORIES: dict[str, list[str]] = {
    # brief carries week-start, week-end and update as on-demand ops under
    # brief/ops/ (D1 Wave 5 fold); they are no longer installable skills.
    "SALES WORKFLOWS": [
        "brief",
        "post-meeting",
    ],
    "ACCOUNT INTELLIGENCE": [
        "grill",
        "pipeline",
        "sf-sync",
    ],
    "MEETING & PURSUIT": [
        "followup-draft",
        "meeting",
        "pursuit-advance",
        "pursuit-auditor",
    ],
    "CONTENT & PROPOSALS": [
        "contract",
    ],
    "CONTACTS & ENGAGEMENT": [
        "contact",
        "workstream-discover",
    ],
    "DATA & INTEGRATIONS": [
        "companion",
        "ingest",
        "memory-management",
        "task-management",
        "task-sync",
        "tool-routing",
    ],
    "UTILITIES": [
        "always-on-guidance",
        "create-cli",
        "draft-review",
        "handoffs",
        "humanizer",
        "pickup",
        "slack-digest",
        "start",
        "win-loss",
    ],
}
