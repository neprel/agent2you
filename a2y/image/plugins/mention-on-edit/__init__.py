"""Make a mention that arrives by EDIT wake the agent.

The problem
-----------
Mattermost's adapter starts with `if event_type != "posted": return`, so
`post_edited` is never looked at. Add `@agent` to a message you already sent and
nothing happens. The same applies to a mention written inside a streamed answer,
because Hermes publishes an answer by creating a post and editing it as text
arrives -- so the name is never present at create time.

There is a second wall behind the first. `_handle_ws_event` calls
`self._dedup.is_duplicate(post_id)` BEFORE the mention gate, with a 300s TTL. The
original post was therefore already recorded even though it was ignored, and an
edit carrying the same post id would be dropped as a duplicate. Forwarding the
event is not enough; the dedup entry has to be released for that one dispatch.

Why the debounce
----------------
Handling edits naively is genuinely dangerous, which is probably why upstream does
not. A Hermes answer is edited dozens of times while it streams, and a human types
in bursts too. Holding each edited post until its edits stop collapses a stream
into a single evaluation of the final text.

The debounce is not perfect and the failure mode is chosen deliberately: a stream
that pauses longer than the window (waiting on the model) is evaluated early, and
`_seen` means a post can only ever be dispatched once this way.

It was also, originally, what was supposed to keep one agent from waking on
another's half-written sentence. It was not enough -- a progress bubble that sat
still for fifteen seconds while a model thought was read as a finished message,
and it cost a four-minute turn. HUMANS below is what actually does that job now.

Installation is a monkeypatch on the adapter class. That is a real cost -- Hermes
is tracked from `main` and this reaches into its internals -- so the patch verifies
what it is replacing and says so in the log rather than failing quietly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# How often to look for the adapter, and how often to say it still is not there.
INSTALL_POLL_SECONDS = 2.0
INSTALL_NAG_SECONDS = 180.0

# How long a post must go unedited before its text is taken as final.
DEBOUNCE_SECONDS = float(os.getenv("MATTERMOST_EDIT_DEBOUNCE_SECONDS", "15"))

# Only these authors' edits wake the agent. Added 2026-08-16 after this plugin
# cost a 248-second codex turn on a status message, and then broke the reply.
#
# What happened: one agent's PROGRESS BUBBLE -- the post Hermes edits in place
# while a turn runs -- narrated its investigation, and the narration contained a
# colleague's `@name`. This plugin does not know chrome from conversation, so it
# woke that colleague (45,194 prompt tokens, four minutes). The first agent then
# DELETED the bubble, as `cleanup_progress: true` is meant to; by the time the
# colleague answered, the post it
# had been given as its thread root did not exist, Mattermost refused the reply
# with `Invalid RootId parameter`, and the answer landed flat under
# "⚠️ Mattermost thread delivery failed". Three visible faults, one cause.
#
# A human's post is never chrome, so gating on authorship removes the whole class.
# What it gives up is narrow and was the rarer half of this plugin's purpose: a
# mention that appears in an agent's post AFTER it was created. An agent that
# means to call a colleague writes the name when it posts, and that path is the
# adapter's own and untouched.
#
# Empty list -> every author is admitted, which is the behaviour before this.
HUMANS = {p.strip() for p in os.getenv("MATTERMOST_HUMANS", "").split(",") if p.strip()}

# Bound on the "already dispatched" record. Post ids only need to be remembered
# long enough to survive a burst of edits to the same post.
_SEEN_MAX = 500
_SEEN_TTL = 3600.0


def _state(adapter) -> Dict[str, Any]:
    """Per-adapter state, kept on the instance so nothing is shared between agents."""
    st = getattr(adapter, "_mention_on_edit_state", None)
    if st is None:
        st = {"timers": {}, "seen": {}}
        adapter._mention_on_edit_state = st
    return st


def _prune(seen: Dict[str, float]) -> None:
    now = time.time()
    for pid, ts in list(seen.items()):
        if now - ts > _SEEN_TTL:
            del seen[pid]
    while len(seen) > _SEEN_MAX:
        del seen[min(seen, key=seen.get)]


async def _get(adapter, path: str) -> Optional[dict]:
    try:
        return await adapter._api_get(path)
    except Exception as exc:  # network, auth, a deleted post
        logger.debug("mention-on-edit: GET %s failed: %s", path, exc)
        return None


def _mentions_us(adapter, text: str) -> bool:
    patterns = [f"@{adapter._bot_username}", f"@{adapter._bot_user_id}"]
    low = (text or "").lower()
    return any(p.lower() in low for p in patterns if p and p != "@")


async def _settle(adapter, post_id: str, seed: Dict[str, Any], seed_data: Dict[str, Any], original) -> None:
    """Wait for the edits to stop, then hand the final text to the real handler."""
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return  # a further edit arrived; that edit owns the post now

    st = _state(adapter)
    st["timers"].pop(post_id, None)
    if post_id in st["seen"]:
        return

    # Re-read rather than trust the snapshot: between the last edit we saw and now,
    # the post may have changed again, and the whole point is to act on final text.
    post = await _get(adapter, f"posts/{post_id}") or seed
    if post.get("delete_at"):
        return

    channel_id = post.get("channel_id", "")
    channel = await _get(adapter, f"channels/{channel_id}") if channel_id else None
    channel_type = (channel or {}).get("type", "O")

    # A DM needs no mention -- that is the adapter's own rule, and it gates on
    # channel_type, which the edit event does not carry.
    if channel_type != "D" and not _mentions_us(adapter, post.get("message", "")):
        return

    sender_id = post.get("user_id", "")
    sender = await _get(adapter, f"users/{sender_id}") if sender_id else None

    st["seen"][post_id] = time.time()
    _prune(st["seen"])

    # Release the dedup entry the original post left behind, or the handler will
    # drop this as a duplicate before it ever reaches the mention gate.
    try:
        adapter._dedup._seen.pop(post_id, None)
    except Exception:
        logger.debug("mention-on-edit: could not clear the dedup entry", exc_info=True)

    data = dict(seed_data or {})
    data["post"] = json.dumps(post)
    data["channel_type"] = channel_type
    data.setdefault("sender_name", (sender or {}).get("username", sender_id))

    logger.info(
        "mention-on-edit: dispatching post %s (edited into a mention, channel %s)",
        post_id, channel_id,
    )
    await original(adapter, {"event": "posted", "data": data})


async def _on_edit(adapter, event: Dict[str, Any], original) -> None:
    data = event.get("data") or {}
    raw = data.get("post")
    if not raw:
        return
    try:
        post = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return

    # Our own streamed answer. Skipping it here is what keeps an agent from
    # chasing its own tail, and mirrors the guard in the original handler.
    if post.get("user_id") == adapter._bot_user_id:
        return
    if post.get("type"):  # system posts
        return
    # Somebody else's chrome. See the note on HUMANS: an edited post that no
    # person wrote is a progress bubble far more often than it is a message, and
    # acting on one costs a full CLI turn and then breaks the thread it answers in.
    if HUMANS and post.get("user_id") not in HUMANS:
        logger.debug(
            "mention-on-edit: ignoring an edit by %s -- not a human",
            post.get("user_id"),
        )
        return

    post_id = post.get("id") or ""
    if not post_id:
        return

    st = _state(adapter)
    if post_id in st["seen"]:
        return  # already woke us once; editing a typo must not re-run the work

    running = st["timers"].pop(post_id, None)
    if running:
        running.cancel()

    st["timers"][post_id] = asyncio.create_task(
        _settle(adapter, post_id, post, data, original)
    )


def _adapter_classes() -> list:
    """Every live MattermostAdapter class, found by scanning loaded modules.

    Importing `plugins.platforms.mattermost.adapter` by name is WRONG here and
    silently does nothing: Hermes loads plugins -- including its own bundled
    platforms -- with `spec_from_file_location` under synthetic names
    (`hermes_plugins.platforms__mattermost`). Importing the same file by its
    package path produces a SECOND, unrelated class object, and patching that one
    leaves the gateway running the original. Found the hard way.
    """
    seen = []
    for name, mod in list(sys.modules.items()):
        if mod is None or "mattermost" not in name.lower():
            continue
        cls = getattr(mod, "MattermostAdapter", None)
        if cls is None or not hasattr(cls, "_handle_ws_event"):
            continue
        if any(cls is c for c in seen):
            continue
        seen.append(cls)
    return seen


def _patch(cls) -> bool:
    original = cls._handle_ws_event
    if getattr(original, "_mention_on_edit", False):
        return False

    async def patched(self, event: Dict[str, Any]) -> None:
        if (event or {}).get("event") == "post_edited":
            try:
                await _on_edit(self, event, original)
            except Exception:
                logger.exception("mention-on-edit: failed handling an edit")
            return
        return await original(self, event)

    patched._mention_on_edit = True
    cls._handle_ws_event = patched
    return True


def _install() -> int:
    return sum(1 for cls in _adapter_classes() if _patch(cls))


def _install_when_loaded() -> None:
    """Keep looking until the platform appears.

    Platform plugins register lazily, so at `register()` time the Mattermost
    module may not be imported yet -- and if it is not, there is nothing to patch
    and no import to hook. Polling is inelegant and it is also the only thing here
    that does not depend on load order holding still across upgrades.
    """
    waited = 0.0
    while True:
        if _install():
            # WARNING and not INFO on purpose: this is a monkeypatch into another
            # project's internals, and it should be visible in a normal log
            # without anyone having to turn on debug first.
            logger.warning(
                "mention-on-edit: patched MattermostAdapter, %.0fs debounce, %s",
                DEBOUNCE_SECONDS,
                f"{len(HUMANS)} human author(s)" if HUMANS else "every author (MATTERMOST_HUMANS unset)",
            )
            return
        time.sleep(INSTALL_POLL_SECONDS)
        waited += INSTALL_POLL_SECONDS
        # No deadline. This used to give up after three minutes and log one line
        # about it, which is the worst of both: the plugin is gone for the life of
        # the process and the evidence has scrolled away by the time anyone asks
        # why a mention did not wake the agent. One dict lookup every couple of
        # seconds costs nothing, and the thread dies with the process.
        if waited % INSTALL_NAG_SECONDS < INSTALL_POLL_SECONDS:
            logger.warning(
                "mention-on-edit: still waiting for MattermostAdapter (%.0fs). A "
                "mention added by editing will not wake this agent until it loads.",
                waited,
            )


def register(ctx) -> None:
    if _install():
        logger.warning(
            "mention-on-edit: patched MattermostAdapter, %.0fs debounce, %s",
            DEBOUNCE_SECONDS,
            f"{len(HUMANS)} human author(s)" if HUMANS else "every author (MATTERMOST_HUMANS unset)",
        )
        return
    threading.Thread(target=_install_when_loaded, name="mention-on-edit", daemon=True).start()
