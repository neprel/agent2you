"""Load and validate the two manifests a fleet is described by.

A fleet workspace is a directory (usually its own git repository) holding:

    fleet.yaml            deployment-level facts: platform, memory, network,
                          observability, image, defaults merged into every agent
    agents/<name>/
        agent.yaml        who the agent is: identity, brains, access, extra MCP
        SOUL.md           its persona and standing instructions
    SOUL-shared.md        optional; appended to every agent's SOUL.md at render
    banks/*.json          optional; shared memory-bank profiles (project banks)
    image/                the agent image build context (vendored by `a2y init`)

Everything here is parsed into plain dicts with defaults resolved, so render.py
never has to guess. Validation is deliberately loud: a fleet that renders is a
fleet that starts, and the cheapest place to fail is here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Internal ports inside one agent container. In the default bridge network every
# container has its own loopback, so every agent uses the same numbers and there
# is nothing to allocate. In shared-namespace mode (network.mode: container:<x>)
# all agents share one loopback and each needs its own block -- see ports_for().
PORTS_DEFAULT = {"acp2api": 10021, "litellm": 10022, "a2a": 10023, "metrics": 10029}

KNOWN_EXECUTOR_KINDS = {"claude", "codex", "opencode", "cline", "custom", "openai"}
KNOWN_PLATFORMS = {"mattermost", "telegram", "slack", "discord", "none"}
KNOWN_MEMORY = {"hindsight", "none"}


class ManifestError(Exception):
    """A manifest problem worth stopping for, with the file and the fix."""


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: expected a mapping at the top level")
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Agent:
    name: str
    dir: Path
    raw: dict
    fleet: "Fleet"

    @property
    def env_prefix(self) -> str:
        return "AGENT_" + self.name.upper().replace("-", "_")

    @property
    def container(self) -> str:
        return f"agent-{self.name}"

    @property
    def description(self) -> str:
        return " ".join(str(self.raw.get("description") or "").split())

    @property
    def brains(self) -> dict:
        return self.raw.get("brains") or {}

    @property
    def chain(self) -> list[str]:
        return list(self.brains.get("chain") or [])

    @property
    def executors(self) -> dict[str, dict]:
        return dict(self.brains.get("executors") or {})

    @property
    def access(self) -> dict:
        return self.raw.get("access") or {}

    @property
    def memory(self) -> dict:
        return self.raw.get("memory") or {}

    @property
    def platform(self) -> dict:
        return self.raw.get("platform") or {}

    @property
    def extra_mcp(self) -> list[dict]:
        return list(self.raw.get("mcp") or [])

    def ports(self) -> dict[str, int]:
        return self.fleet.ports_for(self)

    def project_banks(self) -> list[str]:
        return [str(b) for b in (self.memory.get("projects") or [])]

    def validate(self) -> None:
        where = self.dir / "agent.yaml"
        if not NAME_RE.match(self.name):
            raise ManifestError(f"{where}: name {self.name!r} must be lowercase [a-z0-9-]")
        if not self.description:
            raise ManifestError(
                f"{where}: description is required -- it is the agent's card, "
                "its roster entry, and how colleagues decide when to call it"
            )
        if not (self.dir / "SOUL.md").is_file():
            raise ManifestError(f"{self.dir}: SOUL.md is required (the agent's persona)")
        if not self.chain:
            raise ManifestError(f"{where}: brains.chain must name at least one executor")
        for ex in self.chain:
            if ex not in self.executors:
                raise ManifestError(f"{where}: brains.chain names {ex!r} which is not in brains.executors")
        for name, spec in self.executors.items():
            kind = spec.get("kind") or name
            if kind not in KNOWN_EXECUTOR_KINDS:
                raise ManifestError(
                    f"{where}: executor {name!r} has unknown kind {kind!r} "
                    f"(known: {', '.join(sorted(KNOWN_EXECUTOR_KINDS))})"
                )
            if kind == "custom" and not spec.get("command"):
                raise ManifestError(f"{where}: executor {name!r} is kind custom and needs `command`")
            if kind == "openai" and not spec.get("base_url"):
                raise ManifestError(f"{where}: executor {name!r} is kind openai and needs `base_url`")


@dataclass
class Fleet:
    root: Path
    raw: dict
    agents: list[Agent] = field(default_factory=list)

    # ---- deployment-level accessors -------------------------------------

    @property
    def name(self) -> str:
        return str(self.raw.get("name") or "fleet")

    @property
    def network(self) -> dict:
        return self.raw.get("network") or {}

    @property
    def network_mode(self) -> str:
        return str(self.network.get("mode") or "bridge")

    @property
    def shared_namespace(self) -> bool:
        return self.network_mode.startswith("container:")

    @property
    def platform(self) -> dict:
        return self.raw.get("platform") or {}

    @property
    def platform_kind(self) -> str:
        return str(self.platform.get("kind") or "none")

    @property
    def memory(self) -> dict:
        return self.raw.get("memory") or {}

    @property
    def memory_kind(self) -> str:
        return str(self.memory.get("kind") or "none")

    @property
    def observability(self) -> dict:
        return self.raw.get("observability") or {}

    @property
    def image(self) -> dict:
        return self.raw.get("image") or {}

    @property
    def image_tag(self) -> str:
        return str(self.image.get("tag") or f"agent2you/{self.name}:latest")

    @property
    def defaults(self) -> dict:
        return self.raw.get("defaults") or {}

    # ---- ports ----------------------------------------------------------

    def ports_for(self, agent: Agent) -> dict[str, int]:
        """The four internal ports of one agent.

        bridge mode: every agent gets the same fixed numbers -- each container
        has its own loopback, so there is nothing to coordinate.

        shared-namespace mode: one loopback for the whole fleet, so each agent
        declares `ports.base` (a multiple of 10) and gets base+1/+2/+3/+9 --
        the block-of-ten convention, kept so every number is accounted for.
        """
        if not self.shared_namespace:
            return dict(PORTS_DEFAULT)
        base = (agent.raw.get("ports") or {}).get("base")
        if not base:
            raise ManifestError(
                f"{agent.dir / 'agent.yaml'}: network.mode is {self.network_mode!r} "
                "(one shared loopback), so this agent needs `ports: {base: 100X0}`"
            )
        base = int(base)
        return {"acp2api": base + 1, "litellm": base + 2, "a2a": base + 3, "metrics": base + 9}

    # ---- validation ------------------------------------------------------

    def validate(self) -> None:
        where = self.root / "fleet.yaml"
        if not NAME_RE.match(self.name):
            raise ManifestError(f"{where}: name {self.name!r} must be lowercase [a-z0-9-]")
        if self.platform_kind not in KNOWN_PLATFORMS:
            raise ManifestError(
                f"{where}: platform.kind {self.platform_kind!r} unknown "
                f"(known: {', '.join(sorted(KNOWN_PLATFORMS))})"
            )
        if self.memory_kind not in KNOWN_MEMORY:
            raise ManifestError(f"{where}: memory.kind {self.memory_kind!r} unknown")
        if self.memory_kind == "hindsight" and not self.memory.get("url"):
            raise ManifestError(f"{where}: memory.kind hindsight needs memory.url")
        if not self.agents:
            raise ManifestError(f"{self.root}: no agents/*/agent.yaml found")
        seen: dict[str, str] = {}
        blocks: dict[int, str] = {}
        for a in self.agents:
            if a.name in seen:
                raise ManifestError(f"duplicate agent name {a.name!r}")
            seen[a.name] = a.name
            a.validate()
            if self.shared_namespace:
                self.ports_for(a)  # raises the specific error when base is missing
                base = int((a.raw.get("ports") or {}).get("base") or 0)
                if base in blocks:
                    raise ManifestError(
                        f"agents {blocks[base]!r} and {a.name!r} both claim port base {base} "
                        "on one shared loopback"
                    )
                blocks[base] = a.name


def load_fleet(root: Path) -> Fleet:
    root = root.resolve()
    fleet_file = root / "fleet.yaml"
    if not fleet_file.is_file():
        raise ManifestError(
            f"{root}: no fleet.yaml here. Run from a fleet workspace, or create one with `a2y init <dir>`."
        )
    fleet = Fleet(root=root, raw=_load_yaml(fleet_file))

    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for f in sorted(agents_dir.glob("*/agent.yaml")):
            raw = _deep_merge(fleet.defaults, _load_yaml(f))
            name = str(raw.get("name") or f.parent.name)
            fleet.agents.append(Agent(name=name, dir=f.parent, raw=raw, fleet=fleet))

    fleet.validate()
    return fleet
