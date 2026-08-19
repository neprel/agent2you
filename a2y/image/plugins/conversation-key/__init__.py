"""Put Hermes' session id on the wire, as `x-conversation-id`.

Why this exists
---------------
acp2api keeps a live coding-agent session per conversation and normally works out
which one a request belongs to by matching the incoming history against what each
session has already heard. That only works for a caller that resends a growing
transcript.

Hermes is not that caller. It keeps the transcript on its own side and sends
`system` plus exactly ONE rolled-up user message per request -- verified on the
wire, and visible in acp2api's log as `new session for 1 message(s)` on every
single turn. No two requests share a prefix, so nothing ever matches, and every
Mattermost message got a cold agent that had lost its plan, its open files and its
subagents. Measured before this plugin: 19 new sessions against 1 continuing.

Hermes already knows the answer -- `session_id` is one per thread -- so the fix is
to say it out loud rather than have acp2api guess. `llm_request` middleware is the
supported place to do that: it runs immediately before the provider call, is handed
the session id, and applies to `provider: custom` like any other.

Two things this depends on, both checked:
  * litellm forwards any `x-` header (except `x-stainless-*`) to the upstream,
    but only with `general_settings.forward_client_headers_to_llm_api: true`.
  * acp2api reads `server.conversationHeader`, default `x-conversation-id`
    (>= 1.4.0). Without that version the header is simply ignored.
"""

from __future__ import annotations

HEADER = "x-conversation-id"


def _tag_request(request=None, *, session_id: str = "", platform: str = "", **_ignored):
    """Attach the conversation's identity to the outgoing request.

    Returning ``None`` leaves the request untouched, which is the right answer for
    anything that is not the conversation itself.
    """
    if not isinstance(request, dict) or not session_id:
        return None

    # Structured-output calls are never the conversation. Hermes titles a session
    # with a separate completion carrying its own tiny system prompt ("You name
    # chat sessions...") and a response_format; tagging it would drop that prompt
    # into the thread's own agent session and pollute it.
    if request.get("response_format") is not None:
        return None

    headers = dict(request.get("extra_headers") or {})
    if HEADER in headers:
        return None

    # Platform included so a CLI session and a Mattermost session can never
    # collide on an id, and so the key is readable in acp2api's log.
    headers[HEADER] = f"{platform}:{session_id}" if platform else session_id
    return {"request": {**request, "extra_headers": headers}}


def register(ctx) -> None:
    ctx.register_middleware("llm_request", _tag_request)
