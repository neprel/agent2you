"""Deliver a steered message into the turn that is actually running.

The gap this fills
------------------
`display.busy_input_mode: steer` promises that a message sent mid-turn joins the
work already under way, and Hermes says so out loud: *"Steered into current run.
Your message arrives after the next tool call."*

It cannot happen here. Hermes steers by adding the message to the conversation for
its NEXT model call, and in this deployment **one model call is the entire coding
agent turn** -- Claude Code runs its whole tool loop inside a single completion.
The counter says `iteration 1/90` for twenty minutes because there is no second
iteration until everything is finished. And the "next tool call" in that sentence
means one of HERMES' tools, of which this agent configures none -- its tools live
inside the ACP session, where the loop that calls them actually runs (see
ai/_.hint#tools_reach_brain).

Measured on 2026-08-12: a marker word sent mid-turn never appeared in the trace or
the answer, and no second request reached the bridge. The message was not lost --
it arrived as the next turn, on the same session -- but that is `queue`, whatever
the setting is called.

What this does
--------------
Sends the message to the bridge, which steers it into the live ACP session with
`_session/steering` -- the one extension both shipped adapters implement, since ACP
itself defines no mid-turn input at all. Requires acp2api >= 1.7.1.

Measured end to end: steered twelve seconds into a turn whose first command was a
45-second sleep, the original work finished, the steered command ran too, and both
came back in the answer to the original request.

Delivered instead of Hermes' own handling, not alongside it
-----------------------------------------------------------
When the delivery lands, the busy handler is not called at all and this returns
"handled". Anything less lets the same instruction run twice -- once inside the
live turn, once as the turn after it.

Hermes queues the text because its own steer reports failure, and here it always
does: `AIAgent.steer` stashes the message for the next tool result, and one model
call is the entire coding-agent turn, so there is no next tool result. The obvious
fix -- suppress the one queueing call in the busy handler -- was tried and the log
refuted it: steered into the live turn at 07:42:09, back as `continuing session
keyed, 1 new of 3` at 07:42:19. That handler is not the only thing that queues.
When it reports "not handled" the ADAPTER queues too, in code of its own
(`_queue_text_debounce`, `merge_pending_message_event`), and no amount of patching
the runner reaches those. Patching sites one at a time is a guess repeated until it
runs out; declining to call the handler is a decision.

The price is Hermes' busy-acknowledgement bubble. Every other duty of that handler
is a CONDITION of taking the message over -- authorization, pending approvals, a
draining gateway, internal events, media -- see `_hermes_must_handle`. And the
steered text shows up in the live trace within seconds, which acknowledges it
better than a bubble does.

When nothing is delivered -- no live turn, another model, a bridge that refused --
Hermes handles the message exactly as it always did. Losing a message is the one
outcome this must never produce.

One endpoint, and it is not this file's to choose
-------------------------------------------------
The request goes exactly where Hermes sends its own turns, read from Hermes' own
`model:` block -- base url, key and model name, with `${VAR}` expanded from the
environment the same way. Nothing here is hardcoded.

That is the point rather than a convenience. A plugin pointing somewhere its agent
does not is a second client with its own opinion, and it goes wrong precisely when
the topology changes -- which it does here: litellm holds a failover chain in the
normal shape, and is bypassed while one channel is tested on its own. Both work
without touching this file.

Because the model is Hermes' own, there is no guessing: the running turn is on
whatever Hermes asked for, so the injection asks for the same thing. A **409**
means the turn ended in the meantime, or a failover put it on another brain -- and
costs nothing, because `x-acp2api-inject` says "join a running turn or do nothing".
Without that marker a miss would start a whole turn of a real subscription for an
answer nobody is waiting for.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# How often to repeat that it is still waiting. Repeated rather than said
# once: a single line at boot is gone from the scrollback by the time anyone
# wonders why the plugin is doing nothing.
INSTALL_NAG_SECONDS = 180.0
INSTALL_POLL_SECONDS = 2.0

# Marks a request as an injection and nothing else. acp2api answers 409 when there
# is no running turn to join, which is what makes trying several models free.
INJECT_HEADER = "x-acp2api-inject"
# The same header the conversation-key plugin puts on ordinary completions. It has
# to match exactly, because it is the only thing tying a Mattermost thread to an
# ACP session.
CONVERSATION_HEADER = "x-conversation-id"

# Short. This runs on the gateway's event loop while a turn is in flight, and the
# bridge either has the conversation in hand or does not -- there is nothing here
# worth waiting on.
TIMEOUT_SECONDS = 10.0


def _env(name: str) -> str:
    """A value from the environment, or from the file the entrypoint writes.

    The ports are computed at boot and land in `/run/agent.env`, which supervisord
    sources for the services; a plugin inside the gateway may not have inherited
    them.
    """
    value = os.environ.get(name)
    if value:
        return value
    try:
        with open("/run/agent.env", encoding="utf-8") as fh:
            for line in fh:
                key, _, val = line.strip().partition("=")
                if key == name:
                    return val
    except OSError:
        pass
    return ""


def _brain() -> tuple[str, str, str] | None:
    """Where Hermes sends its turns, read from Hermes' own configuration.

    `(base_url, api_key, model)`, or None when it cannot be determined.

    Nothing here is hardcoded and nothing is guessed, because a plugin that talks
    to a different endpoint than the agent it serves is not a plugin -- it is a
    second client with its own opinion. Whatever `model:` says, this follows:
    litellm holding a failover chain, or one bridge directly while a channel is
    being tested. Both, without editing this file.

    `${VAR}` is expanded the way Hermes expands it, from the process environment,
    so the ports the entrypoint computes at boot resolve identically here.
    """
    path = os.path.join(os.environ.get("HERMES_HOME", "/root/.hermes"), "config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return None
    # A deliberately small reader rather than a YAML dependency: this needs three
    # scalars from one known block, and the block is written by this repository.
    block = re.search(r"^model:\n((?:[ \t]+.*\n|\n)*)", body, re.M)
    if not block:
        return None
    found = {}
    for key in ("base_url", "api_key", "default"):
        m = re.search(rf"^[ \t]+{key}:[ \t]*(\S.*?)[ \t]*$", block.group(1), re.M)
        if m:
            found[key] = re.sub(r"\$\{(\w+)\}", lambda g: _env(g.group(1)), m.group(1).strip("\"'"))
    base, model = found.get("base_url", ""), found.get("default", "")
    return (base.rstrip("/"), found.get("api_key", ""), model) if base and model else None


def _post(url: str, key: str, payload: dict, headers: dict) -> int:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def deliver(conversation_id: str, text: str) -> bool:
    """Put `text` into whichever running turn belongs to `conversation_id`."""
    where = _brain()
    if not where or not conversation_id or not text.strip():
        return False
    base, key, model = where
    status = _post(
        f"{base}/chat/completions",
        key,
        {"model": model, "messages": [{"role": "user", "content": text}]},
        {CONVERSATION_HEADER: conversation_id, INJECT_HEADER: "1"},
    )
    if status == 200:
        logger.warning("steer-into-turn: delivered into the running %s turn [%s]", model, conversation_id)
        return True
    # 409 is the ordinary answer: the turn ended between the steer and this call,
    # or a failover put it on a brain this request did not reach. Nothing is lost --
    # Hermes still delivers the same text with the next turn, on the same session.
    #
    # WARNING even so, and every other line on this path with it. The container
    # emits WARNING and above -- nothing quieter leaves the process. An INFO here
    # is not a quiet log, it is no log, and it made a refusal look exactly like a
    # handler that was never called. That was the whole of the previous test.
    logger.warning("steer-into-turn: %s answered %s for [%s]", model, status, conversation_id)
    return False


def _base_adapter_class():
    """The platform adapter base, whose `set_busy_session_handler` stores the hook."""
    for name, mod in list(sys.modules.items()):
        if mod is None or "platforms.base" not in name:
            continue
        for attr in dir(mod):
            cls = getattr(mod, attr, None)
            if isinstance(cls, type) and "set_busy_session_handler" in vars(cls):
                return cls
    return None


def _hermes_must_handle(runner, event, session_key) -> str:
    """Why this message is Hermes' to handle, or "" when it may be steered.

    Taking a message over means taking over everything the busy handler would have
    done with it, so each of these is one of its own gates, kept in the same order
    and for the same reason:

    * an unauthorized sender is DROPPED there, and in a shared thread that gate is
      the only thing between a stranger and someone else's session;
    * a plain-text "yes" while a dangerous command waits for approval is routed to
      the approval resolver. Steering it instead would leave the agent blocked on
      an approval that never resolves, and the command auto-denied on timeout --
      trading a duplicated message for a deadlock;
    * a draining gateway owes the user "queued for after the restart", not silence;
    * an internal event is not a person talking -- delegation completions and
      background-process notifications re-enter the session as MessageEvents and
      must never be spliced into a running turn;
    * media has to reach the transcription and album paths, which live in there.
      A voice note steered as its empty caption is a lost message.
    """
    if not runner._is_user_authorized(event.source):
        return "sender is not authorized"
    if getattr(event, "internal", False):
        return "internal event"
    if getattr(runner, "_draining", False):
        return "gateway is draining"
    if getattr(event, "media_urls", None) or getattr(event, "media_types", None):
        return "carries media"
    try:
        from tools.approval import has_blocking_approval

        if getattr(event, "allow_gateway_control", True) and has_blocking_approval(session_key):
            return "an approval is waiting on this session"
    except Exception:  # noqa: BLE001 - no approval module is not a reason to refuse
        pass
    return ""


async def _deliver_for(runner, event) -> bool:
    """Put this event's text into the turn running for its conversation."""
    # `session_key` is Hermes' composite routing key, NOT the id the conversation
    # header carries -- that is the session id, and it comes from the store, the
    # same place conversation-key's value comes from.
    entry = await runner.async_session_store.get_or_create_session(event.source)
    platform = getattr(getattr(event.source, "platform", None), "value", None) or ""
    session_id = getattr(entry, "session_id", "") or ""
    conversation_id = f"{platform}:{session_id}" if platform else session_id
    text = (getattr(event, "text", "") or "").strip()
    # Off the event loop: `deliver` is a blocking HTTP call, and the loop it would
    # block is the one streaming the very turn being joined. Awaited rather than
    # fired into a thread and forgotten, because the ANSWER decides whether Hermes
    # may also queue this text -- see `wrapped`.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, deliver, conversation_id, text)


