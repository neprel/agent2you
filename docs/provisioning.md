# Provisioning an agent

Everything an agent needs can be provisioned headlessly EXCEPT its brain — that
one step needs a human at a browser, once, and the login then lives in a volume
that survives every rebuild.

For Telegram, Slack and Discord, `a2y provision <agent>` prints the exact
one-bot-per-agent setup and `.env` names. Telegram requires Bot-to-Bot Mode and
privacy/admin configuration; Slack requires Socket Mode (`xoxb` + `xapp`) and
message events; Discord requires Message Content and Members intents. Missing
either portal-side permission is a silent no-delivery failure.

Teams is a separate front-door role: its adapter listens on port 3978 at
`/api/messages`, and Azure Bot Framework must reach it over public HTTPS. There
is no outbound-only transport equivalent to Slack Socket Mode. The workspace's
`platforms/teams/manifest.json` is the Teams app-package template; `a2y
provision` gives the Entra app, Azure Bot, tenant/user allowlist and reverse
proxy sequence.

An auxiliary `channels.email` uses Hermes' built-in IMAP/SMTP adapter. Use a
dedicated mailbox and app password, always set `allowed_users`, and never enable
allow-all. Email auto-reply loops and prompt injection are why this pack treats
email as reports/approval delivery rather than an autonomous primary office.

The order that works:

1. `a2y render && a2y build`
2. messenger account + token → `.env`
3. `a2y up <agent>`
4. `a2y auth <agent>` — sign the brains in
5. git/forge credentials, host access — only for agents that need them
6. verify with a real turn in the channel, not with probes

## 1. Messenger account (Mattermost)

`a2y provision <agent>` prints the exact sequence. The facts behind it:

- Agents are ordinary `system_user` accounts with **personal access tokens**,
  NOT bot accounts — Mattermost refuses bots several things (creating incoming
  webhooks, for one), and a colleague should read as a colleague.
- Token creation needs the `system_user_access_token` role first.
- **A token's secret is returned only at creation.** A re-run must revoke and
  re-issue, not report a token it cannot read.
- REST login: `login_id` must be the **username** — an email answers 401 that
  reads like a wrong password.
- Membership POSTs are no-ops when already present, so the whole sequence is
  safe to repeat after a partial failure.

Then the fleet-level lists in `.env`:

- `A2Y_MATTERMOST_ALLOWED_USERS` — every human and every agent **user id**.
  A sender not on this list is dropped before mention detection, silently;
  forget one and delegation fails with nothing in any log. Changing it
  **recreates the other agents** (it is container environment).
- `A2Y_MATTERMOST_HUMANS` — the subset that are people. Only a human may wake
  an agent without naming it.
- `A2Y_ROOM_OWNERS` — `room:agent` map for untagged messages, e.g.
  `default:ana,project-x:pm`.
- Boards/Playbooks bot mentions arrive as DMs from those bots' user ids — add
  them to the allowlist if agents should hear card mentions, and know that a
  busy board then wakes the agent per notification.

Leave `MATTERMOST_ALLOWED_CHANNELS` empty. Membership already fences delivery;
the extra list buys a silent drop of group DMs (`G` is a channel to Hermes, not
a DM — only one-to-one `D` bypasses the whitelist).

Other platforms: create the bot/account per that platform's docs and put the
adapter's variables into `platform.env` in fleet.yaml; the entrypoint passes
`TELEGRAM_*` / `SLACK_*` / `DISCORD_*` through to Hermes.

### Voice notes

Inbound chat voice notes work with no manifest setting: Hermes defaults to
local faster-whisper `base`, and ffmpeg is baked into the image. Configure only
when the default is insufficient:

```yaml
voice:
  enabled: true
  provider: local
  language: ru
  model: large-v3-turbo
  tts: true                 # optional replies via free Edge TTS
```

`base` is weak for Russian. Local `large-v3-turbo` is the inexpensive upgrade
(roughly 1.5 GB RAM and around realtime on CPU). Cloud providers `groq`,
`openai`, `mistral`, `xai`, `elevenlabs`, and `deepinfra` are supported; render
adds the exact provider key to `example.env`. OpenAI voice uses
`VOICE_TOOLS_OPENAI_KEY`, never `OPENAI_API_KEY`, so it cannot collide with the
fleet's brain-auth invariant.

For higher RU+EN CPU throughput, operate a sherpa-onnx or Speaches sidecar with
Parakeet-TDT-0.6B-v3 (CC-BY, about 2 GB RAM). GigaAM v3 (MIT) offers a higher
Russian ceiling. Connect either through Hermes `stt.providers` or
`HERMES_LOCAL_STT_COMMAND` using an agent override; the pack does not render a
sidecar until a real fleet needs that branch.

Hermes STT is for short voice notes. Attach long recordings to the one agent
carrying `toolkits: [transcribe]`; its instructions produce a transcript file,
thread summary and action items. Telegram bots can download only files around
20 MB through `getFile`, so Mattermost or a workspace file is the reliable path
for meetings. Recording consent and legal compliance are the deploying
recorder's responsibility.

