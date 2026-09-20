"""fieldkit ingest promote — interactively promote meeting action items to TASKS.md.

Shows each action item from a processed meeting note with a suggested classification,
lets the user confirm, override, or skip, then writes confirmed items to TASKS.md.

Usage:
    fieldkit ingest promote <meeting-file>
    fieldkit ingest promote --recent          # promote from last N meetings
"""

import re
from pathlib import Path
from typing import Any

import click
import yaml

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.pursuit.io import extract_frontmatter_text

# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_DISPLAY_OVERRIDES: dict[str, str] = {
    "globalpay": "GlobalPay",
    "rhoai": "RHOAI",
    "aep": "AEP",
    "eda": "EDA",
    "aap": "AAP",
    "ocp": "OCP",
    "eap": "EAP",
    "hcs": "HCS",
    "ads": "ADS",
    "sow": "SOW",
}


def _display(slug: str) -> str:
    return " ".join(_DISPLAY_OVERRIDES.get(p.lower(), p.title()) for p in slug.replace("-", " ").split())


def _pursuit_label(account: str, pursuits: list[str]) -> str:
    acct = _DISPLAY_OVERRIDES.get(account.lower(), account.replace("-", " ").title())
    if pursuits:
        return f"{acct} / {_display(pursuits[0])}"
    return acct


# ---------------------------------------------------------------------------
# Suggestion engine — lightweight, no classification machinery
# ---------------------------------------------------------------------------

_HIGH_CONFIDENCE_RE = re.compile(
    r"\b(docusign|purchase order|\bpo\b|sow|statement of work|contract|signature|sign off"
    r"|amend|approv|budget|funding|milestone|close date|mvp|july 1|q[1-4]\b"
    r"|proposal|quote|pricing|fiscal|payment|invoice)\b",
    re.IGNORECASE,
)


def _suggest(item: str, user_name: str, user_email: str) -> str:
    """Return 'm' (mine), 'w' (waiting_on hint), or 's' (skip hint)."""
    # Owner extraction — name before colon/dash/to
    m = re.match(r"^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)\s*[:\-]", item)
    owner = m.group(1).lower() if m else ""
    user_lower = user_name.lower()
    user_local = user_email.split("@")[0].lower()

    if owner and (
        owner in user_lower or user_local in owner or any(t in owner for t in user_lower.split() if len(t) > 2)
    ):
        return "m"
    if _HIGH_CONFIDENCE_RE.search(item):
        return "w"
    return "s"


# ---------------------------------------------------------------------------
# Interactive prompt for one item
# ---------------------------------------------------------------------------

_LABEL = {
    "m": click.style("MY TASK  ", fg="green", bold=True),
    "w": click.style("WAITING  ", fg="yellow", bold=True),
    "s": click.style("skip     ", fg="bright_black"),
}

_CHOICE_MAP = {
    "m": "m",
    "mine": "m",
    "my": "m",
    "1": "m",
    "w": "w",
    "wait": "w",
    "waiting": "w",
    "2": "w",
    "s": "s",
    "skip": "s",
    "n": "s",
    "3": "s",
    "q": "q",
    "quit": "q",
}


def _prompt_item(idx: int, total: int, item: str, suggestion: str) -> str:
    """Show one action item and return the user's choice: m/w/s/q.

    Returns "q" on stdin EOF or Ctrl-C (click.exceptions.Abort) so the caller
    can treat non-interactive / CI contexts as a graceful quit rather than a
    crash.  This mirrors the EOFError guard in reprocess._prompt_reprocess_choice.
    """
    click.echo("")
    click.echo(f"  [{idx}/{total}] {item}")
    click.echo(f"         Suggested: {_LABEL[suggestion]}  (m)ine  (w)aiting-on  (s)kip  (q)uit")
    while True:
        try:
            # implementation change: catch Abort (Ctrl-C or stdin EOF signalled by Click) and
            # plain EOFError (piped stdin exhausted before click.prompt reads it).
            # Both are treated as "q" so the existing quit path is reused cleanly.
            raw = (
                click.prompt("         Choice", default=suggestion, show_default=True, prompt_suffix=" → ")
                .strip()
                .lower()
            )
        except (click.exceptions.Abort, EOFError):
            return "q"
        choice = _CHOICE_MAP.get(raw)
        if choice:
            return choice
        click.echo("         Please enter m, w, s, or q.")


# ---------------------------------------------------------------------------
# TASKS.md writers
# ---------------------------------------------------------------------------


def _write_active(tasks_path: Path, item: str, pursuit_label: str) -> None:
    content = tasks_path.read_text(encoding="utf-8")
    tag = f"**[{pursuit_label}]** " if pursuit_label else ""
    entry = f"- {tag}{item}"
    if item[:60] in content:
        click.echo("         → already in TASKS.md, skipped")
        return
    m = re.search(r"^## Active\s*$", content, re.MULTILINE)
    if m:
        insert = m.end()
        rest = content[insert:]
        cm = re.match(r"\n<!--[^\n]*-->\n?", rest)
        if cm:
            insert += cm.end()
        content = content[:insert] + "\n" + entry + content[insert:]
    else:
        content += f"\n## Active\n{entry}\n"
    tasks_path.write_text(content, encoding="utf-8")


