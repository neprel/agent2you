"""`a2y init` -- create a new fleet workspace.

The workspace is meant to be its own git repository: manifests, souls and the
rendered deploy tree are committed; `.env` and `volumes/` never are.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

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
  # Where the agents live. `mattermost` is fully wired (plugins, boards and
  # playbooks tools, provisioning docs). Other Hermes platforms pass through:
  # set kind + platform.env with the adapter's variables.
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

## What you do

- Answer the operator directly and honestly; when a question belongs to a
  colleague's scope, delegate with an @mention in the channel.
- Learn how the operator works and apply it.

## What you never do

- Never report success for a tool call that failed. A confident wrong answer
  about what you just did costs more than the failure itself.
- "Posted" is not "delivered": a mention reaches only members of the channel it
  is written in.
"""

SOUL_SHARED = """\
## Working in this fleet (shared by every agent)

- Reaching a colleague is an @mention in the thread you are already in. Answering
  a request carries the requester's @; thanking does not -- that is what keeps
  two agents from mentioning each other forever.
- Never idle-wait inside a turn (no blocking event waits): set a cron to come
  back later instead.
- If a tool errors, say so, verbatim. Never invent a cause.
"""

GITIGNORE = """\
# Secrets live only here.
.env

# Runtime state: logins, workspaces, Hermes sessions. Containers own the
# permissions below volumes/, so git must not descend into it.
volumes/**
"""

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
    put(".gitignore", GITIGNORE)
    put("README.md", README.format(name=name))

    # Vendor the image build context so the fleet repo is self-contained and the
    # dockerfile is the user's to edit (pins, extra tools, derived images).
    image_dst = dest / "image"
    if not image_dst.exists():
        src = resources.files("a2y") / "image"
        shutil.copytree(str(src), image_dst)
        created.append("image/")
    return created