After building a transcribe agent, run `a2y models pull <agent>` once. With no
token it pulls the Whisper weights and selects the valid fallback diarization
tier; until then transcription alone is unavailable and the rest of the agent
starts normally. To enable the higher-quality pyannote `community-1` tier:

1. Create a free account at huggingface.co.
2. Open `huggingface.co/pyannote/speaker-diarization-community-1` and accept its conditions; a token alone is not enough.
3. Open Settings, then Access Tokens, and create a token with READ access.
4. Put the token in `deploy/.env` as `HF_TOKEN=...`.
5. Run `a2y models pull <agent>` for the agent that transcribes recordings.

The host-side pull command uses the token only for the gated download; it never
enters the image, build arguments, Compose, runtime environment, or store
manifest. Models land in `volumes/models/`, their actual revision and hashes
are recorded, and the pinned toolkit runtime loads them once offline before the
store is accepted. Without a token the equally valid `fallback` tier is pulled
and one informational upgrade pointer is printed.

## 2. Brains (the step nobody can automate)

`a2y auth <agent>` prints per-agent instructions. The rules that matter:

- **Device-code flows only.** Browser-callback logins listen on the container's
  own `127.0.0.1`; your browser's localhost is a different machine, so the
  callback can never arrive. `claude` → `/login`; `codex login --device-auth`.
- A fresh container answers its first turn with `500 Authentication required`
  from acp2api. Everything else looks healthy — the healthcheck is litellm's
  liveness, Hermes connects fine — so recognise this failure by sight.
- A TLS error from a non-Node tool ("error sending request for url …") with
  Node tools working fine means a missing **system CA store**, not a network
  fault. The image installs `ca-certificates` for exactly this; check
  `/etc/ssl/certs/ca-certificates.crt` before suspecting the network.
- `OPENAI_API_KEY` inside the container is the **litellm master key**, never a
  real provider key. `codex doctor` warns about "mixed auth signals" — expected.
  If a real OpenAI key ever lands there, codex may quietly bill the API instead
  of spending the subscription, which is the one thing this arrangement exists
  to avoid.
- Verify continuity from the **logs** (`continuing session keyed …`), not by
  asking the agent to recall something — recall may come from memory, and a
  shell variable does not survive between CLI invocations.

## 3. Git and forges

- `access.ssh: true` mounts a key volume; the entrypoint generates a dedicated
  **git deploy key** (`id_git_ed25519`, separate from any host-access key so the
  two revoke independently) and prints the public half to register.
- `access.github_token: true` expects `AGENT_<NAME>_GH_TOKEN` — a fine-grained
  PAT scoped to exactly the repositories the agent owns. The entrypoint rewrites
  ALL github remote forms (https, ssh://, git@) onto the token, because
  dependency manifests hardcode `git+ssh://` and `yarn install` would otherwise
  demand a key the agent deliberately does not have.
- Gitea: `tea login add` with a hand-made token carrying only the scopes needed.

## 4. Host access (rare, deliberate)

An agent that must reach a host over ssh gets its **own account there**, created
by a reviewable script kept in the fleet repo — full sudo buys attribution (the
journal separates the agent's actions from the operator's), and the grant is
readable in git rather than buried in one machine's `/etc`.

Two traps, both paid for already:

- Before adding the agent to a human's group, `chmod 700` that human's
  `~/.ssh`: sshd tolerates a group-writable `.ssh` only while the group has one
  member, and the new membership is the second member — instant key lockout.
- The agent's own account with sudo is your way back in when that happens.

## 5. Memory

- The pack pushes bank missions from `hindsight.json` at every start; the
  Hermes plugin alone would apply nothing. Before editing a mission in the
  repo, check whether the live bank has drifted **ahead** (tuned via API) —
  pull that text into the repo first, or the push destroys the better version.
- Hindsight needs a Postgres with **pgvector in the `public` schema**, a role
  whose `search_path` ends in `, public`, and `GRANT CREATE ON DATABASE`.
  Wrong schema placement surfaces as a migration loop; a missing trailing
  `public` serves reads fine and fails **every write** silently.
- A retain mission steers *shape* reliably and *prohibits topics* unreliably on
  a small extraction model — treat banks as needing a periodic sweep
  (invalidate wrong facts; consolidation re-derives, it does not purge).

## 6. Verify

Mention the agent and give it a real task that runs a shell command. Check:

- the answer arrives in-thread with the trace behind the info card;
- no `bwrap:` line in the trace (codex sandbox mode is set per session by the
  manifest, not by config.toml);
- second message in the thread logs `continuing session`;
- `a2y doctor` is green.

"The API returned 200" is not verification, and an agent's own report that it
posted something is a report that it pressed send. When two accounts of a
message disagree, read the channel through the platform API before believing
either.
