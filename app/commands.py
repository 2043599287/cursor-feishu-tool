"""Parse Feishu slash commands."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .slash_commands import help_text


@dataclass
class ParsedCommand:
    name: str  # cursor | cursor_used | exit | bind | unbind | query_current_workspace | new_chat | query_chat | enter_chat | query_list | allow_mcp | forbid_mcp | prompt | help | none
    arg: str = ""


_MENTION_RE = re.compile(r"@_user_\d+\s*")
_CMD_RE = re.compile(
    r"^/(cursor_used|cursor|exit|bind|unbind|query_current_workspace|new_chat|query_chat|enter_chat|query_list|allow_mcp|forbid_mcp|help)(?:\s+(.*))?$",
    re.IGNORECASE | re.DOTALL,
)


def normalize_text(raw: str) -> str:
    """Strip Feishu @mentions and surrounding whitespace."""
    text = (raw or "").strip()
    text = _MENTION_RE.sub("", text).strip()
    return text


def parse_command(text: str) -> ParsedCommand:
    text = normalize_text(text)
    if not text:
        return ParsedCommand(name="none")

    m = _CMD_RE.match(text)
    if not m:
        return ParsedCommand(name="prompt", arg=text)

    name = m.group(1).lower()
    arg = (m.group(2) or "").strip()
    return ParsedCommand(name=name, arg=arg)


HELP_TEXT = help_text()
