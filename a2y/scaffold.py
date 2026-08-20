"""`a2y init` -- create a new fleet workspace.

The workspace is meant to be its own git repository: manifests, souls and the
rendered deploy tree are committed; `.env` and `volumes/` never are.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from importlib import resources
from pathlib import Path

from . import __version__

FLEET_YAML = """\
# The one file that describes this deployment. See docs/architecture.md in the
# agent2you pack for what every key means.
name: {name}

image:
  # Built with `a2y build` from ./image (vendored by init -- edit it freely;
  # pin bumps belong there). Extra tools go in as TOOLKITS: a directory
  # `toolkits/<name>/` with toolkit.yaml (apt/npm/uv_tools/env/dockerfile) and
  # a USAGE.md that lands in the SOUL.md of every agent carrying it. List them
  # here for the whole fleet, or per agent as `toolkits: [...]`.
  tag: agent2you/{name}:0.1.0
  # toolkits: [go]

network:
  # bridge: the fleet gets its own docker network; every agent keeps its own
  # loopback and the same internal ports. The advanced alternative is
  # `mode: container:<vpn-container>` (+ `iface: <wg-iface>`): all agents share
  # one network namespace -- then every agent needs a distinct `ports.base`.
  mode: bridge

platform:
  # Full-office platforms: mattermost, telegram, slack, discord. Teams is an
  # enterprise front door; email is an auxiliary per-agent channels.email adapter.
  kind: mattermost
  team: {name}

memory:
  # Long-term memory is an external Hindsight server, or `kind: none`.
  # Banks are the isolation boundary: one private bank per agent, shared
  # project banks under ./banks/*.json.
  kind: none
  # kind: hindsight
  # url: http://hindsight:8888

observability:
  # phoenix_url: http://phoenix:6006   # traces: one project per agent
  # prometheus: true                   # acp2api token metrics listener

# Optional endpoint exposed to supervisor digest duties.
# metrics:
#   prometheus_url: http://prometheus:9090
# Subscription windows are declarations because providers change them:
# accounts:
#   main-openai: {{window: 7d, reset_anchor: Tuesday, alert_thresholds: [0.8, 0.95]}}

# Merged into every agent.yaml (agent values win). Keep the common brain chain
# here so an agent file stays a page about the agent, not about plumbing.
defaults:
  brains:
    chain: [claude, codex]
    executors:
      claude: {{kind: claude, model: opus, reasoning: high, account: main-anthropic}}
      codex: {{kind: codex, reasoning: high, account: main-openai}}
"""

AGENT_YAML = """\
name: {name}
description: >-
  Personal assistant and the fleet's front door. Knows the operator, remembers
  preferences, routes work to the right colleague, and helps plan the hiring
  (creation) of new agents.

platform:
  reply_mode: thread
  require_mention: true

# access:
#   ssh: true            # mount a key volume; the entrypoint generates a git key
#   github_token: true   # expects AGENT_<NAME>_GH_TOKEN in .env

# memory:
#   projects: [myproject]          # shared banks this agent may read/write
#   retain_context: "..."

# mcp:                             # extra MCP servers, appended verbatim
#   - name: metrics
#     url: http://monitor-mcp:8000/mcp
"""

SOUL_MD = """\
# {name}

You are {name}, the operator's assistant and the first voice of this fleet.

Keep this file SHORT: identity, scope, non-goals, and the few rules specific to
you. Fleet-wide conduct arrives from SOUL-shared.md, tool instructions from
toolkit USAGE sections, and operational knowledge lives in .hint files -- not
here. Every line of this file is paid on every turn.

## What you do

- Answer the operator directly and honestly; when a question belongs to a
  colleague's scope, delegate with an @mention in the channel.
- Learn how the operator works and apply it.

## Out of scope

- Name the work you hand off even when it would be convenient to keep.
"""

SOUL_SHARED = """\
## Working in this fleet (shared by every agent)

Communication:
- Lead with the answer or the completed action. If one sentence is enough, use
  one. Skip filler, preamble, and restating the request.
- Stop and ask only when a decision is genuinely the operator's, a blocker
  changes the plan, or a milestone is worth reporting. Otherwise keep working.

Honesty:
- Self-reporting is not evidence. Before saying something works, verify it with
  real output and show it. Never claim success for a tool call that failed;
  quote the error verbatim and never invent a cause.
- Do not invent missing data, credentials, file locations, or task status. Name
  what is missing and where you looked.
- Before claiming something does not exist or is unavailable, try to USE it: a
  tool you cannot enumerate may still answer when invoked.

