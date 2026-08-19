"""`a2y agent ...` -- manage agents in a fleet workspace.

`a2y agent add` is deliberately non-interactive and single-shot: the intended
interactive layer is a fleet agent (the assistant) interviewing the operator in
chat and then calling this command with the answers. The tool stays
deterministic; the conversation stays where conversations belong. See
docs/hiring.md for the interview the assistant runs.

Structured input: `--json` accepts a full agent.yaml body (file path or `-` for
stdin), for callers that would rather build the manifest than spell flags.
Flags win over `--json` keys.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .manifest import NAME_RE, ManifestError, load_fleet

SOUL_SKELETON = """\
# {name}

You are {name}. {description}

Keep this file SHORT: identity, scope, non-goals, and the few rules specific to
you. Fleet-wide conduct arrives from SOUL-shared.md; tool instructions from
toolkit USAGE sections; operational knowledge lives in .hint files. Every line
here is paid on every turn.

## Scope

- What you own and answer for. Be precise: your colleagues route by this.

## Out of scope

- What you do NOT touch, even when convenient.
"""


def cmd_agent_add(ns: argparse.Namespace) -> int:
    root = Path.cwd()
    if not (root / "fleet.yaml").is_file():
        print("a2y: no fleet.yaml here -- run from a fleet workspace", file=sys.stderr)
        return 2

    name = ns.name
    if not NAME_RE.match(name):
        print(f"a2y: agent name {name!r} must be lowercase [a-z0-9-]", file=sys.stderr)
        return 2
    agent_dir = root / "agents" / name
    if agent_dir.exists():
        print(f"a2y: agents/{name}/ already exists", file=sys.stderr)
        return 2

    manifest: dict = {}
    if ns.json:
        raw = sys.stdin.read() if ns.json == "-" else Path(ns.json).read_text()
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"a2y: --json is not valid JSON ({exc})", file=sys.stderr)
            return 2
        if not isinstance(manifest, dict):
            print("a2y: --json must be an object (the agent.yaml body)", file=sys.stderr)
            return 2

    manifest["name"] = name
    if ns.description:
        manifest["description"] = ns.description
    if not str(manifest.get("description") or "").strip():
        print("a2y: --description is required (it is the agent's card and roster entry)",
              file=sys.stderr)
        return 2

    if ns.chain:
        brains = manifest.setdefault("brains", {})
        brains["chain"] = [s.strip() for s in ns.chain.split(",") if s.strip()]
    if ns.ssh or ns.github_token:
        access = manifest.setdefault("access", {})
        if ns.ssh:
            access["ssh"] = True
        if ns.github_token:
            access["github_token"] = True
    if ns.projects:
        memory = manifest.setdefault("memory", {})
        memory["projects"] = [s.strip() for s in ns.projects.split(",") if s.strip()]
    if ns.toolkits:
        manifest["toolkits"] = [s.strip() for s in ns.toolkits.split(",") if s.strip()]
    if ns.reply_mode or ns.no_require_mention:
        platform = manifest.setdefault("platform", {})
        if ns.reply_mode:
            platform["reply_mode"] = ns.reply_mode
        if ns.no_require_mention:
            platform["require_mention"] = False
    if ns.ports_base:
        manifest["ports"] = {"base": int(ns.ports_base)}

    soul = SOUL_SKELETON.format(name=name, description=manifest["description"])
    if ns.soul_file:
        soul = sys.stdin.read() if ns.soul_file == "-" else Path(ns.soul_file).read_text()

    # Write, then validate by loading the whole fleet; roll back on failure so a
    # bad call leaves no half-created agent behind.
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=100))
    (agent_dir / "SOUL.md").write_text(soul)
    try:
        fleet = load_fleet(root)
    except ManifestError as exc:
        (agent_dir / "agent.yaml").unlink()
        (agent_dir / "SOUL.md").unlink()
        agent_dir.rmdir()
        print(f"a2y: rolled back agents/{name}/ -- {exc}", file=sys.stderr)
        return 2

    print(f"  created agents/{name}/agent.yaml")
    print(f"  created agents/{name}/SOUL.md" + ("" if ns.soul_file else "  (skeleton -- write the real soul)"))

    if not ns.no_render:
        from .render import render_fleet
        for rel in render_fleet(fleet):
            print(f"  wrote deploy/{rel}")

    agent = next(a for a in fleet.agents if a.name == name)
    p = agent.env_prefix
    print(f"\n=== next steps for {name} ===")
    step = 1
    print(f"  {step}. add to deploy/.env: {p}_LITELLM_MASTER_KEY=<random>"); step += 1
    if fleet.platform_kind == "mattermost":
        print(f"  {step}. `a2y provision {name}` -- create the Mattermost account, then set")
        print(f"     {p}_MATTERMOST_TOKEN, {p}_MATTERMOST_HOME_CHANNEL (and empty {p}_MATTERMOST_CHANNELS)")
        step += 1
        print(f"  {step}. append the new USER ID to A2Y_MATTERMOST_ALLOWED_USERS and RECREATE the")
        print(f"     other agents (`a2y up` recreates on env change) -- without this, messages")
        print(f"     from {name} are dropped silently"); step += 1
    if agent.access.get("github_token"):
        print(f"  {step}. set {p}_GH_TOKEN (fine-grained PAT scoped to its repositories)"); step += 1
    print(f"  {step}. `a2y up {name}`"); step += 1
    print(f"  {step}. `a2y auth {name}` -- sign the brains in (device-code flows)"); step += 1
    if agent.access.get("ssh"):
        print(f"  {step}. register the git deploy key the entrypoint prints on first start"); step += 1
    print(f"  {step}. verify with a real mention in the channel; `a2y doctor` last")
    return 0


def cmd_agent_list(_: argparse.Namespace) -> int:
    fleet = load_fleet(Path.cwd())
    for a in fleet.agents:
        marks = []
        if a.access.get("ssh"):
            marks.append("ssh")
        if a.access.get("github_token"):
            marks.append("gh-token")
        if a.project_banks():
            marks.append("projects:" + ",".join(a.project_banks()))
        suffix = f"  [{'; '.join(marks)}]" if marks else ""
        print(f"  {a.name}  ({' -> '.join(a.chain)}){suffix}")
        print(f"      {a.description}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("agent", help="manage agents (add, list)")
    ssub = p.add_subparsers(dest="agent_cmd", required=True)

    pa = ssub.add_parser(
        "add", help="add an agent non-interactively (built for being called BY an agent)")
    pa.add_argument("name")
    pa.add_argument("--description", help="what the agent owns and answers for (required)")
    pa.add_argument("--chain", help="brain chain, e.g. claude,codex (default: fleet defaults)")
    pa.add_argument("--ssh", action="store_true", help="mount an ssh key volume")
    pa.add_argument("--github-token", action="store_true", help="expects AGENT_<N>_GH_TOKEN")
    pa.add_argument("--projects", help="shared memory banks, comma-separated")
    pa.add_argument("--toolkits", help="toolkits from ./toolkits/, comma-separated (derived image)")
    pa.add_argument("--reply-mode", choices=["thread", "channel"])
    pa.add_argument("--no-require-mention", action="store_true")
    pa.add_argument("--ports-base", help="port block base (shared-namespace fleets only)")
    pa.add_argument("--soul-file", help="SOUL.md content from a file, or - for stdin")
    pa.add_argument("--json", help="full agent.yaml body as JSON (file or -); flags win")
    pa.add_argument("--no-render", action="store_true")
    pa.set_defaults(fn=cmd_agent_add)

    pl = ssub.add_parser("list", help="list agents with chain and access")
    pl.set_defaults(fn=cmd_agent_list)
