#!/opt/agent/hermes-agent/venv/bin/python3
"""Push a memory bank's profile from the repository into Hindsight.

WHY THIS EXISTS. The Hermes Hindsight plugin reads `bank_mission` and
`bank_retain_mission` out of `hindsight.json` into `self._bank_mission` /
`self._bank_retain_mission` and then **uses them nowhere** — there is no Banks API
call anywhere in `plugins/memory/hindsight/__init__.py` (0.20.1), though its README
says "Applied via Banks API". So every mission this deployment had was set by hand
through the `update_bank` MCP tool, in one pass on 2026-08-14.

That went wrong in both directions at once, which is what a hand-set profile does:

  * `one agent`'s bank was created the day AFTER that pass, so it had no mission
    and no retain mission at all. It had been extracting memory unsteered ever
    since, and nothing said so.
  * the two banks that were set by hand drifted AHEAD of the repository — 841
    characters of tuned extraction prompt live against 240 in `hindsight.json`.
    Pushing the repo blindly would have destroyed the better version.

So the repository is now the source of truth, the tuned text lives in
`hindsight.json`, and this runs at every start to make the server agree.

WHAT IT WILL NOT DO. It never clears a field: a key absent from the file leaves the
live value alone. That is deliberate — a half-filled config must not silently wipe a
mission somebody tuned through the API. To remove one, do it explicitly through the
API and remove it from the file too.

Failure is never fatal. Memory is not load-bearing here (ai/_.hint#agent_memory);
an agent whose bank profile could not be updated is an agent with a stale mission,
not a broken one.

Usage: apply-memory-profile.py <hindsight.json> [<more.json> ...]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# hindsight.json key -> Banks API field. `bank_mission` lands on `reflect_mission`,
# which is what the API calls the identity/framing text; the banks LIST endpoint
# echoes the same value as `mission`, and that is one field, not two.
FIELDS = {
    "bank_mission": "reflect_mission",
    "bank_retain_mission": "retain_mission",
    "bank_observations_mission": "observations_mission",
}

TIMEOUT = 30


def request(method: str, url: str, key: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def apply_one(path: Path) -> bool:
    try:
        cfg = json.loads(path.read_text())
    except Exception as exc:
        print(f"  {path.name}: unreadable ({exc})", file=sys.stderr)
        return False

    bank = cfg.get("bank_id")
    api = (cfg.get("api_url") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not key:
        import os
        key = os.getenv("HINDSIGHT_API_KEY", "")
    wanted = {api_field: cfg[k] for k, api_field in FIELDS.items() if cfg.get(k)}
    if not (bank and api and wanted):
        return False  # nothing declared; not an error

    base = f"{api}/v1/default/banks/{bank}"
    try:
        live = (request("GET", f"{base}/export", key).get("bank") or {})
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"  {bank}: cannot read profile ({exc})", file=sys.stderr)
            return False
        live = {}  # bank not created yet -- PATCH below creates or fails harmlessly
    except Exception as exc:
        print(f"  {bank}: Hindsight unreachable ({exc})", file=sys.stderr)
        return False

    changed = {f: v for f, v in wanted.items() if (live.get(f) or "").strip() != v.strip()}
    if not changed:
        return False

    try:
        request("PATCH", base, key, changed)
    except Exception as exc:
        print(f"  {bank}: profile update failed ({exc})", file=sys.stderr)
        return False

    for field in changed:
        was = "empty" if not (live.get(field) or "").strip() else f"{len(live[field])} chars"
        print(f"  {bank}: {field} set ({was} -> {len(changed[field])} chars)")
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    for arg in argv[1:]:
        p = Path(arg)
        for path in sorted(p.glob("*.json")) if p.is_dir() else ([p] if p.is_file() else []):
            apply_one(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
