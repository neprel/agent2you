# Architecture

Everything here was learned on a running fleet before it became a rule. Where a
decision looks redundant, ask what each part refuses to do — that is usually the
reason it exists.

## One agent is one container

An agent is **Hermes + litellm + acp2api + the coding CLIs**, built into one
image and instantiated once per agent. Read the stack bottom-up, because that is
the order the constraints appear in:

| layer | what it is | why it cannot move |
| --- | --- | --- |
| the CLIs | the brain and the hands | each spends a **subscription** through its own login; no API key exists, which is the legal basis, not an optimisation |
| acp2api | one OpenAI endpoint per CLI, over ACP | it **spawns** the CLI as a child process; a process cannot be spawned into another container |
| litellm | the failover chain | the only place that can decide to try the next brain when the first answers 429 |
| Hermes | the employee: chat, sessions, memory, cron | it speaks OpenAI and nothing else |

acp2api will not fail over (one process, one CLI). litellm will not run a tool
loop. Hermes will not speak ACP. Nothing can be removed without losing the
property that was the reason to build it. Do not "clean this up" into shared
services: the isolation — own CLIs, own logins, own workspace, own quota — is
what makes it safe to give an agent a chat account and a shell.

The **image bakes everything in and pins every version**; the logins live in
volumes so a rebuild never signs an agent out; the **identity (config, SOUL) is
copied from the deploy tree on every start**, overwriting whatever the volume
holds — an identity that drifts inside a volume is one nobody can review.
Deploy = re-render + restart.

The agents run as **root inside their container**. A prompt injection is a
container compromise; the accepted boundary is the container plus `/work`.
Never mount the operator's checkout or the docker socket into an agent.

## Manifests → render → deploy

Configs are **generated, not hand-written**. The production fleet this comes
from needed six hand-kept files per agent plus a compose block, an env block, a
make target and volume markers — and every addition was a chance to miss one.
Here the manifest is the only input; `a2y render` emits the same bytes for the
same manifests, so drift is a `git diff` and review happens on the tree that
actually runs.

Escape hatches, in order of preference: `hermes:` / `acp2api:` deep-merge keys
in agent.yaml; `agents/<name>/overrides/<file>` replacing a generated file
verbatim; and editing the vendored `image/` (it is yours).

## Networking: bridge by default

Default mode gives the fleet one docker network. Every container keeps its own
loopback, so **every agent uses the same internal ports** and there is nothing
to allocate, collide, or account for. acp2api and litellm stay loopback-bound —
they are unauthenticated by design (authorization is litellm's `master_key`,
inside the container) and must never be reachable from outside.

