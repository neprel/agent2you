"""The a2y command line.

    a2y bootstrap               print the bootloader prompt for a coding agent
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
import importlib.metadata
import os
import platform
import re
import shutil
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


def _daemon_guard(allow: bool = False) -> None:
    host = os.environ.get("DOCKER_HOST", "")
    remote = bool(host and not host.startswith(("unix://", "npipe://")))
    if not remote and shutil.which("docker"):
        shown = subprocess.run(["docker", "context", "show"], capture_output=True, text=True)
        remote = shown.returncode == 0 and shown.stdout.strip() not in {"", "default", "desktop-linux"}
    if remote and not allow:
        raise ManifestError(
            "remote Docker daemon refused: compose bind mounts resolve on the daemon host; "
            "run a2y on that host or pass --i-know-my-mounts"
        )


def cmd_bootstrap(_: argparse.Namespace) -> int:
    """Print the bootloader: a self-contained prompt that turns any coding
    agent into the installer of this operator's first fleet agent. Hand it to
    Claude Code / Codex with: run `a2y bootstrap` and follow what it prints —
    or fetch it without installing anything:
    `uvx agent2you bootstrap`."""
    from importlib import resources

    sys.stdout.write((resources.files("a2y") / "bootstrap.md").read_text())
    return 0


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
    _daemon_guard(ns.i_know_my_mounts)
    from .render import render_fleet

    fleet = _fleet()
    image_dir = fleet.root / "image"
    dockerfile = image_dir / "agent.dockerfile"
    if not dockerfile.is_file():
        print(f"{dockerfile} not found -- was this workspace created by `a2y init`?", file=sys.stderr)
        return 1
    installed = importlib.metadata.version("agent2you")
    match = re.search(r"^ARG AGENT2YOU_VERSION=(\S+)$", dockerfile.read_text(), re.M)
    fallback = match.group(1) if match else ""
    release_version = bool(re.fullmatch(r"\d+(?:\.\d+)*(?:\.post\d+)?", installed))
    if ns.a2y_version:
        image_a2y_version = ns.a2y_version
    elif release_version:
        image_a2y_version = installed
    elif fallback:
        image_a2y_version = fallback
        print(
            f"NOTE: running a2y {installed}; container gets {fallback} from the dockerfile "
            "-- pass --a2y-version to override. It matches that version as published, "
            "not this checkout.",
            file=sys.stderr,
        )
    else:
        print(
            f"cannot select in-container a2y for development version {installed}: "
            "the dockerfile has no AGENT2YOU_VERSION default; pass --a2y-version",
            file=sys.stderr,
        )
        return 1
    render_fleet(fleet)  # the derived toolkit dockerfiles live in deploy/build/

    tag = fleet.image_tag
    build_dir = fleet.root / "deploy" / "build"
    # The chain: base -> fleet toolkits (takes the tag agents run) -> per-agent.
    plan: list[tuple[Path, str, Path]] = []
    base_tag = f"{tag}-base" if fleet.image_toolkits else tag
    plan.append((dockerfile, base_tag, image_dir))
    if fleet.image_toolkits:
        plan.append((build_dir / "fleet.dockerfile", tag, build_dir))
    for a in fleet.agents:
        if a.toolkits:
            plan.append((build_dir / f"agent-{a.name}.dockerfile", f"{tag}-{a.name}", build_dir))

    def build_one(item) -> int:
        df, t, ctx = item
        cmd = [
            "docker",
            "build",
            "--build-arg",
            f"AGENT2YOU_VERSION={image_a2y_version}",
            "-f",
            str(df),
            "-t",
            t,
        ]
        if ns.no_cache:
            cmd.append("--no-cache")
        cmd.append(str(ctx))
        print("+", " ".join(cmd))
        return subprocess.call(cmd)

    barrier = 2 if fleet.image_toolkits else 1
    for item in plan[:barrier]:
        rc = build_one(item)
        if rc != 0:
            return rc
    tail = plan[barrier:]
    if tail and ns.parallel > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=ns.parallel) as pool:
            results = list(pool.map(build_one, tail))
        if any(results):
            return next(rc for rc in results if rc)
    else:
        for item in tail:
            rc = build_one(item)
            if rc:
                return rc
    print(f"Built {', '.join(t for _, t, _ in plan)}. A2Y_IMAGE={tag} in deploy/.env.")
    return 0


def cmd_up(ns: argparse.Namespace) -> int:
    from .render import ensure_volumes, render_fleet

    fleet = _fleet()
    _daemon_guard(ns.i_know_my_mounts)
    if platform.system() == "Darwin" and fleet.shared_namespace:
        raise ManifestError("VPN/shared-network-namespace fleets are unsupported on Docker Desktop macOS")
    render_fleet(fleet)
    for d in ensure_volumes(fleet):
        print(f"  created {d.relative_to(fleet.root)}")
    services = [f"agent-{n}" for n in ns.agents] if ns.agents else []
    return _compose(fleet, "up", "-d", *services)


def cmd_down(ns: argparse.Namespace) -> int:
    fleet = _fleet()
    _daemon_guard(ns.i_know_my_mounts)
    services = [f"agent-{n}" for n in ns.agents] if ns.agents else []
    return _compose(fleet, "stop", *services)


def cmd_doctor(ns: argparse.Namespace) -> int:
    from .doctor import DoctorOptions, run_doctor

    return run_doctor(_fleet(), DoctorOptions(offline=ns.offline, probe_brains=ns.probe_brains))


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
        steps = "".join(AUTH_STEPS.get(a.executors[ex].get("kind") or ex, "") for ex in a.chain)
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

PROVISION_TELEGRAM = """\
=== {agent}: Telegram bot ===

