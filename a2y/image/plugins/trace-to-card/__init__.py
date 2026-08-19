"""Put the finished answer in the post, and the work behind it one click away.

The problem
-----------
With `progress: reasoning` in acp2api, a turn now narrates itself: what it read,
what it edited, how its plan is going. Hermes renders that with the thinking, as a
blockquote, at the top of the post it delivers the answer in.

Live, that is exactly what you want. Once the answer lands it is the opposite: the
post someone scrolls back to, quotes, or links a colleague is now four fifths
trace.

What this does
--------------
The leading run of blockquote lines is lifted out of the message body and into
`props.card`. Mattermost renders a post with a card as an ⓘ next to the timestamp,
and clicking it opens the content in the right-hand panel. So the channel shows the
answer, and the whole trace is one click away instead of deleted.

That is deliberately not `cleanup_progress`, which would throw the trace away. The
reason for showing the work is that someone might want to check it.

Both delivery paths, because Hermes uses both
---------------------------------------------
`send` AND `edit_message`. Measured on a live turn: when a streaming preview has
been visible longer than `fresh_final_after_seconds`, Hermes abandons it and posts
the answer as a BRAND NEW message (`GatewayStreamConsumer._try_fresh_final`), and
the gateway's own non-streaming delivery is a plain `send` too. Patching only
`edit_message` -- which is what this plugin did first -- produced a plugin that
worked in a unit test and never once fired in production.

Where the card's contents come from
-----------------------------------
Two sources, best first:

1. the `reasoning-live` plugin's record of the turn, which is the WHOLE trace;
2. failing that, the blockquote Hermes prepended, which is `last_reasoning`
   truncated to fifteen lines -- measured at "... (25 more lines)" on an answer
   that ran three commands.

The fallback is not decoration. It is what makes this plugin work on its own, and
the lookup into the other one is a `sys.modules` probe that returns None rather
than raising if it is not loaded.

It also adds `delete_message`, which Hermes' Mattermost adapter does not implement.
The gateway checks for it before enabling `cleanup_progress` at all, so without it
that setting is silently inert -- and `_try_fresh_final` uses it to clear the
preview it abandoned.

What it is coupled to, and how it fails
---------------------------------------
The fallback reads Hermes' RENDERING, which is presentation and not contract: it
assumes `display.reasoning_style: blockquote`, and finds the trace by the `>`
prefix. If that setting changes, or Hermes changes how it marks reasoning, the
split simply stops matching and every post is left exactly as it is. Fail-open is
the whole design here -- a presentation plugin must never be able to eat an answer.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

INSTALL_NAG_SECONDS = 180.0
INSTALL_POLL_SECONDS = 2.0

# Mattermost renders a card as an ⓘ beside the timestamp; the panel it opens is
# markdown. There is no length field in the API, but a card is a side panel and not
# a document -- past this it is scrollback with extra steps.
CARD_LIMIT = 60_000

# Hermes loads plugins under this package prefix, which is how one finds another.
REASONING_LIVE = "hermes_plugins.reasoning_live"


def split_trace(text: str) -> Tuple[Optional[str], str]:
    """Separate a leading blockquote run from the answer that follows.

    Returns `(trace, answer)`, with `trace` None when there is nothing to lift.

    Three refusals, all deliberate:

    * no leading `>` line -- nothing to do;
    * nothing but blockquote -- the trace IS the message (a turn that produced no
      answer), and hiding it would leave an empty post;
    * an answer that is only whitespace -- same case, seen from the other side.
    """
    lines = text.split("\n")
    cut = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">"):
            cut = i + 1
        elif stripped == "" and cut:
            # A blank line INSIDE the quoted run: keep going, but do not let it
            # extend the trace on its own -- `cut` only moves on a quoted line.
            continue
        else:
            break

    if cut == 0:
        return None, text
    answer = "\n".join(lines[cut:]).strip()
    if not answer:
        return None, text
    return "\n".join(lines[:cut]).strip(), answer


def as_blockquote(text: str) -> str:
    """Quote every line. For the FALLBACK trace only — see `_full_trace`."""
    body = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines())
    return f"> 💭 **Reasoning:**\n{body}"


def _full_trace(chat_id) -> Optional[str]:
    """The whole trace for this channel's last turn, from `reasoning-live`.

    A probe rather than an import: this plugin has to work when that one is not
    in `plugins.enabled`, and Hermes loads both under synthetic module names, so
    importing by path would build a second copy with an empty registry.
    """
    mod = sys.modules.get(REASONING_LIVE)
    fn = getattr(mod, "trace_for", None) if mod is not None else None
    if fn is None:
        return None
    try:
        text = fn(chat_id)
    except Exception:  # noqa: BLE001
        logger.debug("trace-to-card: could not read the live trace", exc_info=True)
        return None
    # Returned READY: `reasoning-live.render` has already given each line its
    # weight -- bold-italic for the agent's own commentary, italic for what a
    # command is for, monospace for the command, a quote bar for its output.
    # Wrapping the lot in one more blockquote would double every bar and flatten
    # exactly the hierarchy that was just built.
    return f"💭 **Reasoning:**\n\n{text}" if text else None


def _adapter_classes() -> list:
    """Every live MattermostAdapter class, found by scanning loaded modules.

    Importing `plugins.platforms.mattermost.adapter` by name is WRONG here and
    silently does nothing: Hermes loads plugins with `spec_from_file_location`
    under synthetic names, so importing the same file by its package path produces
    a SECOND class object and patches the one nobody is running. Learned once,
    written down twice -- see the mention-on-edit plugin.
    """
    seen = []
    for name, mod in list(sys.modules.items()):
        if mod is None or "mattermost" not in name.lower():
            continue
        cls = getattr(mod, "MattermostAdapter", None)
        if cls is None or not hasattr(cls, "edit_message"):
            continue
        if any(cls is c for c in seen):
            continue
        seen.append(cls)
    return seen


def _from_adapter_module(cls, name: str, default=None):
    """A symbol out of the module Hermes actually loaded the adapter from.

    `SendResult` and the mentions-disabled props are both taken this way, for the
    reason `_adapter_classes` scans instead of importing.
    """
    return getattr(sys.modules[cls.__module__], name, default)


def _card_props(cls, card: str) -> Dict[str, Any]:
    """Post props carrying the card, WITH the adapter's own mention guard.

    `props` replaces rather than merges on patch, so whatever the adapter sets on
    every post has to be carried over by hand. Losing it would make a finished
    answer re-notify everyone it names -- and it is read from the adapter rather
    than written out here, because an earlier version of this file guessed the key
    and guessed wrong.
    """
    base = _from_adapter_module(cls, "_MATTERMOST_DISABLE_MENTIONS_PROPS", {}) or {}
    return {**dict(base), "card": card[:CARD_LIMIT]}


def _patch(cls) -> bool:
    if getattr(cls, "_trace_to_card", False):
        return True

    original_send = cls.send
    original_edit = cls.edit_message
    if original_send is None or original_edit is None:
        logger.error("trace-to-card: %s has no send/edit_message; not patching", cls)
        return False

    async def _attach_card(self, message_id: str, card: str) -> None:
        """Add the card to a post that has already been delivered.

        A props-only patch: the message is left exactly as the adapter wrote it,
        including any chunking it did. On a split answer this lands on the last
        chunk, which is the one the reader ends on.
        """
        try:
            await self._api_put(f"posts/{message_id}/patch", {"props": _card_props(cls, card)})
        except Exception:  # noqa: BLE001 - a rendering nicety must never lose an answer
            logger.exception("trace-to-card: could not attach the card to %s", message_id)

    # NO `**kwargs` ON EITHER WRAPPER, and it is not a style preference.
    #
    # Hermes decides what to pass by INSPECTING the signature it finds:
    #
    #     _edit_params = inspect.signature(adapter.edit_message).parameters
    #     _edit_accepts_metadata = ("metadata" in _edit_params
    #         or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in ...))
    #
    # A wrapper with `**kwargs` therefore ADVERTISES a parameter the wrapped
    # method does not have. Hermes passed `metadata=`, this file forwarded it,
    # and the stock `edit_message` -- which takes no metadata -- raised
    # TypeError on every single progress edit:
    #
    #     ERROR gateway.run: Progress message error:
    #     MattermostAdapter.edit_message() got an unexpected keyword argument 'metadata'
    #
    # The progress bubble was created and then never updated again, on every
    # turn, for as long as this plugin has been loaded. It looked exactly like
    # the trace not being produced, and cost a live debugging session to find.
    # A wrapper must present the wrapped signature, no wider.
    #
    # `metadata` is accepted here and deliberately DISCARDED: for Mattermost it
    # only ever carried thread routing, and an edit is addressed by post id.
    # Accepting it is what makes the bubble editable at all.

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        trace, answer = split_trace(content or "")
        if trace is None:
            return await original_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)

        result = await original_send(self, chat_id, answer, reply_to=reply_to, metadata=metadata)
        message_id = getattr(result, "message_id", None)
        if getattr(result, "success", False) and message_id:
            await _attach_card(self, message_id, _full_trace(chat_id) or trace)
        return result

    async def edit_message(self, chat_id, message_id, content, *, finalize: bool = False, metadata=None):
        # Only the last edit. Doing this mid-stream would make the trace vanish
        # while it is still the interesting part of the post.
        if not finalize:
            return await original_edit(self, chat_id, message_id, content, finalize=finalize)

        trace, answer = split_trace(content or "")
        if trace is None:
            return await original_edit(self, chat_id, message_id, content, finalize=finalize)

        try:
            payload: Dict[str, Any] = {
                "message": self.format_message(answer),
                "props": _card_props(cls, _full_trace(chat_id) or trace),
            }
            data = await self._api_put(f"posts/{message_id}/patch", payload)
            if data and "id" in data:
                return _from_adapter_module(cls, "SendResult")(success=True, message_id=data["id"])
            logger.warning("trace-to-card: patch returned nothing; falling back to a plain edit")
        except Exception:  # noqa: BLE001
            logger.exception("trace-to-card: patch failed; falling back to a plain edit")

        return await original_edit(self, chat_id, message_id, content, finalize=finalize)

    async def delete_message(self, chat_id, message_id) -> bool:
        """Mattermost can delete a post; the stock adapter just never says so.

        The gateway checks whether this method is overridden before enabling
        `cleanup_progress` at all, so its absence made that setting silently inert.

        Written against `self._session` directly because the adapter has GET, POST
        and PUT helpers and no DELETE. Same headers, same base url, same failure
        style: a deletion that does not happen returns False and the caller leaves
        the message in place.
        """
        try:
            url = f"{self._base_url}/api/v4/posts/{message_id}"
            async with self._session.delete(url, headers=self._headers()) as resp:
                if resp.status >= 400:
                    logger.debug("trace-to-card: delete %s → %s", message_id, resp.status)
                    return False
                return True
        except Exception:  # noqa: BLE001
            logger.debug("trace-to-card: could not delete %s", message_id, exc_info=True)
            return False

    cls.send = send
    cls.edit_message = edit_message
    cls.delete_message = delete_message
    cls._trace_to_card = True
    return True


def _install() -> bool:
    classes = _adapter_classes()
    return bool(classes) and all(_patch(cls) for cls in classes)


def _install_when_loaded() -> None:
    """Wait for the platform plugin, however long the gateway takes to load it.

    No deadline. Giving up after three minutes left the plugin absent for the life
    of the process with a single error line at boot to explain it -- so the answer
    lands as a wall of trace and the reason is somewhere in the scrollback.
    """
    waited = 0.0
    while True:
        if _install():
            logger.warning("trace-to-card: patched MattermostAdapter")
            return
        time.sleep(INSTALL_POLL_SECONDS)
        waited += INSTALL_POLL_SECONDS
        if waited % INSTALL_NAG_SECONDS < INSTALL_POLL_SECONDS:
            logger.warning(
                "trace-to-card: still waiting for MattermostAdapter (%.0fs). The "
                "trace stays in the post instead of the card until it loads.", waited,
            )


def register(ctx) -> None:
    if _install():
        logger.warning("trace-to-card: patched MattermostAdapter")
        return
    threading.Thread(target=_install_when_loaded, name="trace-to-card", daemon=True).start()
