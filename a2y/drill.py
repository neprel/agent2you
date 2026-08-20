"""Deterministic behavioral probes sent through the running chat platform."""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable

import yaml

from .doctor import parse_env
from .manifest import Fleet, ManifestError


def evaluate(reply: str | None, expect: dict) -> list[str]:
    text = reply or ""
    failures = []
    if expect.get("silent") is True and reply:
        failures.append("expected silence")
    if expect.get("answers") is True and not text.strip():
        failures.append("expected a non-empty answer")
    if expect.get("refuses") is True and not any(
        word in text.casefold() for word in ("cannot", "can't", "won't", "refuse", "не могу", "не буду")
    ):
        failures.append("expected an explicit refusal")
    for value in expect.get("mentions") or []:
        if f"@{value}" not in text:
            failures.append(f"missing mention @{value}")
    for value in expect.get("contains") or []:
        if str(value) not in text:
            failures.append(f"missing literal {value!r}")
    for value in expect.get("not_contains") or []:
        if str(value) in text:
            failures.append(f"forbidden literal {value!r}")
    return failures


def _request(url: str, token: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def mattermost_exchange(fleet: Fleet, probe: str, timeout: float = 45) -> str | None:
    env_path = fleet.root / "deploy/.env"
    env = parse_env(env_path.read_text()) if env_path.is_file() else {}
    base = env.get("A2Y_MATTERMOST_URL", "").rstrip("/") + "/api/v4"
    token = env.get("A2Y_DRILL_TOKEN", "")
    channel = env.get("A2Y_DRILLS_CHANNEL", "")
    if not token or not channel:
        raise ManifestError("Mattermost drills need A2Y_DRILL_TOKEN and A2Y_DRILLS_CHANNEL in deploy/.env")
    post = _request(f"{base}/posts", token, "POST", {"channel_id": channel, "message": probe})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        thread = _request(f"{base}/posts/{post['id']}/thread", token)
        replies = [p for p in thread.get("posts", {}).values() if p.get("id") != post["id"]]
        if replies:
            replies.sort(key=lambda item: item.get("create_at", 0))
            return str(replies[-1].get("message") or "")
        time.sleep(2)
    return None


def run_drills(
    fleet: Fleet,
    agent_name: str,
    *,
    limit: int = 10,
    exchange: Callable[[Fleet, str], str | None] | None = None,
) -> tuple[int, int]:
    files = sorted((fleet.root / "drills" / agent_name).glob("*.yaml"))
    if len(files) > limit:
        raise ManifestError(f"{len(files)} probes exceeds --max {limit}")
    print(f"About to spend {len(files)} real turn(s) on behavioral drills.")
    if exchange is None:
        if fleet.platform_kind != "mattermost":
            raise ManifestError(
                "real drill transport currently supports Mattermost; no synthetic fallback is used"
            )
        exchange = mattermost_exchange
    passed = 0
    for path in files:
        spec = yaml.safe_load(path.read_text()) or {}
        reply = exchange(fleet, str(spec.get("probe") or ""))
        failures = evaluate(reply, spec.get("expect") or {})
        print(
            f"  {'PASS' if not failures else 'FAIL'} {path.name}"
            + (f": {'; '.join(failures)}" if failures else "")
        )
        passed += not failures
    return passed, len(files)


def cmd_drill(ns, fleet: Fleet) -> int:
    passed, total = run_drills(fleet, ns.agent, limit=ns.max)
    print(f"{passed}/{total} drill(s) passed")
    return 0 if passed == total else 1