def _wrap(handler):
    """Wrap the gateway's busy-message handler with the delivery."""
    if handler is None or getattr(handler, "_steer_into_turn", False):
        return handler

    async def wrapped(event, session_key):
        runner = getattr(handler, "__self__", None)
        # `_busy_input_mode` is only the fallback. Hermes resolves the real one per
        # source (`_effective_busy_input_mode`), because a profile may override it --
        # so reading the attribute alone can report `interrupt` for a session that
        # is genuinely in steer mode.
        effective = getattr(runner, "_effective_busy_input_mode", None)
        mode = effective(event.source) if effective else getattr(runner, "_busy_input_mode", "")
        text = (getattr(event, "text", "") or "").strip()

        why = ""
        if runner is None:
            why = "no runner behind the handler"
        elif mode != "steer":
            why = f"busy_input_mode is {mode!r}"
        elif not text:
            why = "no text to steer"
        else:
            why = _hermes_must_handle(runner, event, session_key)
        # Every decision is spoken, and at WARNING, because the container emits
        # nothing quieter. Whether this runs at all is the one thing the log could
        # never tell us -- Hermes only calls the busy handler while the session is
        # in `_active_sessions`, so a message landing a second after the turn ends
        # never reaches here, and that case is indistinguishable from a broken
        # install unless the entry says so. Each round of guessing cost a live test.
        logger.warning("steer-into-turn: busy message (mode=%r, hermes_handles=%r)", mode, why or False)

        delivered = False
        if not why:
            try:
                delivered = await _deliver_for(runner, event)
            except Exception:  # noqa: BLE001 - a delivery must never swallow a message
                logger.exception("steer-into-turn: could not deliver the steered message")

        if delivered:
            # HANDLED, and Hermes' busy handler is not called at all. That is the
            # only reliable way to stop the message being run a second time.
            #
            # Suppressing the one queueing call inside that handler was not enough,
            # and the log proved it: the text was steered into the live turn and
            # still came back as `continuing session keyed, 1 new of 3` ten seconds
            # later. The handler is not the only thing that queues -- when it
            # reports "not handled" the ADAPTER queues too, in its own code
            # (`_queue_text_debounce`, `merge_pending_message_event`), and those are
            # reached whatever is done to the runner. Patching each site in turn is
            # a guess repeated until it runs out; not calling the handler is a
            # decision.
            #
            # What is given up with it is the busy acknowledgement bubble. Every
            # other duty of that handler is a condition of getting here at all --
            # see `_hermes_must_handle` -- and the steered text appears in the trace
            # within seconds, which is a better acknowledgement than a bubble.
            logger.warning("steer-into-turn: handled here; Hermes will not queue it again")
            return True

        # Nothing delivered: no live turn, another model, a bridge that refused.
        # Hermes handles it exactly as it always did, and nothing is lost.
        return await handler(event, session_key)

    wrapped._steer_into_turn = True
    return wrapped


