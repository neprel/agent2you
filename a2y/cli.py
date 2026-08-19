"""The a2y command line.

    a2y init <dir> [--name N]   create a fleet workspace
    a2y agent add <name> ...    add an agent (non-interactive; see docs/hiring.md)
    a2y agent list              list agents
    a2y render                  manifests -> deploy/
    a2y build                   build the agent image from ./image
    a2y up [agent ...]          ensure volumes, then docker compose up -d
    a2y down [agent ...]        docker compose stop
    a2y doctor                  check manifests, env parity, compose, logins
    a2y auth [agent]            print the interactive sign-in instructions
    a2y provision [agent]       print the messenger provisioning sequence

Run everything except `init` from a fleet workspace (the directory with
fleet.yaml).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import __version__
from .manifest import Fleet, ManifestError, load_fleet


def _fleet() -> Fleet:
    return load_fleet(Path.cwd())


def _compose(fleet: Fleet, *args: str) -> int:
    deploy = fleet.root / "deploy"
    env_file = deploy / ".env"
    cmd = ["docker", "compose", "-f", str(deploy / "docker-compose.yaml")]
    if env_file.is_file():
        cmd += ["--env-file", str(env_file)]
    cmd += list(args)
    return subprocess.call(cmd)


def cmd_init(ns: argparse.Namespace) -> int:
    from .scaffold import init_workspace

    dest = Path(ns.dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    name = ns.name or dest.name
    created = init_workspace(dest, name=name, first_agent=ns.agent)
    if not created:
        print(f"{dest}: nothing to do (already initialised)")
        return 0
    for rel in created:
        print(f"  created {rel}")
    print(
        f"\nNext:\n"
        f"  1. edit {dest / 'fleet.yaml'} and agents/{ns.agent}/\n"
        f"  2. a2y render && a2y build\n"
        f"  3. cp deploy/example.env deploy/.env  # then fill it\n"
        f"  4. a2y up && a2y auth {ns.agent}\n"
        f"  docs: see the agent2you pack's docs/ directory"
    )
    return 0


def cmd_render(_: argparse.Namespace) -> int:
    from .render import render_fleet

    fleet = _fleet()
    changed = render_fleet(fleet)
    if changed:
        for rel in changed:
            print(f"  wrote deploy/{rel}")
    else:
        print("deploy/ already up to date")
    return 0


def cmd_build(ns: argparse.Namespace) -> int:
    fleet = _fleet()
    image_dir = fleet.root / "image"
    dockerfile = image_dir / "agent.dockerfile"
    if not dockerfile.is_file():
        print(f"{dockerfile} not found -- was this workspace created by `a2y init`?", file=sys.stderr)
        return 1
    cmd = ["docker", "build", "-f", str(dockerfile), "-t", fleet.image_tag]
    if ns.no_cache:
        cmd.append("--no-cache")
    cmd.append(str(image_dir))
    print("+", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"Built {fleet.image_tag}. Set A2Y_IMAGE={fleet.image_tag} in deploy/.env.")
    return rc


def cmd_up(ns: argparse.Namespace) -> int:
    from .render import ensure_volumes, render_fleet

    fleet = _fleet()
    render_fleet(fleet)
    for d in ensure_volumes(fleet):
        print(f"  created {d.relative_to(fleet.root)}")
    services = [f"agent-{n}" for n in ns.agents] if ns.agents else []
    return _compose(fleet, "up", "-d", *services)


def cmd_down(ns: argparse.Namespace) -> int:
    fleet = _fleet()
    services = [f"agent-{n}" for n in ns.agents] if ns.agents else []
    return _compose(fleet, "stop", *services)


def cmd_doctor(_: argparse.Namespace) -> int:
    from .doctor import run_doctor

    return run_doctor(_fleet())


AUTH_TEXT = """\
=== {agent}: signing the brains in ===

The logins live in volumes/agent-{agent}/ and survive rebuilds; this is done once
per agent. Use DEVICE-CODE flows only: browser-callback logins listen on the
container's own localhost, which your browser cannot reach.

  docker exec -it agent-{agent} bash

{steps}
Forges (only if this agent uses them):

  gh auth login --hostname github.com --git-protocol ssh   # web-browser one-time code
  tea login add --name <forge> --url <gitea-url> --token <token>

