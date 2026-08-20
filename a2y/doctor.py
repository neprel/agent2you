"""Read-only diagnostics for manifests, secrets, platform and runtime state."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import socket
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .manifest import Fleet
from .render import render_fleet


@dataclass
class DoctorOptions:
    offline: bool = False
    probe_brains: bool = False
    timeout: float = 3.0


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip("\"'")
    return values


def _tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def check_deploy(fleet: Fleet, _env: dict, _opts: DoctorOptions):
    deploy = fleet.root / "deploy"
    if not deploy.is_dir():
        return "problem", "deploy/ does not exist -- run `a2y render`"
    with tempfile.TemporaryDirectory() as td:
        rendered = Path(td)
        render_fleet(fleet, rendered)
        actual = {k: v for k, v in _tree(deploy).items() if k != ".env"}
        if _tree(rendered) != actual:
            return "problem", "deploy/ differs from a read-only render -- run `a2y render`"
    return "ok", "deploy/ matches the manifests"


def check_env(fleet: Fleet, env: dict, _opts: DoctorOptions):
    example = fleet.root / "deploy" / "example.env"
    if not env:
        return "problem", "deploy/.env is missing or empty"
    required = parse_env(example.read_text()) if example.is_file() else {}
    missing = sorted(set(required) - set(env))
    return (
        ("problem", ".env is missing: " + ", ".join(missing))
        if missing
        else ("ok", ".env carries every example.env variable")
    )


def check_version(fleet: Fleet, _env: dict, _opts: DoctorOptions):
    try:
        installed = importlib.metadata.version("agent2you")
    except importlib.metadata.PackageNotFoundError:
        installed = __version__
    stamp = fleet.root / ".a2y-version"
    current = stamp.read_text().strip() if stamp.is_file() else "unknown"
    if current != installed:
        return "problem", f"workspace pack is {current}, installed pack is {installed}; run `a2y upgrade`"
    if _opts.offline:
        return "ok", f"workspace and installed pack are {installed}; image label skipped offline"

    image_root = _env.get("A2Y_IMAGE") or fleet.image_tag
    images = sorted(
        {f"{image_root}-{agent.name}" if agent.toolkits else image_root for agent in fleet.agents}
    )
    labels: dict[str, str] = {}
    for image in images:
        try:
            probe = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    image,
                    "--format",
                    '{{ index .Config.Labels "org.agent2you.version" }}',
                ],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return "problem", "Docker is unavailable; run doctor on the daemon host"
        if probe.returncode:
            return "problem", f"cannot inspect {image}; run `a2y build`"
        labels[image] = probe.stdout.strip() or "missing"

    skew = [f"{image} label={label}" for image, label in labels.items() if label != installed]
    if skew:
        return (
            "problem",
            f"version skew: installed/workspace={installed}; "
            + ", ".join(skew)
            + "; run `a2y build` with this installed pack, or use --a2y-version intentionally",
        )
    return "ok", f"installed pack, workspace and image labels are {installed}"


def _expiry(path: Path):
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return "invalid credential JSON"
    stack, candidates = [data], []
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                candidates.append(child) if key.lower() in {
                    "expiresat",
                    "expires_at",
                    "expiry",
                } else stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    for value in candidates:
        try:
            raw = str(value)
            when = (
                datetime.fromtimestamp(float(value) / (1000 if float(value) > 10**11 else 1), timezone.utc)
                if raw.replace(".", "", 1).isdigit()
                else datetime.fromisoformat(raw.replace("Z", "+00:00"))
            )
            hours = (when - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours < 0:
                return "expired"
            if hours < 48:
                return f"expires in {hours:.0f}h"
        except (ValueError, TypeError, OverflowError):
            continue
    return "present"


def check_logins(fleet: Fleet, _env: dict, _opts: DoctorOptions):
    if _opts.offline:
        return "info", "credential checks skipped offline"
    issues = []
    for agent in fleet.agents:
        base = fleet.root / "volumes" / agent.container
        for name, rel in (("claude", "claude/.credentials.json"), ("codex", "codex/auth.json")):
            if any((agent.executors[e].get("kind") or e) == name for e in agent.chain):
                state = _expiry(base / rel)
                if state != "present":
                    issues.append(f"{agent.name}/{name}: {state or 'not signed in'}")
    return (
        ("problem", "; ".join(issues))
        if issues
        else ("ok", "credential files present and not near known expiry")
    )


def check_duties(fleet: Fleet, _env: dict, _opts: DoctorOptions):
    stale = []
    for agent in fleet.agents:
        quiet = {str(d["name"]): d for d in agent.duties if d.get("quiet")}
        if not quiet:
            continue
        jobs_file = fleet.root / "volumes" / agent.container / "hermes" / "cron" / "jobs.json"
        if not jobs_file.is_file():
            stale.extend(f"{agent.name}/{name}: never observed" for name in quiet)
            continue
        try:
            jobs = json.loads(jobs_file.read_text())
        except (OSError, json.JSONDecodeError):
            stale.append(f"{agent.name}: unreadable cron/jobs.json")
            continue
        by_name = {job.get("name"): job for job in jobs if isinstance(job, dict)}
        for name, duty in quiet.items():
            job = by_name.get(name)
            if not job or not job.get("last_run_at"):
                stale.append(f"{agent.name}/{name}: never ran")
                continue
            try:
                last = datetime.fromisoformat(str(job["last_run_at"]).replace("Z", "+00:00"))
                fields = str(duty["schedule"]).split()
                if fields[0].startswith("*/"):
                    max_age = int(fields[0][2:]) * 2 * 60 + 300
                elif fields[1].startswith("*/"):
                    max_age = int(fields[1][2:]) * 2 * 3600 + 3600
                elif fields[4] != "*":
                    max_age = 15 * 86400
                else:
                    max_age = 49 * 3600
                age = (datetime.now(timezone.utc) - last).total_seconds()
                if age > max_age:
                    stale.append(f"{agent.name}/{name}: last ran {age / 3600:.0f}h ago")
            except (ValueError, TypeError, IndexError):
                stale.append(f"{agent.name}/{name}: invalid last_run_at")
    if stale:
        level = "info" if _opts.offline else "problem"
        return level, "; ".join(stale)
    return "ok", "quiet duties have native Hermes run state"


def check_hygiene(fleet: Fleet, _env: dict, _opts: DoctorOptions):
    dotenv = fleet.root / "deploy" / ".env"
    issues = []
    if dotenv.is_file() and dotenv.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        issues.append("deploy/.env is group/world accessible (chmod 600)")
    if (fleet.root / ".git").exists():
        ignored = subprocess.run(["git", "check-ignore", "-q", "deploy/.env"], cwd=fleet.root).returncode == 0
        tracked = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "deploy/.env"], cwd=fleet.root, capture_output=True
            ).returncode
            == 0
        )
        if not ignored:
            issues.append("deploy/.env is not gitignored")
        if tracked:
            issues.append("deploy/.env is tracked; git rm --cached it and rotate every contained secret")
    return (
        ("problem", "; ".join(issues))
        if issues
        else ("ok", "secret files are ignored and restrictively permissioned")
    )


def check_browser(fleet: Fleet, env: dict, opts: DoctorOptions):
    agents = [agent for agent in fleet.agents if "browser" in agent.toolkits]
    if not agents:
        return "info", "no agent carries the browser toolkit"
    if opts.offline:
        return "info", "browser runtime checks skipped offline"
    failures = []
    for agent in agents:
        try:
            probe = subprocess.run(
                ["docker", "exec", agent.container, "a2y-browser-check"],
                capture_output=True,
                text=True,
                timeout=max(30.0, opts.timeout),
            )
            if probe.returncode:
                detail = (probe.stderr or probe.stdout).strip().splitlines()
                failures.append(f"{agent.name}: {(detail[-1] if detail else 'browser check failed')}")
                continue
            if agent.browser_novnc:
                port = env.get(f"{agent.env_prefix}_BROWSER_NOVNC_PORT", "")
                if not port:
                    failures.append(f"{agent.name}: noVNC port is unset")
                    continue
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/vnc.html", timeout=opts.timeout
                ) as response:
                    if response.status >= 400:
                        failures.append(f"{agent.name}: noVNC returned HTTP {response.status}")
        except (OSError, subprocess.TimeoutExpired, urllib.error.URLError) as exc:
            failures.append(f"{agent.name}: {exc}")
    if failures:
        return "problem", "; ".join(failures)
    return "ok", "Chromium launches, Playwright MCP responds, profiles are writable and noVNC is reachable"


def check_models(fleet: Fleet, _env: dict, _opts: DoctorOptions):
    agents = [agent for agent in fleet.agents if agent.model_specs]
    if not agents:
        return "info", "no agent carries a toolkit with external models"
    store = fleet.root / "volumes" / "models"
    manifest_path = store / "manifest.json"
    if not manifest_path.is_file():
        names = ", ".join(agent.name for agent in agents)
        return "info", f"model store is empty for {names}; run `a2y models pull {agents[0].name}`"
    try:
        manifest = json.loads(manifest_path.read_text())
        entries = {str(item["path"]): item for item in manifest.get("models") or []}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return "info", f"model store manifest is unreadable ({exc}); rerun `a2y models pull`"

    issues = []
    tier = str(manifest.get("tier") or "fallback")
    for agent in agents:
        for model in agent.model_specs:
            if model.get("gated") and tier != str(model.get("tier")):
                continue
            path = str(model["path"])
            entry = entries.get(path)
            if not entry:
                issues.append(f"{agent.name}:{model['name']} absent (run `a2y models pull {agent.name}`)")
                continue
            for relative, expected in (entry.get("sha256") or {}).items():
                target = store / path / str(relative)
                if not target.is_file():
                    issues.append(f"{model['name']}/{relative} absent")
                    continue
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    issues.append(f"{model['name']}/{relative} checksum changed")
    if issues:
        return "info", f"tier={tier}; " + "; ".join(issues)
    revisions = ", ".join(
        f"{item.get('name')}@{str(item.get('revision', 'unknown'))[:12]} "
        f"(pulled {item.get('pulled_at') or manifest.get('pulled_at')})"
        for item in manifest.get("models") or []
    )
    return "ok", f"model store tier={tier}; checksums match; {revisions}"


def _get_json(url: str, token: str = "", timeout: float = 3.0, scheme: str = "Bearer"):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"{scheme} {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def check_platform(fleet: Fleet, env: dict, opts: DoctorOptions):
    if opts.offline or fleet.platform_kind == "none":
        return "info", "online platform probes skipped"
    try:
        if fleet.platform_kind == "mattermost":
            base = env.get("A2Y_MATTERMOST_URL", "").rstrip("/")
            _get_json(f"{base}/api/v4/system/ping", timeout=opts.timeout)
            allowed = {x.strip() for x in env.get("A2Y_MATTERMOST_ALLOWED_USERS", "").split(",") if x.strip()}
            missing = []
            for agent in fleet.agents:
                me = _get_json(
                    f"{base}/api/v4/users/me",
                    env.get(f"{agent.env_prefix}_MATTERMOST_TOKEN", ""),
                    opts.timeout,
                )
                if str(me.get("id") or "") not in allowed:
                    missing.append(agent.name)
            if missing:
                return "problem", "agent ids absent from A2Y_MATTERMOST_ALLOWED_USERS: " + ", ".join(missing)
        elif fleet.platform_kind == "telegram":
            if not env.get("A2Y_TELEGRAM_ALLOWED_USERS"):
                return "problem", "A2Y_TELEGRAM_ALLOWED_USERS is empty"
            for agent in fleet.agents:
                token = env.get(f"{agent.env_prefix}_TELEGRAM_BOT_TOKEN", "")
                _get_json(f"https://api.telegram.org/bot{token}/getMe", timeout=opts.timeout)
        elif fleet.platform_kind == "slack":
            if not env.get("A2Y_SLACK_ALLOWED_USERS"):
                return "problem", "A2Y_SLACK_ALLOWED_USERS is empty"
            for agent in fleet.agents:
                data = _get_json(
                    "https://slack.com/api/auth.test",
                    env.get(f"{agent.env_prefix}_SLACK_BOT_TOKEN", ""),
                    opts.timeout,
                )
                if not data.get("ok"):
                    return "problem", f"{agent.name} Slack auth.test rejected the bot token"
        elif fleet.platform_kind == "discord":
            if not env.get("A2Y_DISCORD_ALLOWED_USERS"):
                return "problem", "A2Y_DISCORD_ALLOWED_USERS is empty"
            for agent in fleet.agents:
                _get_json(
                    "https://discord.com/api/v10/users/@me",
                    env.get(f"{agent.env_prefix}_DISCORD_BOT_TOKEN", ""),
                    opts.timeout,
                    "Bot",
                )
        elif fleet.platform_kind == "teams":
            if not env.get("A2Y_TEAMS_ALLOWED_USERS"):
                return "problem", "A2Y_TEAMS_ALLOWED_USERS is empty"
            tenant = env.get("A2Y_TEAMS_TENANT_ID", "")
            body = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": env.get("A2Y_TEAMS_CLIENT_ID", ""),
                    "client_secret": env.get("A2Y_TEAMS_CLIENT_SECRET", ""),
                    "scope": "https://api.botframework.com/.default",
                }
            ).encode()
            request = urllib.request.Request(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data=body,
            )
            with urllib.request.urlopen(request, timeout=opts.timeout) as response:
                if not json.loads(response.read()).get("access_token"):
                    return "problem", "Teams client credentials returned no access token"
            try:
                urllib.request.urlopen(fleet.platform["public_endpoint"], timeout=opts.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code >= 500:
                    raise
        for agent in fleet.agents:
            email = agent.channels.get("email") or {}
            if email:
                for host, port in (
                    (email["imap_host"], int(email.get("imap_port", 993))),
                    (email["smtp_host"], int(email.get("smtp_port", 587))),
                ):
                    with socket.create_connection((str(host), port), timeout=opts.timeout):
                        pass
    except Exception as exc:
        return "problem", f"platform probe failed: {exc}"
    return "ok", f"{fleet.platform_kind} reachable and tokens accepted"


CHECKS = [
    ("deploy", check_deploy),
    ("environment", check_env),
    ("upgrade", check_version),
    ("brains", check_logins),
    ("duties", check_duties),
    ("hygiene", check_hygiene),
    ("models", check_models),
    ("browser", check_browser),
    ("platform", check_platform),
]


def run_doctor(fleet: Fleet, opts: DoctorOptions | None = None) -> int:
    opts = opts or DoctorOptions()
    dotenv = fleet.root / "deploy" / ".env"
    env = parse_env(dotenv.read_text()) if dotenv.is_file() else {}
    problems = 0
    for section, check in CHECKS:
        level, message = check(fleet, env, opts)
        mark = {"ok": "✓", "problem": "✗", "info": "·"}[level]
        print(f"{section}\n  {mark} {message}")
        problems += level == "problem"
    print(f"\n{problems} problem(s)." if problems else "\nNo problems found.")
    return 1 if problems else 0
