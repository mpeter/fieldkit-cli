"""Rendering functions for the morning brief.

These functions take pre-collected data strings and format them into
Markdown output sections.
"""


def _render_degraded_section(degraded: list[tuple[str, str]]) -> str:
    """Format degraded sources as a markdown section. Returns empty string when none."""
    if not degraded:
        return ""
    lines = ["### ⚠️ Degraded Sources", ""]
    for label, reason in degraded:
        lines.append(f"- **{label}**: {reason}")
    lines.append("")
    return "\n".join(lines)


def _render_no_llm_brief(
    today_str: str,
    *,
    pursuit_alerts: str,
    stale_prose: str,
    tasks_today: str,
    tasks_waiting: str,
    champion_signals: str,
    decay_signals: str,
    degraded_sources: str = "",
) -> str:
    """Render a brief without LLM synthesis (--no-llm mode)."""
    degraded_block = f"{degraded_sources}\n---\n\n" if degraded_sources else ""
    return f"""## ☀️ Morning Brief — {today_str}

## LLM Sections (omitted — run without --no-llm to generate)
- Overnight Email Triage
- Today's Calendar
- Top 3 Priorities

---

{degraded_block}### 🚦 Pursuit Alerts

{pursuit_alerts}

---

### 📄 Stale Prose Warnings

{stale_prose}

---

### 📋 Today's Commitments

{tasks_today}

---

### ⏳ Waiting On

{tasks_waiting}

---

### 🤝 Relationship Signals

#### Champion Health

{champion_signals}

---

#### Relationship Decay (domain-filtered, ≥10 messages, silent >60d)

{decay_signals}
"""
