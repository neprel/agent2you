# Subscription logins and API fallback

agent2you is designed for one operator using that operator's own harness logins
on that operator's own machines. acp2api translates ACP because Hermes does not
yet speak it; it does not share accounts, resell access, or provide a
multi-tenant service. This is a description of the mechanics and operator
controls, not legal advice or a claim of provider compliance.

Provider policies and metering have changed before. Keep one human operator per
subscription login, do not use one login to serve unrelated users, leave agents
reactive unless you deliberately schedule work, and use fallback chains to fit
documented limits.

## Move to API billing

For chat-grade continuity, replace or append a harness executor:

```yaml
brains:
  chain: [paid]
  executors:
    paid:
      kind: api
      model: anthropic/claude-sonnet-4-5
      api_key_env: ANTHROPIC_API_KEY_MAIN
```

Set the named key in `deploy/.env`, then `a2y render && a2y up`. Apply the same
edit under `defaults.brains` for the whole fleet. A native `api` brain is a bare
model and lacks Claude Code/Codex harness tools. For full Claude Code capability
on API billing, configure Claude Code itself with its supported Anthropic API-key
login and keep the ACP path. Never use `OPENAI_API_KEY` as a provider-key name in
the manifest: inside this image that name is reserved for the LiteLLM bearer.