def _install() -> bool:
    """Wrap the handler AT REGISTRATION, not on the class that defines it.

    The gateway hands the adapter a BOUND METHOD --
    `adapter.set_busy_session_handler(self._handle_active_session_busy_message)` --
    and the adapter keeps that object. Replacing the attribute on `GatewayRunner`
    afterwards changes nothing: the binding was already taken, and the wrapper
    installed cleanly, logged that it had, and was never once called. Silent, and
    for the same reason as before -- a component that cannot fail loudly will
    eventually fail quietly.

    So the wrap happens where the handler is handed over -- and, because load order
    is not ours to choose, live adapters are swept too: anything registered from now
    on goes through the wrapper, and anything registered already is rewrapped in
    place. Both halves, or the whole thing is a coin toss on plugin ordering.
    """
    cls = _base_adapter_class()
    if cls is None:
        return False
    if not getattr(cls, "_steer_into_turn", False):
        original = cls.set_busy_session_handler

        def set_busy_session_handler(self, handler):
            return original(self, _wrap(handler))

        cls.set_busy_session_handler = set_busy_session_handler
        cls._steer_into_turn = True
    # The adapters already alive. One pass over the heap at boot, on a background
    # thread, against the alternative of a wrapper whose correctness depends on
    # which plugin the loader happened to reach first. `_wrap` is idempotent.
    for obj in gc.get_objects():
        try:
            if isinstance(obj, cls) and getattr(obj, "_busy_session_handler", None) is not None:
                obj._busy_session_handler = _wrap(obj._busy_session_handler)
        except Exception:  # noqa: BLE001 -- a half-built object on the heap is not our business
            continue
    return True


