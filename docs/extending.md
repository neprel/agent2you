# Extending the pack

The pack is a floor, not a ceiling. Extension points in increasing order of
commitment:

## Per-agent knobs (no new files)

- `defaults:` in fleet.yaml deep-merges into every agent; the agent's own keys
  win. Keep the common brain chain there.
- `acp2api:` in agent.yaml deep-merges into the generated server block
  (timeouts, session bounds, `busy`, …).
- `hermes:` in agent.yaml deep-merges into the generated Hermes config
  (`agent.max_turns`, display, extra plugin names, …).
- `mcp:` appends MCP servers verbatim to every executor's session. Prefer this
  route for tools of the WORK — they run inside the agent's own loop. Two traps
  encoded in the defaults: never set `env:` on a stdio server unless you mean
  to REPLACE the child's whole environment (node falls off PATH), and pass URLs
  as flags when a package reads several env aliases.
- `env:` in agent.yaml / `platform.env` in fleet.yaml add container environment
  verbatim (values may reference `.env` variables as `${VAR}`).

## Overrides (replace one generated file)

`agents/<name>/overrides/<filename>` ships verbatim instead of the generated
`config.yaml`, `acp2api.yaml`, `litellm.yaml`, `hindsight.json`, `agent.yaml`
or `SOUL.md`. The file is still copied into `deploy/`, so review stays in one
place. Use it for a shape the generator cannot express yet; when the need
repeats, teach the generator instead.

## Toolkits: a tool and its instructions, as one unit

The primary way to give agents extra tools. A toolkit is a directory in the
fleet workspace bundling the INSTALL recipe with the USAGE instructions —
because a tool nobody was told how to use is a tool that gets misused:

```
toolkits/go/
  toolkit.yaml     # apt: / npm: / uv_tools: / env: / dockerfile: (verbatim)
  USAGE.md         # appended to the SOUL.md of every agent that carries it
```

```yaml
# toolkits/go/toolkit.yaml — pin versions, keep the base image's posture
apt: [golang-1.26]
npm: ["some-linter@1.2.3"]
uv_tools: ["some-tool==2.0"]
env: {GOFLAGS: -mod=readonly}
dockerfile: |
  RUN curl -fsSL ... && sha256sum -c ...   # anything the sugar keys can't say
```

Attach it in the manifests:

- `image.toolkits: [go]` in fleet.yaml — baked into the **fleet image**, every
  agent gets it;
- `toolkits: [go]` in an agent.yaml (or `a2y agent add ... --toolkits go`) —
  that agent runs a **derived image** (`<tag>-<name>`), built automatically by
  `a2y build` from the generated `deploy/build/agent-<name>.dockerfile`.

Either way the toolkit's USAGE.md lands in the agent's rendered SOUL.md under
`## Toolkit: <name>`, so install and instructions cannot drift apart. The
default set in the base image (git, gh, tea, openspec, spec-kit, hint, …) is
just the floor — a fleet that wants a leaner base edits the vendored
dockerfile and re-adds what it needs as toolkits.

## The image (vendored, yours)

`a2y init` copies `image/` into the fleet workspace. For a tool one fleet or
one agent needs, prefer a toolkit (above); edit the image itself for pins and
for changing the default set:

- **bump a pin**: change the ARG. Version pins are the reason two agents built
  a month apart run the same software; keep them honest.
- A toolchain worth copying from another image: copy a self-contained prefix
  (like `/usr/local/go`), never a whole `/usr/local` over an existing one, and
  check for symlinks that point outside what you copied — a dangling one waits
  months and then fails with an error naming the wrong thing.

## A new chat platform

Hermes carries the adapter; the pack needs three small things:

1. a `platform.kind` branch in `render.py` deciding which env vars flow and
   which plugins load (see the mattermost branch — it is the template);
2. the entrypoint's `.env` whitelist already passes `TELEGRAM_*`/`SLACK_*`/
   `DISCORD_*` through; extend the prefix list for anything else;
3. a binding for `untagged-routing` if the fleet should route untagged
   messages there: the policy (`policy.py`) names no chat product on purpose —
   write a new `__init__.py`-style binding that answers the policy's two
   questions (who last spoke in this thread, who else is in this room) from
   that platform's API, and reuse the rules unchanged.

Plugins that are Mattermost-specific today: mention-on-edit, trace-to-card,
reasoning-live, steer-into-turn (they patch the Mattermost adapter). The
capability gaps they close may or may not exist on another adapter — measure
before porting.

## A new executor (brain)

Anything that speaks ACP is `kind: custom` + `command:`/`args:` in the
manifest. Before trusting it, drive it for real: a clean `initialize` proves
nothing (one registry survey found 5 of 38 agents usable end to end). Check: a
plain turn, a tool call through MCP, streaming, continuity, and whether it
sends `usage_update` (without it `maxContextFill` cannot protect its sessions).
An OpenAI-compatible endpoint (local vLLM, a paid API) is `kind: openai` with
`base_url` + `api_key_env` and joins the same litellm chain.

## Recording what you learn

Fleet workspaces are HINT-friendly: keep durable knowledge (decisions,
invariants, hazards) in `.hint` files next to what they govern, and let agents
read it with `hint <path>`. The pack's own founding decisions live in `_.hint`
at its root. A checkout an agent works from can be arbitrarily old — fix stale
claims where they stand, and pull the agent's checkout when the fix has to take
effect now.
