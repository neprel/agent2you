"""Read-only, explicitly network-using pin comparison."""

from __future__ import annotations

import importlib.metadata
import json
import re
import urllib.request

from .manifest import Fleet

PIN_SOURCES = {
    "LITELLM_VERSION": ("pypi", "litellm"),
    "FASTAPI_VERSION": ("pypi", "fastapi"),
    "CLAUDE_CODE_VERSION": ("npm", "@anthropic-ai/claude-code"),
    "CODEX_VERSION": ("npm", "@openai/codex"),
    "OPENCODE_VERSION": ("npm", "opencode-ai"),
    "CLINE_VERSION": ("npm", "cline"),
    "ACP2API_VERSION": ("npm", "acp2api"),
    "BOARDS_MCP_VERSION": ("npm", "mattermost-boards-mcp"),
    "PLAYBOOKS_MCP_VERSION": ("npm", "mattermost-playbooks-mcp"),
    "PHOENIX_MCP_VERSION": ("npm", "@arizeai/phoenix-mcp"),
    "HINT_VERSION": ("npm", "@openhint/cli"),
    "HINTBOOK_VERSION": ("npm", "@openhint/hintbook-software-engineer"),
    "HINDSIGHT_CLIENT_VERSION": ("pypi", "hindsight-client"),
    "FIRECRAWL_ANYDOC_VERSION": ("pypi", "firecrawl-anydoc"),
    "SPECIFY_VERSION": ("pypi", "specify-cli"),
    "OPENSPEC_VERSION": ("npm", "@fission-ai/openspec"),
    "YARN_VERSION": ("npm", "@yarnpkg/cli-dist"),
    "AGENT2YOU_VERSION": ("pypi", "agent2you"),
    "GH_VERSION": ("github", "cli/cli"),
    "HERMES_OTEL_VERSION": ("github", "briancaffey/hermes-otel"),
    "HERMES_COMMIT": ("github_commit", "NousResearch/hermes-agent"),
    "TEA_VERSION": ("gitea", "gitea/tea"),
}


def _latest(kind: str, name: str) -> str:
    if kind == "pypi":
        url = f"https://pypi.org/pypi/{name}/json"
    elif kind == "npm":
        url = f"https://registry.npmjs.org/{name}/latest"
    elif kind == "github":
        url = f"https://api.github.com/repos/{name}/releases/latest"
    elif kind == "github_commit":
        url = f"https://api.github.com/repos/{name}/commits/main"
    else:
        url = f"https://gitea.com/api/v1/repos/{name}/releases/latest"
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.load(response)
    if kind == "pypi":
        return str(data["info"]["version"])
    if kind == "npm":
        return str(data["version"])
    if kind == "github_commit":
        return str(data["sha"])
    return str(data["tag_name"]).removeprefix("v").removeprefix("hermes-otel-v")


def _older(current: str, latest: str) -> bool:
    def key(value: str):
        parts = re.findall(r"\d+", value)
        return tuple(int(part) for part in parts[:4])

    return current != latest if len(current) > 20 else key(current) < key(latest)


def collect(fleet: Fleet) -> dict:
    installed = importlib.metadata.version("agent2you")
    stamp = (fleet.root / ".a2y-version").read_text().strip()
    results = [
        {
            "name": "agent2you/workspace",
            "current": stamp,
            "latest": installed,
            "source": "installed",
            "outdated": _older(stamp, installed),
        }
    ]
    dockerfile = (fleet.root / "image/agent.dockerfile").read_text()
    pins = dict(re.findall(r"^ARG ([A-Z0-9_]+)=([^\s]+)", dockerfile, re.M))
    errors = []
    try:
        latest_pack = _latest("pypi", "agent2you")
        results.append(
            {
                "name": "agent2you/PyPI",
                "current": installed,
                "latest": latest_pack,
                "source": "PyPI",
                "outdated": _older(installed, latest_pack),
            }
        )
    except Exception as exc:
        errors.append(f"agent2you: {exc}")
    for key, (kind, package) in PIN_SOURCES.items():
        if key not in pins:
            continue
        try:
            latest = _latest(kind, package)
            results.append(
                {
                    "name": key,
                    "current": pins[key],
                    "latest": latest,
                    "source": f"{kind}:{package}",
                    "outdated": _older(pins[key], latest),
                }
            )
        except Exception as exc:
            errors.append(f"{key}: {exc}")
    return {
        "network_used": True,
        "updates": results,
        "errors": errors,
        "ritual": (
            "Read _.hint {#pins}; LiteLLM and FastAPI pins are coupled. Use plan-01 "
            "a2y upgrade, then plan-15 rolling rebuild. No changes were made."
        ),
    }


def cmd_outdated(ns, fleet: Fleet) -> int:
    report = collect(fleet)
    if ns.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["updates"]:
            print(
                f"{'OUTDATED' if item['outdated'] else 'current':8} {item['name']}: "
                f"{item['current']} -> {item['latest']}"
            )
        print(report["ritual"])
    return 1 if report["errors"] else 0
