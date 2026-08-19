# Hiring a new agent — the interview an assistant runs

`a2y agent add` is deliberately **non-interactive**: the interactive layer is a
fleet agent (the assistant) interviewing the operator in chat, then calling the
tool once with the answers. The conversation stays where conversations belong;
the tool stays deterministic and auditable.

Give your assistant access to the fleet workspace checkout and put a pointer to
this file in its SOUL.md. The flow below is written to be followed by an agent.

## The interview

Ask only what the manifest needs; propose defaults and confirm rather than
open-ended-ask. One question per message works best in chat.

1. **Name** — lowercase, `[a-z0-9-]`, short (`acme-pm`, `ops-sre`). It is
   the container name, the env prefix, and the memory bank id.
2. **Responsibility** — one or two sentences: what it owns, what it answers
   for. This becomes `description`: the roster entry, the Agent Card, and the
   reason colleagues route to it. Push for precision — "helps with the project"
   routes nothing.
3. **Brains** — default: the fleet's `defaults.brains` (say what that is).
   Deviate only with a reason: e.g. engineers on codex while the manager runs
   claude splits two subscriptions along the same line as the work; a metrics
   agent on a cheaper model spends fewer of the shared turns.
4. **Repositories** — does it own repos? Which forge?
   - GitHub with full ownership of named repos → `--github-token` (a
     fine-grained PAT scoped to exactly those repos).
   - Pushing over ssh / other forges → `--ssh` (the entrypoint generates a
     deploy key to register).
   - Answers-only agents get neither.
5. **Host access** — should it reach machines over ssh? (`--ssh`, plus the
   reviewable host-account script — see provisioning.md §4.) Most agents
   should not; an investigator that reads metrics needs no shell, and the
   split keeps the one with a shell honest.
6. **Shared memory** — which project banks does it join? (`--projects a,b`;
   a bank is created on first write, its profile lives in `banks/*.json`.)
6a. **Special tools** — does the work need anything beyond the base image
   (a language toolchain, a CLI, a vendor SDK)? Each becomes a toolkit:
   `toolkits/<name>/toolkit.yaml` (the pinned install) + `USAGE.md` (how to
   use it — this text lands in the agent's SOUL.md), attached with
   `--toolkits <name>`. Draft the USAGE.md the same way as the soul: show it,
   get an ok.
7. **Where it lives** — which channel is home, and does it own the room's
   untagged messages? (home channel goes to `.env`; room ownership is an edit
   to `A2Y_ROOM_OWNERS`.)
8. **Soul** — draft SOUL.md from the answers: identity, scope, non-goals, and
   only the rules specific to this agent. Keep it SHORT and in complete
   imperative sentences — fleet-wide conduct already arrives via
   SOUL-shared.md, tool instructions via toolkit USAGE, operational knowledge
   via .hint files. Every soul line is paid on every turn, and long personas
   measurably fight themselves (more reasoning tokens, not better behavior).
   Show the draft, get an edit or an ok.

## The call

```bash
cd <fleet-workspace>
a2y agent add acme-pm \
  --description "Project manager for acme: specs, task lists, sequencing, the board. Writes no product code." \
  --github-token \
  --projects acme \
  --soul-file - <<'SOUL'
# acme-pm
...the draft the operator approved...
SOUL
```

Or build the whole manifest as JSON and pass `--json -` (flags win over json
keys) — easier when the answers were collected structurally.

The command validates against the whole fleet and **rolls back on failure** —
a bad call leaves no half-created agent. On success it re-renders `deploy/` and
prints the numbered next-steps checklist (env keys, provisioning, allowlist
recreate, sign-in). Relay that checklist to the operator verbatim; the steps
that need a human are the messenger token and the brain sign-in.

## What the assistant must NOT do

- Invent env secrets or write to `.env` — name the keys, let the operator fill
  them.
- Skip the allowlist step. A new colleague whose id is missing from
  `A2Y_MATTERMOST_ALLOWED_USERS` is dropped in silence, and the failure will be
  misdiagnosed as anything but this.
- Report the agent as ready before a real mention in a real channel answered.
  "The container is healthy" means litellm is alive, nothing more.
