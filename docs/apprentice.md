# Apprentice agents

Declare `role: apprentice` and an immutable platform `owner` user id. The fast
gate answers mentions, DMs and replies; unrelated human messages are observations
and consume no full agent turn. Bot messages remain behind the loop fence.

Observations keep references and neutralized summaries, not wholesale bodies of
other participants. Only owner-authored resolutions may seed distillation.
Recurring patterns become proposals with source episode ids; they never become
active procedures automatically. Approved files live under
`agents/<name>/procedures/` and the SOUL carries only their names.

Autonomy is per procedure: `shadow`, `draft`, then owner-promoted `auto`. Money,
access and personnel stay non-auto by default; explicit requests for the owner
always escalate. Auto replies use the bot's own identity and say they are on
behalf of the owner—never impersonating the owner. Disclose the observing bot in
the channel topic, and keep the durable owner DM approval queue reviewable.
