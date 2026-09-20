"""Pure Slack search-match classification for the Slack thread watcher."""

import datetime
from typing import Any, TypedDict

_BOT_USERNAME_PATTERNS: tuple[str, ...] = (
    "bot",
    "google drive",
    "google calendar",
    "google docs",
    "aap-911",
    "escalation",
    "pagerduty",
    "opsgenie",
    "jira",
)

_INTERNAL_CHANNEL_PATTERNS: tuple[str, ...] = ("internal-team", "-internal", "-team-")


class SlackThread(TypedDict):
    """The stable alert/state payload for one stale Slack thread."""

    channel: str
    channel_id: str
    ts: str
    message_dt: str
    age_hours: float
    sender_username: str
    permalink: str
    text_snippet: str


class SlackAccountThread(SlackThread):
    """A classified Slack thread attributed to one configured account."""

    account: str


def _ts_to_datetime(ts: str | None) -> datetime.datetime | None:
    """Convert a Slack epoch-string timestamp to a UTC datetime."""
    if ts is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(ts), tz=datetime.UTC)
    except (ValueError, TypeError, OSError):
        return None


def _permalink_for(channel_id: str, ts: str) -> str:
    """Build a best-effort Slack permalink from channel ID and message ts."""
    return f"https://app.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def load_bot_patterns(config: dict[str, Any]) -> frozenset[str]:
    """Return hard-coded and configured bot username patterns."""
    extra: list[str] = config.get("slack_bot_patterns") or []
    combined = set(_BOT_USERNAME_PATTERNS)
    for pattern in extra:
        if isinstance(pattern, str):
            combined.add(pattern.strip().lower())
    return frozenset(combined)


def _is_bot_message(msg: dict[str, Any], sender_username: str, extra_patterns: frozenset[str] | None = None) -> bool:
    """Return whether a Slack message was sent by automation."""
    if msg.get("subtype") == "bot_message" or msg.get("bot_id") or msg.get("app_id"):
        return True
    patterns = extra_patterns if extra_patterns is not None else frozenset(_BOT_USERNAME_PATTERNS)
    return any(pattern in sender_username for pattern in patterns)


def _is_current_user(sender_username: str, sender_user_id: str, current_username: str | None) -> bool:
    """Return whether the message was sent by the current user."""
    return bool(current_username and (sender_username == current_username or sender_user_id == current_username))


def _extract_channel_info(msg: dict[str, Any]) -> tuple[str, str]:
    """Extract a channel name and ID with the watcher's existing fallbacks."""
    channel: dict[str, Any] = msg.get("channel") or {}
    name = channel.get("name") or channel.get("id") or "unknown"
    return str(name), str(channel.get("id") or "")


def _resolve_permalink(msg: dict[str, Any], channel_id: str, ts_str: str) -> str:
    """Return the supplied permalink or construct the existing fallback."""
    permalink = str(msg.get("permalink") or "")
    if not permalink and channel_id and ts_str:
        permalink = _permalink_for(channel_id, ts_str)
    return permalink


def _truncate_text(text: str, max_len: int = 120) -> str:
    """Truncate text with the watcher's existing ellipsis convention."""
    return text[: max_len - 3] + "..." if len(text) > max_len else text


def _rich_text_block_text(block: Any) -> str:
    """Extract the first text value from a supported Slack rich-text block."""
    if not isinstance(block, dict) or block.get("type") != "rich_text":
        return ""
    elements = block.get("elements") or []
    if not elements or not isinstance(elements[0], dict):
        return ""
    inner = elements[0].get("elements") or []
    if not inner or not isinstance(inner[0], dict):
        return ""
    return str(inner[0].get("text") or "").strip()


def _extract_message_text(msg: dict[str, Any]) -> str:
    """Return the first available text from a Slack message payload."""
    text = str(msg.get("text") or "").strip()
    if text:
        return text

    attachments = msg.get("attachments") or []
    if attachments and isinstance(attachments[0], dict):
        attachment_text = str(attachments[0].get("text") or "").strip()
        if attachment_text:
            return attachment_text

    for block in msg.get("blocks") or []:
        block_text = _rich_text_block_text(block)
        if block_text:
            return block_text
    return ""


def _build_thread_result(
    msg: dict[str, Any],
    msg_dt: datetime.datetime,
    age_hours: float,
    sender_username: str,
    sender_user_id: str,
) -> SlackThread:
    """Build the stable result payload for an alertable Slack message."""
    channel_name, channel_id = _extract_channel_info(msg)
    ts_str = str(msg.get("ts") or "")
    return {
        "channel": channel_name,
        "channel_id": channel_id,
        "ts": ts_str,
        "message_dt": msg_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_hours": round(age_hours, 1),
        "sender_username": sender_username or sender_user_id or "unknown",
        "permalink": _resolve_permalink(msg, channel_id, ts_str),
        "text_snippet": _truncate_text(_extract_message_text(msg).strip()),
    }


def classify_message(
    msg: dict[str, Any],
    *,
    current_username: str | None,
    threshold_hours: int,
    now_utc: datetime.datetime,
    extra_bot_patterns: frozenset[str] | None = None,
) -> SlackThread | None:
    """Classify a search match as one stale external thread or ``None``."""
    msg_dt = _ts_to_datetime(msg.get("ts"))
    if msg_dt is None:
        return None

    age_hours = (now_utc - msg_dt).total_seconds() / 3600.0
    if age_hours < threshold_hours:
        return None

    sender_username = str(msg.get("username") or "").strip().lower()
    sender_user_id = str(msg.get("user") or "").strip().lower()
    if _is_bot_message(msg, sender_username, extra_patterns=extra_bot_patterns):
        return None
    if _is_current_user(sender_username, sender_user_id, current_username):
        return None
    return _build_thread_result(msg, msg_dt, age_hours, sender_username, sender_user_id)


def _channel_matches_account(channel_name: str, account_channels: list[str]) -> bool:
    """Return whether a channel contains one of the account channel keywords."""
    normalized_channel = channel_name.lower()
    return any(keyword.lower() in normalized_channel for keyword in account_channels if keyword)