In @BotFather create one bot for this agent. Put its token in deploy/.env as
{prefix}_TELEGRAM_BOT_TOKEN and put the operator's numeric Telegram user id in
A2Y_TELEGRAM_ALLOWED_USERS. For group routing disable Group Privacy Mode; for
agent-to-agent messages enable Bot-to-Bot Communication Mode. Add every fleet
bot to the group and verify a real two-bot @mention before relying on routing.
"""

PROVISION_SLACK = """\
=== {agent}: Slack app (Socket Mode) ===

Create one Slack app per agent. Run `hermes slack manifest --agent-view --write`
inside a temporary Hermes environment and paste the generated manifest at
api.slack.com/apps. Enable Socket Mode, install the app, then set:
  {prefix}_SLACK_BOT_TOKEN=xoxb-...
  {prefix}_SLACK_APP_TOKEN=xapp-...   # connections:write
  {prefix}_SLACK_HOME_CHANNEL=C...
Add immutable member ids to A2Y_SLACK_ALLOWED_USERS. Invite every fleet bot to
the shared channels. Missing message.channels/message.groups/message.mpim events
is a silent no-delivery failure; reinstall after changing scopes.
"""

PROVISION_DISCORD = """\
=== {agent}: Discord bot application ===

Create one application/bot in the Discord Developer Portal. Enable Server
Members Intent and Message Content Intent in the portal, invite it with bot and
applications.commands scopes, then set:
  {prefix}_DISCORD_BOT_TOKEN=...
  {prefix}_DISCORD_HOME_CHANNEL=...
Add immutable user ids to A2Y_DISCORD_ALLOWED_USERS. The rendered gateway accepts
other bots only on explicit mentions; verify a real human turn and two-bot turn.
"""

PROVISION_TEAMS = """\
=== Microsoft Teams enterprise front door ({agent}) ===

Teams uses a public HTTPS Bot Framework webhook; there is no Socket-Mode/NAT
alternative. Point the Azure Bot messaging endpoint at:
  {endpoint}

1. Register a single-tenant Entra application and create a client secret.
2. Create an Azure Bot resource using that application id and enable Teams.
3. Replace placeholders in platforms/teams/manifest.json, add icons, zip the
   manifest, and upload it in Teams Admin Center (admin consent may be required).
4. Set A2Y_TEAMS_CLIENT_ID, A2Y_TEAMS_CLIENT_SECRET, A2Y_TEAMS_TENANT_ID,
   A2Y_TEAMS_ALLOWED_USERS (AAD object ids), A2Y_TEAMS_HOME_CHANNEL, and expose
   A2Y_TEAMS_PORT through the HTTPS reverse proxy/tunnel.

