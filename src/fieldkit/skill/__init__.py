"""fieldkit.skill — Skill template rendering and installation."""

from fieldkit.skill.judge import (
    SkillJudgement as SkillJudgement,
)
from fieldkit.skill.judge import (
    judge_skill as judge_skill,
)
from fieldkit.skill.template import (
    SkillInstallResult as SkillInstallResult,
)
from fieldkit.skill.template import (
    UnresolvedVariable as UnresolvedVariable,
)
from fieldkit.skill.template import (
    build_template_ctx as build_template_ctx,
)
from fieldkit.skill.template import (
    install_skills as install_skills,
)
from fieldkit.skill.template import (
    render_skill_text as render_skill_text,
)

__all__ = [
    "SkillInstallResult",
    "SkillJudgement",
    "UnresolvedVariable",
    "build_template_ctx",
    "install_skills",
    "judge_skill",
    "render_skill_text",
]
