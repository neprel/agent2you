"""Pure, testable apprentice gate and poisoning-aware procedure proposals."""

from __future__ import annotations

import re
from collections import Counter

NEVER_AUTO = {"money", "payment", "access", "permission", "hiring", "firing", "personnel"}


def gate(*, mentioned: bool, direct: bool, reply_to_self: bool, sender_is_bot: bool) -> str:
    if sender_is_bot:
        return "silent"
    if mentioned or direct or reply_to_self:
        return "answer"
    return "observe"


def neutralize(text: str) -> str:
    """Turn observed imperatives into reported, non-executable speech."""
    cleaned = " ".join(text.split())
    patterns = r"\b(ignore|forget|disregard|override|execute|run|send|delete|reveal)\b"
    if re.search(patterns, cleaned, re.I):
        return "An untrusted participant attempted to give an instruction; content withheld."
    return f"A participant said: {cleaned}"


def distill(episodes: list[dict], owner: str, minimum: int = 3) -> list[dict]:
    sources = [e for e in episodes if e.get("answerer") == owner and e.get("intent")]
    counts = Counter(str(e["intent"]) for e in sources)
    proposals = []
    for intent, count in sorted(counts.items()):
        if count < minimum:
            continue
        rows = [e for e in sources if e["intent"] == intent]
        proposals.append(
            {
                "intent": intent,
                "level": "shadow",
                "source_episode_ids": [e["id"] for e in rows],
                "steps": [neutralize(str(rows[-1].get("resolution") or ""))],
                "never_auto": any(word in intent.casefold() for word in NEVER_AUTO),
            }
        )
    return proposals


def set_level(procedure: dict, level: str, *, owner_action: bool) -> dict:
    if level not in {"shadow", "draft", "auto"}:
        raise ValueError("level must be shadow, draft or auto")
    if level == "auto" and not owner_action:
        raise PermissionError("only the owner may promote a procedure to auto")
    updated = dict(procedure)
    updated["level"] = level
    return updated
