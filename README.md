# agent2you

**A team of AI agents that live in your chat, hire each other, and spend your
existing coding subscriptions — deployed from declarative manifests.**

Each agent is one docker container: [Hermes](https://github.com/NousResearch/hermes-agent)
(chat presence, sessions, memory, cron) → litellm (failover) →
[acp2api](https://github.com/neprel/acp2api) (OpenAI API over ACP) → Claude
Code / Codex / opencode as the brain, each signed into its own subscription.
By default it uses the operator's own harness logins; a documented switch moves
any chain to API billing when needed. See [subscription and API posture](docs/subscriptions.md).

## Start: hand one prompt to a coding agent

```
Run `uvx agent2you bootstrap` and follow what it prints.
```

That is the whole onboarding. The bootloader interviews you about *your*
infrastructure — local docker; Mattermost, Telegram, Slack or Discord office;
Microsoft Teams enterprise front door; Hindsight/local HINT memory or none yet;
subscriptions or a local vLLM — builds a fleet
workspace to match, walks you through the two steps no tool can automate
(platform accounts, device-code sign-ins), and verifies with a real message in
your chat.

It leaves behind a **supervisor**: the agent that owns the fleet repository,
knows who does what (the manifests are the discovery registry), and hires the
next agents — you describe a colleague in chat, it prepares everything with
`a2y agent add`, you run one command.

## What you describe, what you get

```yaml
# agents/acme-pm/agent.yaml — an agent is this file plus a SOUL.md
name: acme-pm
description: Project manager for acme — specs, tasks, sequencing. Writes no code.
brains:
  chain: [claude, codex, local]        # any length, any order, per agent
  executors:
    claude: {kind: claude, model: opus, account: main-anthropic}
    codex:  {kind: codex, account: main-openai}
    local:  {kind: openai, base_url: "http://vllm:8000/v1", model: ai01, api_key_env: VLLM_KEY}
access: {github_token: true}
memory: {projects: [acme]}
toolkits: [go]                         # a pinned install + usage instructions, one unit
```

`a2y render` turns manifests into a committable `deploy/` tree — same input,
same bytes, reviewed in git. The stack shrinks to fit: one brain → no litellm;
a bare endpoint brain → no acp2api either.

Out of the box, because it was all paid for once already:

- **one coding-agent session per chat thread** (no cold start per message),
  live progress in the post while a turn runs, `/steer` into the running turn;
- **routing without a classifier** — untagged messages claimed by exactly one
  agent; agents need explicit @mentions from each other (the loop guard);
- **memory in tiers**: a private HINT repository or Hindsight bank per agent,
  plus reviewable git/HINT project wikis;
- **cost visibility**: tokens per agent / executor / account in Prometheus,
  one trace project per agent in Phoenix;
- **the traps pre-sprung**: device-code-only logins, `CLAUDE_CONFIG_DIR`,
  codex sandbox mode, cline self-update, Hermes self-update refusal, pinned
  everything.

## Commands

| | |
| --- | --- |
| `a2y bootstrap` | print the bootloader prompt |
| `a2y init <dir>` | create a fleet workspace |
| `a2y agent add <name> …` | add a colleague (non-interactive — built to be called by your supervisor) |
| `a2y agent remove <name>` | remove a colleague; keep state unless explicitly purged |
| `a2y render` | manifests → `deploy/` |
| `a2y upgrade` | three-way refresh the pack-owned image tree |
| `a2y backup` / `restore` / `rebuild` | protect state and safely re-base an agent |
| `a2y build --parallel N` / `up` / `down` | image chain, containers |
| `a2y provision` / `auth` | platform accounts, brain sign-ins |
| `a2y doctor` | end-to-end checks, including `.env` completeness |

## Docs

- [architecture](docs/architecture.md) — the stack and why each layer exists
- [provisioning](docs/provisioning.md) — accounts, tokens, sign-ins, keys
- [hiring](docs/hiring.md) — the interview a supervisor runs to add a colleague
- `a2y duties templates`, `a2y drill`, `a2y rotate`, and network-only
  `a2y outdated` cover recurring posts and fleet maintenance.
- [extending](docs/extending.md) — toolkits, new platforms, new brains
- [operations](docs/operations.md) — backup, removal, rebuild, rotation, VPNs
- [subscriptions](docs/subscriptions.md) — operator-owned logins and API fallback
- [apprentice](docs/apprentice.md) — observation, proposals and graduated autonomy

## License

MIT