def _write_waiting(tasks_path: Path, item: str, pursuit_label: str, meeting_title: str, meeting_date: str) -> None:
    content = tasks_path.read_text(encoding="utf-8")
    tag = f"**[{pursuit_label}]** " if pursuit_label else ""
    # Strip owner prefix for the "re:" description
    topic = re.sub(r"^[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*\s*[:\-]\s*", "", item).strip()
    # Extract owner
    om = re.match(r"^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)\s*[:\-]", item)
    owner_str = f" {om.group(1)}" if om else ""
    entry = f"- {tag}Waiting on{owner_str} re: {topic} — from {meeting_title} ({meeting_date})"
    if item[:60] in content:
        click.echo("         → already in TASKS.md, skipped")
        return
    m = re.search(r"^## Waiting On\s*$", content, re.MULTILINE)
    if m:
        insert = m.end()
        rest = content[insert:]
        cm = re.match(r"\n<!--[^\n]*-->\n?", rest)
        if cm:
            insert += cm.end()
        content = content[:insert] + "\n" + entry + content[insert:]
    else:
        content += f"\n## Waiting On\n{entry}\n"
    tasks_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Core promote logic for one meeting file
# ---------------------------------------------------------------------------


def _promote_file(meeting_path: Path, tasks_path: Path, user_name: str, user_email: str) -> bool:
    """Interactively promote action items from one meeting note.

    Returns True if the user chose to quit early.
    """
    text = meeting_path.read_text(encoding="utf-8")
    fm_text = extract_frontmatter_text(text)
    if not fm_text:
        click.echo(f"  No frontmatter found in {meeting_path.name}", err=True)
        return False

    try:
        fm: dict[str, Any] = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        click.echo(f"  YAML error in {meeting_path.name}: {exc}", err=True)
        return False

    action_items = [str(a) for a in (fm.get("action_items") or [])]
    if not action_items:
        click.echo(f"  {meeting_path.name}: no action items, skipping.")
        return False

    account = str(fm.get("account", "unknown"))
    pursuits_list = [str(p) for p in (fm.get("pursuits") or [])]
    meeting_title = str(fm.get("meeting_title", meeting_path.stem))
    meeting_date = str(fm.get("meeting_date", ""))
    label = _pursuit_label(account, pursuits_list)

    click.echo("")
    click.echo(click.style("━" * 70, fg="bright_black"))
    click.echo(click.style(f"  📋 {meeting_title}", bold=True))
    click.echo(f"  {meeting_date}  ·  {meeting_path.name}")
    click.echo(click.style("━" * 70, fg="bright_black"))
    click.echo(f"  {len(action_items)} action item(s)  ·  label: {label}")

    n_mine = n_wait = n_skip = 0
    for i, item in enumerate(action_items, 1):
        suggestion = _suggest(item, user_name, user_email)
        choice = _prompt_item(i, len(action_items), item, suggestion)

        if choice == "q":
            click.echo("  Quitting. Progress saved.")
            return True
        elif choice == "m":
            _write_active(tasks_path, item, label)
            click.echo("         → added to Active")
            n_mine += 1
        elif choice == "w":
            _write_waiting(tasks_path, item, label, meeting_title, meeting_date)
            click.echo("         → added to Waiting On")
            n_wait += 1
        else:
            n_skip += 1

    click.echo("")
    click.echo(f"  Done: {n_mine} active, {n_wait} waiting-on, {n_skip} skipped.")
    return False


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def _help_callback(ctx: click.Context, _param: click.Parameter, val: bool) -> None:
    if val:
        click.echo(ctx.get_help())
        ctx.exit(0)


@click.command("promote")
@click.argument("meeting_file", required=False, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--recent", "-r", default=0, metavar="N", help="Promote from the N most recently written meeting notes (omit FILE)."
)
@click.option("--account", "-a", default=None, help="Filter --recent to a specific account slug.")
# show_help uses expose_value=False — handled by _help_callback, not passed to cli().
@click.option(
    "-h",
    "--help",
    "show_help",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_help_callback,
)
def cli(meeting_file: Path | None, recent: int, account: str | None) -> None:
    """Interactively promote meeting action items to TASKS.md.

    Review each action item from a processed meeting note and choose:

    \b
      m  →  add to Active (it's your task)
      w  →  add to Waiting On (you're waiting on someone else)
      s  →  skip (delivery noise, not relevant to you)
      q  →  quit and save progress

    \b
    Examples:
      fieldkit ingest promote fieldkit-data/accounts/<acct>/meetings/2026-05-13-*.md
      fieldkit ingest promote --recent 5
      fieldkit ingest promote --recent 10 --account <acct>
    """
    from fieldkit.config import get_fieldkit_home
    from fieldkit.config import get_user_email as _get_user_email
    from fieldkit.config import get_user_name as _get_user_name

    # Load user identity
    user_name = _get_user_name()
    user_email = _get_user_email() or ""

    data_root = get_fieldkit_home()
    tasks_path = data_root / "TASKS.md"

    if not tasks_path.exists():
        click.echo(f"Error: TASKS.md not found at {tasks_path}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    # Resolve which files to process
    if meeting_file:
        files = [meeting_file]
    elif recent > 0:
        pattern = "accounts/*/meetings/*.md" if not account else f"accounts/{account}/meetings/*.md"
        candidates = [f for f in data_root.glob(pattern) if f.name != ".gitkeep" and not f.name.startswith(".")]
        # Sort by mtime descending — most recently written first
        candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        files = candidates[:recent]
        if not files:
            click.echo("No meeting files found.")
            raise SystemExit(0)
        click.echo(
            f"Promoting from {len(files)} most recent meeting(s)"
            + (f" in account '{account}'" if account else "")
            + ":"
        )
        for f in files:
            click.echo(f"  {f.relative_to(data_root)}")
    else:
        click.echo("Provide a MEETING_FILE or use --recent N.", err=True)
        raise SystemExit(EXIT_PARTIAL)

    # Process each file
    for f in files:
        quit_early = _promote_file(f, tasks_path, user_name, user_email)
        if quit_early:
            break

    click.echo("")
    click.echo("✓ Promotion complete. Review TASKS.md to verify.")
