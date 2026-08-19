# agent2you

Deploy a **team of chat-resident AI agents** from declarative manifests: each
agent is one container holding Hermes (the employee), litellm (the failover
chain), acp2api (an OpenAI endpoint over ACP) and the subscription coding CLIs
(Claude Code, Codex, opencode, cline) that are its brain and hands.

The design was extracted from a production fleet that runs infrastructure,
product and PM agents as colleagues in Mattermost channels. Every decision in
this pack — one container per agent, no provider API keys, chat as the only
inter-agent surface, generated-not-hand-written configs — was measured there
first.

## What an agent is

```
   chat platform (Mattermost / Telegram / Slack / ...)
        │  @mention
   ┌────┴─────────────────────────────────────────┐
   │  hermes      presence, sessions, memory, cron │
   │  litellm     failover: next brain on 429      │   one
   │  acp2api     OpenAI API ⇄ ACP, per-thread     │   container
   │              session continuity               │   per agent
   │  claude / codex / opencode / cline            │
   │              each spends its own SUBSCRIPTION │
   └───────────────────────────────────────────────┘
```

No provider API key exists anywhere in a default deployment: the CLIs log in to
their own subscriptions, which is the legal basis of the arrangement — and the
reason nothing here can quietly bill an API account.

Nothing is shared between agents except memory (optional, bank-per-agent) and
the chat workspace. Two agents cannot interfere with each other by construction.

## Quickstart

```bash
uv tool install /path/to/agent2you     # or: pipx install /path/to/agent2you

a2y init myfleet && cd myfleet          # a self-contained fleet workspace
$EDITOR fleet.yaml agents/ana/          # who exists, what platform, what memory
a2y render                              # manifests -> deploy/  (commit it)
a2y build                               # the agent image, pinned end to end
cp deploy/example.env deploy/.env       # secrets; a2y doctor checks parity
$EDITOR deploy/.env
a2y provision                           # prints the messenger account sequence
a2y up
a2y auth ana                            # sign the brains in (once; survives rebuilds)
a2y doctor                              # end-to-end checks
```

Adding a colleague later is: `mkdir agents/<name>`, write `agent.yaml` +
`SOUL.md`, `a2y render`, add its two secrets to `.env`, `a2y up <name>` — and
recreate the others only because the platform allowlist is container
environment. Running agents discover the newcomer without a restart: the fleet
roster in every SOUL.md regenerates on a loop from the mounted manifests.

## The manifests

`fleet.yaml` — deployment-level facts: platform (`mattermost` fully wired;
other Hermes platforms pass their env through), memory (`hindsight` or `none`),
network (`bridge` by default; shared VPN namespace as an advanced mode),
observability (Phoenix traces, Prometheus token metrics), image tag, and
`defaults` merged into every agent.

`agents/<name>/agent.yaml` — the agent: description (its card, roster entry and
the reason colleagues call it), brain chain and executors, access (ssh volume,
GitHub token), memory banks, extra MCP servers.

`agents/<name>/SOUL.md` — the persona. `SOUL-shared.md` is appended to everyone.

`a2y render` turns those into `deploy/`: per-agent Hermes / litellm / acp2api /
hindsight configs, a compose file, and an `example.env` naming every variable
the deployment needs. The output is deterministic — same manifests, same bytes —
so the deploy tree is reviewable and belongs in git. Anything the generator
does not expose: `agents/<name>/overrides/<file>` replaces the generated file,
and `hermes:`/`acp2api:` keys in agent.yaml deep-merge into those configs.

## What the pack takes care of

- **Session continuity per chat thread** — the `conversation-key` plugin +
  litellm header forwarding + acp2api's conversation keying, so a thread keeps
  one coding-agent session instead of cold-starting per message.
- **A visible working turn** — progress narration into the chat post while the
  agent works, the trace tucked behind the post's info card when it finishes,
  and mid-turn steering (`/steer`) delivered INTO the running turn.
- **Fleet discovery without restarts** — a roster generated from every agent's
  own manifest, appended to each SOUL.md on a loop.
- **Routing without a classifier** — untagged messages claimed by exactly one
  agent from Mattermost facts alone; agent-to-agent messages always need an
  explicit @mention, which is the loop guard.
- **Memory in tiers** (optional) — a private Hindsight bank per agent written
  automatically, shared project banks written deliberately through tools, and
  bank missions pushed from the repository at every start.
- **Cost visibility** — acp2api's Prometheus metrics (tokens per agent, per
  executor, per account) and one Phoenix trace project per agent.
- **The traps already sprung** — device-code-only logins, `CLAUDE_CONFIG_DIR`,
  codex's sandbox mode, cline's self-update, the CA store, the tty-guarded
  bashrc, healthchecks that mean something. They are encoded, not documented.

## Docs

- [docs/architecture.md](docs/architecture.md) — the stack and every decision in it
- [docs/provisioning.md](docs/provisioning.md) — accounts, tokens, sign-ins, keys
- [docs/extending.md](docs/extending.md) — custom tools, platforms, derived images

## Status

v0.1. Extracted from a running deployment; the mattermost + hindsight +
claude/codex path is the proven one. Telegram/Slack/Discord pass through to
Hermes' own adapters and are not yet exercised end to end by the maintainers.