`network.mode: container:<vpn>` is the advanced mode for "every agent egresses
through this tunnel": all agents join one namespace, which brings back per-agent
port blocks (`ports.base`), the resolv.conf repair, and the interface assertion
(fail closed rather than egress on the host's address). Use it only when the
tunnel is the point.

A2A agent cards bind loopback. In the shared namespace that loopback IS the
fleet network, so cards are cross-readable; in bridge mode they are not — and
that costs nothing, because **discovery is the roster and conversation is the
chat platform**, deliberately: agents that confer where nobody is watching
cannot be corrected, and every turn spends a real subscription.

## The chat platform is an option, not a foundation

Hermes carries the platform adapters; the pack only decides which variables
reach it and which plugins load. `platform.kind: mattermost` is the fully wired
path: account provisioning docs, Boards/Playbooks MCP sidecars, and five
plugins that close real gaps (mention-on-edit, untagged-routing, trace-to-card,
reasoning-live, steer-into-turn). Other kinds pass `platform.env` through to
Hermes' own adapter — the untagged-routing policy is already split from its
Mattermost binding (`policy.py` names no chat product), so a second platform
adds a binding, not a second copy of the rules.

Fleet-talk rules that keep a fleet safe regardless of platform:

- **Inbound is allowlisted by sender id, one list for the whole fleet.** A
  sender not on it is dropped before anything looks at the message — silently.
  Changing the list is the one thing that still recreates containers.
- **Agent-to-agent always requires an explicit mention; only humans can wake an
  agent untagged.** That is the loop guard: two agents can never talk each
  other into an unbounded exchange.
- **Delivery ends with the requester's mention; courtesy does not.** A asks, B
  delivers with an @, A reports and stops. Thanking with a mention is how loops
  start; reporting without one is how delegations deadlock.

## Sessions and continuity

Hermes is stateless on the wire — it resends the whole transcript every turn.
Three pieces keep one coding-agent session per chat thread, and removing any of
them silently returns the fleet to a cold agent per message:

1. the `conversation-key` plugin puts Hermes' session id on the wire as
   `x-conversation-id`;
2. litellm's `forward_client_headers_to_llm_api: true` lets it through;
3. acp2api keys the session by it (prefix matching as fallback).

acp2api then bounds sessions three different ways — `maxSessions` (resident
processes), `sessionTtlMs` (parks a thread, `session/resume` brings it back with
everything read still in place), `maxContextFill` (retires a session before the
context window does it the hard way). Three questions, three bounds; none
substitutes for another.

`warmup` stays **off**: a forked session's MCP servers are dead (measured), and
an agent that cannot recall anything is worth less than a warm start.

## Tools reach the brain inside the session

MCP servers belong in **acp2api.yaml** — they live inside the ACP session and
the agent calls them in its own loop. Tools routed through Hermes' `tools`
array also work, but every call ends the completion and round-trips through
Hermes; that route is worth it only for what Hermes alone has (skills, session
search, memory). The pack therefore disables Hermes' duplicate toolsets
(terminal, file, todo, code_execution) — the hands are the CLI's own.

Do not prune MCP servers to save context: both major CLIs defer tool schemas
and fetch them on use (measured at ~14 tokens per tool on claude, zero on
codex). Prune for blast radius, not for tokens. And never test availability by
asking the model to *list* its tools — enumeration is narration; invocation is
evidence.

## Memory: banks are the isolation boundary

Three tiers, when `memory.kind: hindsight`:

| tier | where | how |
| --- | --- | --- |
| short-term | the channel session | Hermes' own |
| long-term personal | one bank per agent | recalled automatically before each turn, retained automatically after |
| long-term shared | a project bank | MCP tools only — a bank several agents read is written on purpose, not as a side effect |

Bank **missions are pushed from the repository at every start**
(`apply-memory-profile.py`): the Hermes plugin reads them from config and never
applies them itself, so without the push a bank extracts unsteered and nothing
says so. The push never clears a field it does not carry.

Memory is deliberately **not load-bearing**: an agent whose memory server is
down loses recall and keeps everything else. Its extraction LLM must never be a
coding CLI — that would spawn a Claude Code process to summarise a chat message
out of a paid subscription. Point Hindsight at a small local model.

Do not treat memory as a source of truth about live state: it records what was
said, imperfectly reconciled. For what is true, the agents have a shell.

## Observability

- **Prometheus** (`observability.prometheus`): acp2api exports
  `acp2api_tokens_total{agent,executor,account,kind}` and friends — spend per
  turn, settled, for every executor at once. `account` is declared per executor
  in the manifest, because no protocol message reports which subscription pays.
- **Phoenix** (`observability.phoenix_url`): one trace per turn, one project per
  agent, prompts included — which is why a Phoenix should never be exposed
  beyond the agents themselves.
- Phoenix answers *what the turn did*; Prometheus answers *what it cost*. Token
  counts are not in the spans; do not go looking for that setting.

## Supply chain posture

Every version is an ARG in the dockerfile; binaries are checksum-verified;
`CLINE_NO_AUTO_UPDATE=1` is set because cline otherwise reinstalls itself from
`latest` at startup, defeating the pin inside a container that holds chat
tokens, memory keys and — worth more than either — the CLIs' OAuth logins.
Hermes installs from its own installer tracking `main` (pass `--commit` in the
vendored dockerfile when reproducibility matters more than currency). Version
pins verify a number, not an artifact; treat the pins as the cheap half of the
defence and the login volumes as the thing actually worth stealing.
