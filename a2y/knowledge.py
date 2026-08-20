"""Operator-side additions and tombstone retractions for local HINT memory."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .manifest import Fleet, ManifestError


def _repo(fleet: Fleet, agent: str) -> Path:
    if fleet.memory_kind != "local":
        raise ManifestError(
            "knowledge commands currently target memory.kind local; Hindsight curation uses its bank API"
        )
    if agent not in {a.name for a in fleet.agents}:
        raise ManifestError(f"unknown agent {agent!r}")
    return fleet.root / "volumes" / f"agent-{agent}" / "memory"


def remember(fleet: Fleet, agent: str, topic: str, text: str) -> Path:
    root = _repo(fleet, agent)
    slug = re.sub(r"[^a-z0-9]+", "-", topic.casefold()).strip("-") or "briefing"
    path = root / "wiki" / slug / "_.hint"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(f"\n# evidence operator-briefing {date.today().isoformat()}\n{text.strip()}\n")
    return path


def retract(fleet: Fleet, agent: str, topic: str, superseded_by: str = "") -> Path:
    root = _repo(fleet, agent)
    slug = re.sub(r"[^a-z0-9]+", "-", topic.casefold()).strip("-") or "knowledge"
    path = root / "wiki" / slug / "_.hint"
    path.parent.mkdir(parents=True, exist_ok=True)
    statement = f"{topic} is outdated since {date.today().isoformat()}."
    if superseded_by:
        statement += f" Superseded by {superseded_by}."
    path.write_text(f"# supersedes {topic}\n{statement}\n")
    return path


def cmd_knowledge(ns, fleet: Fleet) -> int:
    path = (
        remember(fleet, ns.agent, ns.topic, ns.text)
        if ns.knowledge_cmd == "remember"
        else retract(fleet, ns.agent, ns.topic, ns.superseded_by or "")
    )
    print(f"  wrote {path}; commit the memory repository so the change remains reviewable and reversible")
    return 0
