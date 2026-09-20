"""Validate executable fieldkit command paths cited by agent skills."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fieldkit.cli_registry import walk_cli

from . import model

IGNORE_NEXT = "<!-- fieldkit-command-check: ignore-next -->"
_INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_COMMAND_START = re.compile(r"^\s*(?:\$\s*)?(?:uv run\s+)?fieldkit(?:\s+|$)")
_SHELL_FENCES = {"", "bash", "console", "sh", "shell", "zsh"}


@dataclass(frozen=True)
class CommandReferenceError:
    """One invalid command reference or exception marker."""

    file: Path
    line: int
    message: str


def command_tree() -> dict[tuple[str, ...], bool]:
    """Return command paths and leaf status from the shared Click traversal."""
    return {tuple(node.full_name.split()): node.is_leaf for node in walk_cli()}


def _command_tokens(code: str) -> tuple[str, ...] | None:
    match = _COMMAND_START.match(code)
    if match is None:
        return None
    tokens: list[str] = []
    command_segment = re.split(r"&&|\|\||[;&|]", code[match.end() :], maxsplit=1)[0]
    for word in command_segment.split():
        if word.startswith(("-", "<", "[", "'", '"', "#", "\\", "|", "&", ";", ".", "{", "(")):
            break
        tokens.append(word)
    return tuple(tokens)


def _executable_chunks(text: str) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    in_shell_fence = False
    in_other_fence = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if in_shell_fence or in_other_fence:
                in_shell_fence = False
                in_other_fence = False
            else:
                language = stripped[3:].strip().lower()
                in_shell_fence = language in _SHELL_FENCES
                in_other_fence = not in_shell_fence
            continue
        if in_shell_fence:
            chunks.append((line_number, line))
        elif not in_other_fence:
            chunks.extend((line_number, match.group(1)) for match in _INLINE_CODE.finditer(line))
    return chunks


def _active_marker_lines(text: str) -> list[int]:
    markers: list[int] = []
    in_other_fence = False
    in_shell_fence = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if in_other_fence or in_shell_fence:
                in_other_fence = False
                in_shell_fence = False
            else:
                language = stripped[3:].strip().lower()
                in_shell_fence = language in _SHELL_FENCES
                in_other_fence = not in_shell_fence
            continue
        marker_text = line if in_shell_fence else _INLINE_CODE.sub("", line)
        if not in_other_fence and IGNORE_NEXT in marker_text:
            markers.append(line_number)
    return markers


def _unknown_path(tokens: tuple[str, ...], commands: dict[tuple[str, ...], bool]) -> tuple[str, ...] | None:
    if not tokens:
        return None
    matched: tuple[str, ...] = ()
    for size in range(1, len(tokens) + 1):
        candidate = tokens[:size]
        if candidate not in commands:
            alternatives = tokens[size - 1].split("/")
            prefix = tokens[: size - 1]
            if len(alternatives) > 1 and all(
                (path := (*prefix, alternative)) in commands and commands[path] for alternative in alternatives
            ):
                return None
            if not matched:
                return candidate
            return candidate if not commands[matched] else None
        matched = candidate
        if commands[candidate]:
            return None
    return None


def validate_skill_command_refs(
    skill_files: list[Path], commands: dict[tuple[str, ...], bool] | None = None
) -> list[CommandReferenceError]:
    """Return invalid command references from canonical skill files."""
    command_paths = command_tree() if commands is None else commands
    errors: list[CommandReferenceError] = []
    for path in sorted(set(skill_files)):
        text = path.read_text(encoding="utf-8")
        markers = _active_marker_lines(text)
        marker_index = 0
        for line, code in _executable_chunks(text):
            tokens = _command_tokens(code)
            if tokens is None:
                continue
            if marker_index < len(markers) and markers[marker_index] < line:
                marker_index += 1
                continue
            if unknown := _unknown_path(tokens, command_paths):
                errors.append(CommandReferenceError(path, line, f"unknown fieldkit command path: {' '.join(unknown)}"))
        errors.extend(
            CommandReferenceError(path, line, f"unused exception marker: {IGNORE_NEXT}")
            for line in markers[marker_index:]
        )
    return errors


def check_command_refs(context: model.IntegrityContext, registry: model.Registry) -> list[model.Violation]:
    """Map command-reference diagnostics into the shared integrity report."""
    entry_files = [*registry.project_skills.values(), *registry.fieldkit_skills.values()]
    skill_files = [markdown for entry in entry_files for markdown in entry.parent.rglob("*.md")]
    return [
        model.Violation(
            code="R007",
            file=str(error.file.relative_to(context.repo_root)),
            line=error.line,
            message=error.message,
        )
        for error in validate_skill_command_refs(skill_files)
    ]
