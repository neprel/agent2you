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

import yaml
from croniter import croniter

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CRON_RE = re.compile(r"^[\d*/?,\-]+(?:\s+[\d*/?,\-]+){4}$")

# Internal ports inside one agent container. In the default bridge network every
# container has its own loopback, so every agent uses the same numbers and there
# is nothing to allocate. In shared-namespace mode (network.mode: container:<x>)
# all agents share one loopback and each needs its own block -- see ports_for().
PORTS_DEFAULT = {"acp2api": 10021, "litellm": 10022, "a2a": 10023, "metrics": 10029}

KNOWN_EXECUTOR_KINDS = {"claude", "codex", "opencode", "cline", "custom", "openai", "api"}
KNOWN_PLATFORMS = {"mattermost", "telegram", "slack", "discord", "teams", "none"}
KNOWN_MEMORY = {"hindsight", "local", "none"}


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
    def channels(self) -> dict:
        return self.raw.get("channels") or {}

    @property
    def duties(self) -> list[dict]:
        return list(self.raw.get("duties") or [])

    @property
    def extra_mcp(self) -> list[dict]:
        merged: dict[str, dict] = {}
        for toolkit in [*self.fleet.image_toolkits, *self.toolkits]:
            for server in self.fleet.load_toolkit(toolkit).get("mcp") or []:
                merged[str(server.get("name") or "")] = dict(server)
        for server in self.raw.get("mcp") or []:
            merged[str(server.get("name") or "")] = dict(server)
        return list(merged.values())

    @property
    def toolkits(self) -> list[str]:
        return [str(t) for t in (self.raw.get("toolkits") or [])]

    # ---- brain topology --------------------------------------------------
    # The stack is assembled per agent from the chain, not assumed:
    #   * acp2api runs only when an ACP executor (a coding CLI) exists;
    #   * litellm runs only when there is a failover decision to make --
    #     `brains.litellm: auto` (default) means on for a chain of 2+, off for
    #     a single brain; `on`/`off` override either way;
    #   * a single `kind: openai` brain is Hermes pointed straight at the
    #     endpoint: no acp2api, no litellm, two processes fewer.

    def _kind(self, name: str) -> str:
        return self.executors[name].get("kind") or name

    def acp_chain(self) -> list[str]:
        return [n for n in self.chain if self._kind(n) not in {"openai", "api"}]

    def openai_chain(self) -> list[str]:
        return [n for n in self.chain if self._kind(n) in {"openai", "api"}]

    @property
    def acp2api_enabled(self) -> bool:
        return bool(self.acp_chain())

    @property
    def litellm_mode(self) -> str:
        """auto | on | off. YAML 1.1 reads bare `on`/`off` as booleans, so both
        spellings are accepted and normalized."""
        raw = self.brains.get("litellm", "auto")
        if raw is True:
            return "on"
        if raw is False:
            return "off"
        return str(raw)

    @property
    def litellm_enabled(self) -> bool:
        mode = self.litellm_mode
        if mode == "on":
            return True
        if mode == "off":
            return False
        return len(self.chain) > 1 or any(self._kind(name) == "api" for name in self.chain)

    @property
    def hermes_env(self) -> list[str]:
        """Extra container variables to cross into Hermes' own .env."""
        return [str(v) for v in (self.raw.get("hermes_env") or [])]

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
            if kind == "openai":
                if not spec.get("base_url") or not spec.get("model"):
                    raise ManifestError(
                        f"{where}: executor {name!r} is kind openai and needs `base_url` and `model`"
                    )
                if spec.get("api_key_env") == "OPENAI_API_KEY":
                    raise ManifestError(
                        f"{where}: executor {name!r}: api_key_env must not be OPENAI_API_KEY -- "
                        "inside the container that variable carries the litellm master key so "
                        "no real provider key can be billed by accident; pick a distinct name"
                    )
            if kind == "api":
                if not spec.get("model") or not spec.get("api_key_env"):
                    raise ManifestError(
                        f"{where}: executor {name!r} is kind api and needs `model` and `api_key_env`"
                    )
                if spec.get("api_key_env") == "OPENAI_API_KEY":
                    raise ManifestError(f"{where}: executor {name!r}: api_key_env must not be OPENAI_API_KEY")
        for server in self.extra_mcp:
            if not server.get("name"):
                raise ManifestError(f"{where}: every mcp server needs a name")
            if bool(server.get("command")) == bool(server.get("url")):
                raise ManifestError(
                    f"{where}: mcp server {server.get('name')!r} needs exactly one of command or url"
                )
        host = self.raw.get("host_access") or {}
        if "privileged" in host or self.raw.get("privileged"):
            raise ManifestError(f"{where}: privileged containers are prohibited; use explicit gpus/devices")
        if self.fleet.platform_kind != "mattermost" and any(
            key in self.platform for key in ("reply_mode", "require_mention")
        ):
            raise ManifestError(f"{where}: reply_mode/require_mention are only implemented for mattermost")
        if self.raw.get("role") == "apprentice" and not self.raw.get("owner"):
            raise ManifestError(f"{where}: role apprentice requires owner (an immutable platform user id)")
        for briefing in self.memory.get("briefings") or []:
            if not isinstance(briefing, dict) or not briefing.get("date") or not briefing.get("text"):
                raise ManifestError(f"{where}: each memory.briefings entry needs date and text")
        email = self.channels.get("email")
        if email:
            required = {"address", "password_env", "imap_host", "smtp_host", "allowed_users"}
            missing = sorted(required - set(email))
            if missing:
                raise ManifestError(f"{where}: channels.email is missing {', '.join(missing)}")
            if not str(email.get("password_env", "")).isupper():
                raise ManifestError(
                    f"{where}: channels.email.password_env must name an uppercase env variable"
                )
            if not str(email.get("allowed_users") or "").strip():
                raise ManifestError(
                    f"{where}: channels.email.allowed_users must be non-empty (email fails closed)"
                )
        duty_names: set[str] = set()
        for duty in self.duties:
            if not isinstance(duty, dict):
                raise ManifestError(f"{where}: every duty must be a mapping")
            name = str(duty.get("name") or "")
            if not NAME_RE.match(name):
                raise ManifestError(f"{where}: duty name {name!r} must be lowercase [a-z0-9-]")
            if name in duty_names:
                raise ManifestError(f"{where}: duplicate duty name {name!r}")
            duty_names.add(name)
            schedule = str(duty.get("schedule") or "")
            if not CRON_RE.fullmatch(schedule):
                raise ManifestError(
                    f"{where}: duty {name!r} schedule must be a numeric five-field cron expression (UTC)"
                )
            if not croniter.is_valid(schedule):
                raise ManifestError(f"{where}: duty {name!r} schedule is not a valid cron expression")
            if not str(duty.get("channel") or "").strip():
                raise ManifestError(f"{where}: duty {name!r} needs a non-empty channel")
            if not str(duty.get("instruction") or "").strip():
                raise ManifestError(f"{where}: duty {name!r} needs an instruction")
        mode = self.litellm_mode
        if mode not in ("auto", "on", "off"):
            raise ManifestError(f"{where}: brains.litellm must be auto, on or off (got {mode!r})")
        if mode == "off" and len(self.chain) > 1:
            raise ManifestError(
                f"{where}: brains.litellm is off but the chain has {len(self.chain)} executors -- "
                "nothing could fail over between them; drop to one executor or let litellm run"
            )


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
        return self.network_mode.startswith("container:") or bool(self.network.get("vpn_service"))

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

    @property
    def image_toolkits(self) -> list[str]:
        """Toolkits baked into the fleet image (every agent gets them)."""
        return [str(t) for t in (self.image.get("toolkits") or [])]

    @property
    def git_hosts(self) -> list[str]:
        """ssh hosts the entrypoint pre-seeds into known_hosts (github.com is
        the default; a gitea or gitlab host goes here)."""
        return [str(h) for h in ((self.raw.get("git") or {}).get("hosts") or [])]

    # ---- toolkits --------------------------------------------------------

    def toolkit_dir(self, name: str) -> Path:
        return self.root / "toolkits" / name

    def load_toolkit(self, name: str) -> dict:
        """A toolkit is a directory bundling an INSTALL recipe with USAGE
        instructions: `toolkits/<name>/toolkit.yaml` (apt/npm/uv_tools/env/
        dockerfile keys) plus an optional USAGE.md appended to the SOUL.md of
        every agent that carries it. Install and instructions travel together
        because a tool nobody was told how to use is a tool that gets misused."""
        d = self.toolkit_dir(name)
        f = d / "toolkit.yaml"
        if not f.is_file():
            raise ManifestError(
                f"toolkit {name!r} is referenced but toolkits/{name}/toolkit.yaml does not exist"
            )
        spec = _load_yaml(f)
        allowed = {"description", "apt", "npm", "uv_tools", "env", "dockerfile", "mcp"}
        unknown = set(spec) - allowed
        if unknown:
            raise ManifestError(f"{f}: unknown toolkit key(s): {', '.join(sorted(unknown))}")
        for server in spec.get("mcp") or []:
            if not isinstance(server, dict) or not server.get("name"):
                raise ManifestError(f"{f}: every mcp entry must be a mapping with a name")
            if bool(server.get("command")) == bool(server.get("url")):
                raise ManifestError(
                    f"{f}: mcp server {server.get('name')!r} needs exactly one of command or url"
                )
        usage = d / "USAGE.md"
        spec["_usage"] = usage.read_text() if usage.is_file() else ""
        spec["_name"] = name
        return spec

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
        if self.platform_kind == "teams":
            gateway = str(self.platform.get("gateway_agent") or self.agents[0].name)
            if gateway not in {agent.name for agent in self.agents}:
                raise ManifestError(f"{where}: platform.gateway_agent {gateway!r} is not an agent")
            endpoint = str(self.platform.get("public_endpoint") or "")
            if not endpoint.startswith("https://"):
                raise ManifestError(
                    f"{where}: Teams needs platform.public_endpoint as public HTTPS /api/messages URL"
                )
        roster_mode = str((self.raw.get("roster") or {}).get("mode") or "full")
        if roster_mode not in {"full", "brief", "off"}:
            raise ManifestError(f"{where}: roster.mode must be full, brief or off")
        if not self.agents:
            raise ManifestError(f"{self.root}: no agents/*/agent.yaml found")
        for t in self.image_toolkits:
            self.load_toolkit(t)  # raises with the toolkit's name when missing
        seen: dict[str, str] = {}
        blocks: dict[int, str] = {}
        for a in self.agents:
            for t in a.toolkits:
                self.load_toolkit(t)
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
