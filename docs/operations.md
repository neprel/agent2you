# Operations

## State, backup and migration

Each `volumes/agent-<name>/` holds OAuth credentials (`claude`, `codex`,
`opencode`, `cline`), forge credentials (`gh`, `tea`, optional `ssh`), Hermes
sessions, the working checkout, and local memory. `deploy/.env` and
`volumes/*/hermes/.env` contain live secrets on the host; use encrypted disks
and restrictive home-directory permissions.

For a browser agent, the same volume also contains `browser/profile`: live web
cookies and account sessions with credential-level sensitivity. It is included
in cold backups automatically. A headed Chromium workload commonly needs 1–2
GB at peak, so size that agent's `resources.memory` separately from the fleet.

Run `a2y backup <agent> --cold` on a cadence appropriate to the work. The
0600 archive contains live credentials and excludes `workspace/` by default;
add `--include-work` for irreplaceable uncommitted work. For migration: copy the
fleet git repository, copy encrypted archives, run `a2y restore`, then `a2y up`.
Hindsight is server-side and needs its own database backup.

`a2y agent remove NAME` removes identity and generated files but parks the
volume. `--yes --purge-volumes` is the explicit irreversible path. Revoke the
platform account/token and deploy key and remove its `.env`/allowlist/room-owner
entries. Rename is intentionally remove + add + `restore --agent NEW`; remote
memory and observability ids do not follow the name.

## Rebuild and continuity

`a2y rebuild NAME` stops the agent, takes a snapshot, builds, recreates only that
service and runs the shared offline verification checks. `--all` is rolling and
stops on the first failure. Follow with online `a2y doctor --probe-brains` and a
real mention: process health is not proof that a login or conversational path
works. Volume mount paths are a compatibility contract; pin bumps must retain or
migrate them and must confirm Hermes session and CLI credential compatibility.

## Reading doctor output

Doctor is read-only. It renders into a temporary directory, checks `.env`
parity, pack/image versions, known credential expiry fields, git tracking and
permissions, then probes the platform with a three-second timeout. Use
`--offline` in CI. A Mattermost token absent from `A2Y_MATTERMOST_ALLOWED_USERS`
is a real failure: colleagues otherwise disappear silently.

The version check is three-way: installed `a2y`, the workspace `.a2y-version`,
and `org.agent2you.version` on every base/derived image used by the fleet. A
skew means the generated deployment and executable may disagree; run `a2y
upgrade`, rerender and rebuild instead of suppressing it. Offline doctor checks
the first two and explicitly reports the image probe as skipped.

For agents carrying `browser`, online doctor also launches headed Chromium,
checks the Playwright MCP executable and persistent profile, and probes noVNC
when enabled. A successful HTTP probe does not make noVNC safe to publish: keep
the generated loopback bind and reach it through SSH forwarding.

Model-bearing toolkits use the shared `volumes/models/` store. `a2y models
pull` is the only writer; containers mount it at `/models:ro`. Doctor displays
the recorded tier, revision, pull time and verifies the recorded file hashes.
An absent store is informational—the rest of the agent starts normally—and
names the exact pull command that enables the capability.

## Rotation

| secret | rotate | verify |
| --- | --- | --- |
| LiteLLM master key | edit `.env`, `a2y up` | compose + real turn |
| platform token | revoke/reissue, edit `.env`, recreate | doctor platform probe |
| GitHub/tea token | reissue, edit `.env`, recreate | authenticated forge command |
| Claude/Codex OAuth | `a2y auth <agent>` | real turn / brain probe |
| SSH deploy key | backup, remove its `ssh/` key, recreate, register new public key, revoke old | `ssh -T` |
| Hindsight key | rotate server and `.env`, recreate | health/recall probe |

Never merely remove a committed secret: `git rm --cached deploy/.env`, then
rotate everything it ever contained.

`a2y rotate litellm-keys [AGENT...]` (or `--all-internal`) performs the internal
case, recreates affected services, sends one authenticated probe turn per agent,
and runs doctor. Platform, forge and SSH
classes stop at their provider-owned human step with the exact env/path name;
this is intentional, and the command lists what it did not rotate.

## Fleet maintenance

`a2y outdated --json` is read-only but explicitly uses the network: it compares
the installed and stamped pack with PyPI and selected vendored image pins with
their recorded PyPI/npm sources. It never runs in render or doctor. A weekly
gardener duty may turn that report into an idempotent proposal; it must never
edit pins, build or start anything. Proposals point to `a2y upgrade`, the
rolling-rebuild procedure above, and the `_.hint` `{#pins}` coupling ritual.

Use `a2y drill AGENT` after SOUL or procedure edits. It prints the real-turn
count before sending and evaluates only deterministic contracts (`refuses`,
`mentions`, `answers`, `silent`, `contains`, `not_contains`). Mattermost needs
an ordinary dedicated drill user token and channel id as `A2Y_DRILL_TOKEN` and
`A2Y_DRILLS_CHANNEL`; no synthetic transport is substituted for evidence.

## Hosts, resources and networks

Run `a2y` on the Docker daemon host. Remote Docker contexts are refused because
relative bind mounts resolve on the daemon filesystem; the escape flag exists
only for operators who have arranged identical mounts. Docker Desktop macOS is
supported for bridge mode (VirtioFS may make bind-mount I/O slower); shared/VPN
network namespaces are Linux-only.

For an external `container:<vpn>`, recreating the VPN container orphans every
joined namespace: run `a2y up` to recreate agents, not `restart`. Prefer an
in-project `network.vpn_service`, which renders `service:` mode and health-based
ordering. Resource limits are optional via `defaults.resources` or per-agent
`resources`; size them from observed idle and peak session usage, not guesses.

## Changing an agent's mind

SOULs, toolkit usage, procedures, `.hint` knowledge and project wikis are the
reviewable identity plane: change and commit them. For `memory.kind: local`, use
`a2y knowledge remember` and `a2y knowledge retract`; retraction writes a factual
HINT `supersedes` tombstone so old capture cannot silently resurrect a deleted
fact. Hindsight curation uses its bank API. Versioned `memory.briefings` entries
in agent.yaml are replayed into the bank mission.
