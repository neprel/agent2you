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
import shutil
import subprocess
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
    folded = name.replace("-", "").casefold()
    for existing in (root / "agents").glob("*/agent.yaml"):
        other = existing.parent.name
        if other.replace("-", "").casefold() == folded:
            print(f"a2y: {name!r} is too easily confused with existing agent {other!r}", file=sys.stderr)
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
        print("a2y: --description is required (it is the agent's card and roster entry)", file=sys.stderr)
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
    if ns.role:
        manifest["role"] = ns.role
    if ns.owner:
        manifest["owner"] = ns.owner
    if ns.duty:
        duties = manifest.setdefault("duties", [])
        for raw in ns.duty:
            try:
                duty = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"a2y: --duty is not valid JSON ({exc})", file=sys.stderr)
                return 2
            if not isinstance(duty, dict):
                print("a2y: --duty must be a JSON object", file=sys.stderr)
                return 2
            duties.append(duty)

    soul = SOUL_SKELETON.format(name=name, description=manifest["description"])
    if ns.soul_file:
        soul = sys.stdin.read() if ns.soul_file == "-" else Path(ns.soul_file).read_text()

    # Write, then validate by loading the whole fleet; roll back on failure so a
    # bad call leaves no half-created agent behind.
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=100)
    )
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
    print(
        f"  created agents/{name}/SOUL.md" + ("" if ns.soul_file else "  (skeleton -- write the real soul)")
    )

    if not ns.no_render:
        from .render import render_fleet

        for rel in render_fleet(fleet):
            print(f"  wrote deploy/{rel}")

    agent = next(a for a in fleet.agents if a.name == name)
    p = agent.env_prefix
    print(f"\n=== next steps for {name} ===")
    step = 1
    if agent.litellm_enabled:
        print(f"  {step}. add to deploy/.env: {p}_LITELLM_MASTER_KEY=<random>")
        step += 1
    for ex in agent.openai_chain():
        key_env = agent.executors[ex].get("api_key_env")
        if key_env:
            print(f"  {step}. set {key_env} in deploy/.env (key for the {ex} endpoint)")
            step += 1
    if fleet.platform_kind == "mattermost":
        print(f"  {step}. `a2y provision {name}` -- create the Mattermost account, then set")
        print(f"     {p}_MATTERMOST_TOKEN, {p}_MATTERMOST_HOME_CHANNEL (and empty {p}_MATTERMOST_CHANNELS)")
        step += 1
        print(f"  {step}. append the new USER ID to A2Y_MATTERMOST_ALLOWED_USERS and RECREATE the")
        print("     other agents (`a2y up` recreates on env change) -- without this, messages")
        print(f"     from {name} are dropped silently")
        step += 1
    if agent.access.get("github_token"):
        print(f"  {step}. set {p}_GH_TOKEN (fine-grained PAT scoped to its repositories)")
        step += 1
    if agent.toolkits:
        print(f"  {step}. `a2y build` on the Docker host")
        step += 1
    if agent.model_specs:
        print(f"  {step}. `a2y models pull {name}` on the Docker host")
        step += 1
    print(f"  {step}. `a2y up {name}`")
    step += 1
    print(f"  {step}. `a2y auth {name}` -- sign the brains in (device-code flows)")
    step += 1
    if agent.access.get("ssh"):
        print(f"  {step}. register the git deploy key the entrypoint prints on first start")
        step += 1
    print(f"  {step}. verify with a real mention in the channel; `a2y doctor` last")
    return 0


def cmd_agent_remove(ns: argparse.Namespace) -> int:
    root = Path.cwd()
    fleet = load_fleet(root)
    matches = [a for a in fleet.agents if a.name == ns.name]
    if not matches:
        print(f"a2y: no agent named {ns.name!r}", file=sys.stderr)
        return 2
    agent = matches[0]
    from .cli import _compose

    try:
        running = (
            subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", agent.container],
                capture_output=True,
                text=True,
            ).stdout.strip()
            == "true"
        )
    except FileNotFoundError:
        running = False
    if running and not ns.yes:
        print(f"a2y: {agent.container} is running; stop it first or pass --yes", file=sys.stderr)
        return 2
    if ns.yes:
        _compose(fleet, "stop", agent.container)

    if ns.purge_volumes and not ns.yes:
        if not sys.stdin.isatty():
            print("a2y: --purge-volumes requires typed confirmation or --yes", file=sys.stderr)
            return 2
        answer = input(f"Type {agent.name} to permanently delete its credentials and workspace: ")
        if answer != agent.name:
            print("a2y: confirmation did not match; nothing removed", file=sys.stderr)
            return 2

    shutil.rmtree(agent.dir)
    remaining = load_fleet(root) if len(fleet.agents) > 1 else None
    if remaining:
        from .render import render_fleet

        render_fleet(remaining)
    else:
        deploy_agent = root / "deploy" / "agents" / agent.name
        shutil.rmtree(deploy_agent, ignore_errors=True)

    volume = root / "volumes" / agent.container
    if ns.purge_volumes:
        shutil.rmtree(volume, ignore_errors=True)
    residue = [
        f"{agent.env_prefix}_MATTERMOST_TOKEN",
        f"{agent.env_prefix}_MATTERMOST_HOME_CHANNEL",
        f"{agent.env_prefix}_MATTERMOST_CHANNELS",
        "A2Y_MATTERMOST_ALLOWED_USERS",
        "A2Y_ROOM_OWNERS",
    ]
    payload = {
        "removed": agent.name,
        "volume": "purged" if ns.purge_volumes else str(volume),
        "env_residue": residue,
    }
    if ns.json_output:
        print(json.dumps(payload))
    else:
        print(f"  removed agents/{agent.name}/ and pruned generated deploy files")
        if ns.purge_volumes:
            print(f"  permanently deleted {volume}")
        else:
            print(f"  state kept at {volume}; it contains live logins (backup before deleting)")
        print(
            "  remove/revoke these manually: "
            + ", ".join(residue)
            + "; platform account/token and deploy key"
        )
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
    p = sub.add_parser("agent", help="manage agents (add, remove, list)")
    ssub = p.add_subparsers(dest="agent_cmd", required=True)

    pa = ssub.add_parser("add", help="add an agent non-interactively (built for being called BY an agent)")
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
    pa.add_argument("--role", choices=["agent", "apprentice"])
    pa.add_argument("--owner", help="immutable platform user id (required for apprentice)")
    pa.add_argument("--duty", action="append", help="recurring duty as JSON; repeat for multiple duties")
    pa.add_argument("--soul-file", help="SOUL.md content from a file, or - for stdin")
    pa.add_argument("--json", help="full agent.yaml body as JSON (file or -); flags win")
    pa.add_argument("--no-render", action="store_true")
    pa.set_defaults(fn=cmd_agent_add)

    pl = ssub.add_parser("list", help="list agents with chain and access")
    pl.set_defaults(fn=cmd_agent_list)

    pr = ssub.add_parser("remove", help="remove an agent; state is kept by default")
    pr.add_argument("name")
    pr.add_argument("--purge-volumes", action="store_true")
    pr.add_argument("--yes", action="store_true", help="stop the service and confirm destructive actions")
    pr.add_argument("--json", dest="json_output", action="store_true")
    pr.set_defaults(fn=cmd_agent_remove)
