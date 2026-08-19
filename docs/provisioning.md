# Provisioning an agent

Everything an agent needs can be provisioned headlessly EXCEPT its brain — that
one step needs a human at a browser, once, and the login then lives in a volume
that survives every rebuild.

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