Colleagues:
- Reaching a colleague is an @mention in the thread you are already in, and a
  mention reaches only members of that channel. Answering a request carries the
  requester's @; thanking does not -- that is what keeps two agents from
  mentioning each other forever.
- "Posted" is not "delivered". Asked whether a message arrived, you only know
  your own inbox; when accounts disagree, read the channel through the API.

Memory and continuity:
- Automatic recall injects only consolidated observations, and reconciliation
  is best-effort. Before answering about past decisions, probe the memory tools
  explicitly. Memory records what was said; for what is true, use the shell.
- A conversation that arrives summarized was compacted, not restarted. Continue
  from it; do not redo finished work.

Discipline:
- Before building a new mechanism, check whether a cron, skill, or script
  already does it.
- Never idle-wait inside a turn (no blocking event waits); set a cron to come
  back later instead.
"""

GITIGNORE = """\
# a2y — do not remove: secrets and runtime state
deploy/.env
backup/

# Runtime state: logins, workspaces, Hermes sessions. Containers own the
# permissions below volumes/, so git must not descend into it.
volumes/
"""

REQUIRED_IGNORES = ("deploy/.env", "volumes/", "backup/")


def ensure_gitignore(dest: Path) -> bool:
    """Merge safety patterns without rewriting the repository's own rules."""
    path = dest / ".gitignore"
    old = path.read_text() if path.is_file() else ""
    present = {
        line.strip() for line in old.splitlines() if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [pattern for pattern in REQUIRED_IGNORES if pattern not in present]
    if not missing:
        return False
    prefix = "" if not old or old.endswith("\n") else "\n"
    block = prefix + ("\n" if old else "") + "# a2y — do not remove\n" + "\n".join(missing) + "\n"
    path.write_text(old + block)
    return True


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def write_upgrade_state(dest: Path) -> None:
    owned = [
        dest / "image",
        dest / ".github/workflows/a2y-fleet.yml",
        dest / ".gitea/workflows/a2y-fleet.yml",
    ]
    files = {}
    for item in owned:
        paths = sorted(item.rglob("*")) if item.is_dir() else [item]
        files.update({str(path.relative_to(dest)): _hash(path) for path in paths if path.is_file()})
    (dest / ".a2y-version").write_text(__version__ + "\n")
    (dest / ".a2y-upgrade.json").write_text(
        json.dumps({"version": __version__, "files": files}, indent=2, sort_keys=True) + "\n"
    )


README = """\
# {name} -- an agent2you fleet

- `fleet.yaml` + `agents/<name>/` describe the fleet; `a2y render` turns them
  into `deploy/` (committed, reviewable).
- `a2y build` builds the agent image from `image/`.
- `cp deploy/example.env deploy/.env`, fill it, then `a2y up`.
- `a2y auth <agent>` prints how to sign the brains in (one-time, interactive).
- `a2y doctor` checks the whole thing.
"""


def init_workspace(dest: Path, name: str, first_agent: str = "ana") -> list[str]:
    created: list[str] = []

    def put(rel: str, content: str) -> None:
        p = dest / rel
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        created.append(rel)

    put("fleet.yaml", FLEET_YAML.format(name=name))
    put(f"agents/{first_agent}/agent.yaml", AGENT_YAML.format(name=first_agent))
    put(f"agents/{first_agent}/SOUL.md", SOUL_MD.format(name=first_agent))
    put("SOUL-shared.md", SOUL_SHARED)
    if ensure_gitignore(dest):
        created.append(".gitignore")
    put("README.md", README.format(name=name))

    # Vendor the image build context so the fleet repo is self-contained and the
    # dockerfile is the user's to edit (pins, extra tools, derived images).
    image_dst = dest / "image"
    if not image_dst.exists():
        src = resources.files("a2y") / "image"
        shutil.copytree(str(src), image_dst)
        created.append("image/")
    toolkits_dst = dest / "toolkits"
    if not toolkits_dst.exists():
        bundled = resources.files("a2y") / "toolkits"
        shutil.copytree(str(bundled), toolkits_dst)
        created.append("toolkits/")
    platforms_dst = dest / "platforms"
    if not platforms_dst.exists():
        bundled = resources.files("a2y") / "platforms"
        shutil.copytree(str(bundled), platforms_dst)
        created.append("platforms/")
    workflows = resources.files("a2y") / "fleet_workflows"
    for forge in ("github", "gitea"):
        rel = f".{forge}/workflows/a2y-fleet.yml"
        target = dest / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((workflows / forge / "a2y-fleet.yml").read_bytes())
            created.append(rel)
    state_missing = [p for p in (".a2y-version", ".a2y-upgrade.json") if not (dest / p).exists()]
    write_upgrade_state(dest)
    created.extend(state_missing)
    return created