def _install_when_loaded() -> None:
    """Wait for the platform adapter base to load. No deadline.

    It arrives with the platform plugin, which may load after this one, and a
    plugin that gave up at three minutes is a plugin that is simply not there.
    """
    waited = 0.0
    while True:
        try:
            if _install():
                logger.warning("steer-into-turn: wrapped the busy-message handler")
                return
        except Exception:  # noqa: BLE001
            logger.debug("steer-into-turn: install attempt failed", exc_info=True)
        time.sleep(INSTALL_POLL_SECONDS)
        waited += INSTALL_POLL_SECONDS
        if waited % INSTALL_NAG_SECONDS < INSTALL_POLL_SECONDS:
            logger.warning(
                "steer-into-turn: still waiting for the platform adapter (%.0fs). "
                "Steering stays inert until it loads.", waited,
            )


def register(ctx) -> None:
    # Nothing is attempted on the loader's own thread. `register` runs inside
    # plugin discovery, and importing one of Hermes' own modules there means
    # pulling a very large module in at exactly the wrong moment -- and anything
    # that raises or blocks in here takes the plugin out of the run with no error
    # anywhere. That is not a hypothesis: this plugin was silently absent for
    # several boots while `hermes plugins list` kept reporting it enabled.
    #
    # The worker does the same work two seconds later, guarded per attempt, and
    # says so either way.
    threading.Thread(target=_install_when_loaded, name='steer-into-turn', daemon=True).start()