This is a human-to-fleet front door, not an agent office: Teams bots do not
receive other bots' messages. The default gateway agent routes internally.
"""


def cmd_provision(ns: argparse.Namespace) -> int:
    fleet = _fleet()
    if fleet.platform_kind == "telegram":
        agents = [a for a in fleet.agents if not ns.agent or a.name == ns.agent]
        for a in agents:
            print(PROVISION_TELEGRAM.format(agent=a.name, prefix=a.env_prefix))
        return 0
    if fleet.platform_kind in {"slack", "discord"}:
        agents = [a for a in fleet.agents if not ns.agent or a.name == ns.agent]
        template = PROVISION_SLACK if fleet.platform_kind == "slack" else PROVISION_DISCORD
        for a in agents:
            print(template.format(agent=a.name, prefix=a.env_prefix))
        return 0
    if fleet.platform_kind == "teams":
        gateway = str(fleet.platform.get("gateway_agent") or fleet.agents[0].name)
        if ns.agent and ns.agent != gateway:
            raise ManifestError(f"Teams has one gateway agent {gateway!r}; provision that agent")
        print(PROVISION_TEAMS.format(agent=gateway, endpoint=fleet.platform["public_endpoint"]))
        return 0
    if fleet.platform_kind != "mattermost":
        print(
            f"platform.kind is {fleet.platform_kind!r}; provisioning docs cover mattermost. "
            "For other Hermes platforms create the bot/account per that platform's docs and "
            "pass its variables via platform.env."
        )
        return 0
    agents = [a for a in fleet.agents if not ns.agent or a.name == ns.agent]
    for a in agents:
        print(
            PROVISION_MM.format(agent=a.name, team=fleet.platform.get("team", "<team>"), prefix=a.env_prefix)
        )
    return 0


def cmd_backup_cli(ns: argparse.Namespace) -> int:
    from .backup import cmd_backup

    return cmd_backup(ns, _fleet())


def cmd_restore_cli(ns: argparse.Namespace) -> int:
    from .backup import cmd_restore

    return cmd_restore(ns, _fleet())


def cmd_knowledge_cli(ns: argparse.Namespace) -> int:
    from .knowledge import cmd_knowledge

    return cmd_knowledge(ns, _fleet())


def cmd_drill_cli(ns: argparse.Namespace) -> int:
    from .drill import cmd_drill

    return cmd_drill(ns, _fleet())


def cmd_rotate_cli(ns: argparse.Namespace) -> int:
    from .rotate import cmd_rotate

    return cmd_rotate(ns, _fleet())


def cmd_outdated_cli(ns: argparse.Namespace) -> int:
    from .outdated import cmd_outdated

    return cmd_outdated(ns, _fleet())


def cmd_duty_templates(_: argparse.Namespace) -> int:
    from importlib import resources

    sys.stdout.write((resources.files("a2y") / "duty-templates.yaml").read_text())
    return 0


def cmd_rebuild(ns: argparse.Namespace) -> int:
    """Snapshot, rebuild, recreate, then run reusable offline identity checks."""
    from .backup import create_backup
    from .doctor import DoctorOptions, run_doctor

    fleet = _fleet()
    _daemon_guard(ns.i_know_my_mounts)
    names = [a.name for a in fleet.agents] if ns.all else [ns.agent]
    known = {a.name for a in fleet.agents}
    if any(name not in known for name in names):
        raise ManifestError("unknown agent in rebuild request")
    for name in names:
        print(f"=== rebuilding {name} ===")
        _compose(fleet, "stop", f"agent-{name}")
        if not ns.no_backup:
            archive = create_backup(fleet, name, fleet.root / "backup")
            print(f"  snapshot {archive}")
        build_ns = argparse.Namespace(
            no_cache=ns.no_cache,
            parallel=1,
            i_know_my_mounts=ns.i_know_my_mounts,
            a2y_version=None,
        )
        if cmd_build(build_ns):
            return 1
        if _compose(fleet, "up", "-d", "--force-recreate", f"agent-{name}"):
            return 1
        rc = run_doctor(fleet, DoctorOptions(offline=True))
        if rc:
            print(f"rebuild verification failed at {name}; rolling rebuild stopped", file=sys.stderr)
            return rc
        print(
            "  identity files and offline continuity checks passed; run online doctor "
            "for a real platform/brain probe"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="a2y", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"a2y {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap", help="print the bootloader prompt for a coding agent")
    p.set_defaults(fn=cmd_bootstrap)

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
    p.add_argument("--a2y-version", help="explicit in-container agent2you version")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--i-know-my-mounts", action="store_true")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("up", help="ensure volumes, docker compose up -d")
    p.add_argument("agents", nargs="*")
    p.add_argument("--i-know-my-mounts", action="store_true")
    p.set_defaults(fn=cmd_up)

    p = sub.add_parser("down", help="docker compose stop")
    p.add_argument("agents", nargs="*")
    p.add_argument("--i-know-my-mounts", action="store_true")
    p.set_defaults(fn=cmd_down)

    p = sub.add_parser("doctor", help="check the deployment end to end")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--probe-brains", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("upgrade", help="three-way update the pack-owned image tree")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    from .upgrade import cmd_upgrade

    p.set_defaults(fn=cmd_upgrade)

    p = sub.add_parser("backup", help="archive credential-bearing agent state")
    p.add_argument("agents", nargs="*")
    p.add_argument("--out")
    p.add_argument("--cold", action="store_true")
    p.add_argument("--include-work", action="store_true")
    p.set_defaults(fn=cmd_backup_cli)

    p = sub.add_parser("restore", help="restore one agent state archive")
    p.add_argument("archive")
    p.add_argument("--agent")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_restore_cli)

    p = sub.add_parser("rebuild", help="snapshot, rebuild, recreate and verify an agent")
    p.add_argument("agent", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--i-know-my-mounts", action="store_true")
    p.set_defaults(fn=cmd_rebuild)

    p = sub.add_parser("models", help="manage the shared host-side model store")
    msub = p.add_subparsers(dest="models_cmd", required=True)
    pull_p = msub.add_parser("pull", help="NETWORK: download models and verify them offline")
    pull_p.add_argument("agents", nargs="*", help="agents whose toolkit models should be pulled")
    pull_p.add_argument("--agent", help="one agent (equivalent to a positional name)")
    from .models import cmd_models_pull

    pull_p.set_defaults(fn=cmd_models_pull)

    p = sub.add_parser("knowledge", help="curate local HINT memory")
    ksub = p.add_subparsers(dest="knowledge_cmd", required=True)
    remember_p = ksub.add_parser("remember")
    remember_p.add_argument("agent")
    remember_p.add_argument("--topic", required=True)
    remember_p.add_argument("--text", required=True)
    remember_p.set_defaults(fn=cmd_knowledge_cli)
    retract_p = ksub.add_parser("retract")
    retract_p.add_argument("agent")
    retract_p.add_argument("--topic", required=True)
    retract_p.add_argument("--superseded-by")
    retract_p.set_defaults(fn=cmd_knowledge_cli)

    p = sub.add_parser("duties", help="inspect declarative recurring-duty helpers")
    dsub = p.add_subparsers(dest="duties_cmd", required=True)
    templates_p = dsub.add_parser("templates", help="print cost, quota and gardener duty templates")
    templates_p.set_defaults(fn=cmd_duty_templates)

    p = sub.add_parser("drill", help="run deterministic behavior probes through the live chat platform")
    p.add_argument("agent")
    p.add_argument("--max", type=int, default=10, help="maximum real turns to spend (default: 10)")
    p.set_defaults(fn=cmd_drill_cli)

    p = sub.add_parser("rotate", help="rotate secrets; external classes stop at an explicit human step")
    p.add_argument(
        "rotation_class",
        nargs="?",
        choices=["litellm-keys", "platform-token", "github-token", "ssh-key"],
    )
    p.add_argument("agents", nargs="*")
    p.add_argument("--all-internal", action="store_true")
    p.add_argument("--no-recreate", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_rotate_cli)

    p = sub.add_parser(
        "outdated",
        help="NETWORK: compare pack/image pins with PyPI and npm; never changes files",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_outdated_cli)

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