Verify with a real turn, not with the login command's exit code: mention the agent
in its channel and watch the trace. A missing login surfaces as
`500 Authentication required` from acp2api on the FIRST turn only.
"""

AUTH_STEPS = {
    "claude": "  claude          # then /login -- pick the subscription, it prints a URL and a code\n",
    "codex": "  codex login --device-auth\n",
    "opencode": "  # opencode: point it at your endpoint in ~/.config/opencode/opencode.json\n",
    "cline": "  # cline: requires an interactive cline-account login before ACP works at all\n",
}


def cmd_auth(ns: argparse.Namespace) -> int:
    fleet = _fleet()
    agents = [a for a in fleet.agents if not ns.agent or a.name == ns.agent]
    if not agents:
        print(f"no agent named {ns.agent!r}", file=sys.stderr)
        return 1
    for a in agents:
        steps = "".join(
            AUTH_STEPS.get(a.executors[ex].get("kind") or ex, "") for ex in a.chain
        )
        print(AUTH_TEXT.format(agent=a.name, steps=steps))
    return 0


PROVISION_MM = """\
=== {agent}: Mattermost account (ordinary user + personal access token) ===

Agents are ordinary `system_user` accounts, NOT bot accounts -- Mattermost
refuses bots several things (incoming webhooks, for one), and a colleague should
read as a colleague.

Via the local admin socket (on the Mattermost host):

  M="docker exec <mattermost-container> mmctl --local"
  $M user create --email {agent}@{team}.local --username {agent} --password '<strong>'
  $M team users add {team} {agent}
  $M channel users add {team}:<channel> {agent}
  # a personal access token needs the role that permits one:
  #   sql: update users set roles='system_user system_user_access_token' where username='{agent}';
  $M token generate {agent} "agent gateway"

Or via the v4 REST API (works from anywhere; login_id must be the USERNAME --
the admin email answers 401 that reads like a wrong password):

  POST /api/v4/users/login  {{"login_id": "<admin-username>", "password": "..."}}
  POST /api/v4/users        -> user id
  PUT  /api/v4/users/{{id}}/roles   {{"roles": "system_user system_user_access_token"}}
  POST /api/v4/teams/<team>/members
  POST /api/v4/channels/<id>/members
  POST /api/v4/users/{{id}}/tokens  -> the token, RETURNED ONLY ONCE

Then:
  1. put the token in deploy/.env as {prefix}_MATTERMOST_TOKEN
  2. append the new USER ID to A2Y_MATTERMOST_ALLOWED_USERS
  3. recreate the OTHER agents (that list is container environment) -- without
     this, messages from the new colleague are dropped with no error anywhere.

Membership POSTs are no-ops when already present, so the sequence is safe to
re-run after a partial failure.
"""


def cmd_provision(ns: argparse.Namespace) -> int:
    fleet = _fleet()
    if fleet.platform_kind != "mattermost":
        print(f"platform.kind is {fleet.platform_kind!r}; provisioning docs cover mattermost. "
              "For other Hermes platforms create the bot/account per that platform's docs and "
              "pass its variables via platform.env.")
        return 0
    agents = [a for a in fleet.agents if not ns.agent or a.name == ns.agent]
    for a in agents:
        print(PROVISION_MM.format(agent=a.name, team=fleet.platform.get("team", "<team>"),
                                  prefix=a.env_prefix))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a2y", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"a2y {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a fleet workspace")
    p.add_argument("dir")
    p.add_argument("--name", help="fleet name (default: directory name)")
    p.add_argument("--agent", default="ana", help="name of the first agent (default: ana)")
    p.set_defaults(fn=cmd_init)

    from .agents_cmd import register as register_agent_cmd
    register_agent_cmd(sub)

    p = sub.add_parser("render", help="manifests -> deploy/")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("build", help="build the agent image")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("up", help="ensure volumes, docker compose up -d")
    p.add_argument("agents", nargs="*")
    p.set_defaults(fn=cmd_up)

    p = sub.add_parser("down", help="docker compose stop")
    p.add_argument("agents", nargs="*")
    p.set_defaults(fn=cmd_down)

    p = sub.add_parser("doctor", help="check the deployment end to end")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("auth", help="print brain sign-in instructions")
    p.add_argument("agent", nargs="?")
    p.set_defaults(fn=cmd_auth)

    p = sub.add_parser("provision", help="print messenger provisioning sequence")
    p.add_argument("agent", nargs="?")
    p.set_defaults(fn=cmd_provision)

    ns = parser.parse_args(argv)
    try:
        return ns.fn(ns)
    except ManifestError as exc:
        print(f"a2y: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
