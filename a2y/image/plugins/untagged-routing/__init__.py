"""Route an untagged message to the agent it is actually for -- Mattermost binding.

The problem
-----------
Every agent here runs `require_mention: true`, so a message without `@name` is
dropped in a channel and in a GROUP DM alike -- only a one-to-one DM is exempt.
That is correct as a default and wrong as an experience: a human answering inside
a thread the agent is already working in has to name it again on every line, and
forgetting costs a silence with nothing in any log to explain it.

Turning the mention requirement off is not the fix. It is what stops two agents in
one channel from waking on each other's every word, and each wake is a whole
coding-CLI turn out of a real subscription.

What is here and what is not
----------------------------
THE RULES ARE NOT HERE. They live in `policy.py`, which knows nothing about
Mattermost, Hermes or any product name. This file is the binding: it translates a
websocket event into that module's `Message`, answers its two questions about the
room over the v4 REST API, and admits what it claims. A second chat platform would
add a second file like this one and reuse the policy unchanged -- see
ai/_.hint#untagged_routing for why the split is drawn here rather than left for
later.

Configuration this binding reads
--------------------------------
`MATTERMOST_HUMANS`      user ids of the PEOPLE who may wake an agent without
                         naming it. Empty leaves the plugin inert, deliberately:
                         with no list, the only alternative to doing nothing is
                         guessing, and a wrong guess wakes agents on each other.
`AGENT_ROOM_OWNERS`      the fleet's routing map, `default:<agent>,<room>:<agent>`,
                         IDENTICAL for every agent. Each one decides independently
                         whether it is the owner named, so a single winner needs no
                         coordinator. `default` takes anything no room names.
`MATTERMOST_UNTAGGED_FALLBACK`  the older form of `default:<this agent>`. Still
                         honoured so an existing deployment does not silently stop
                         claiming, and the map wins where both are set.
`MATTERMOST_THREAD_WINDOW_SECONDS`  how long a thread stays claimed by whoever
                         last answered in it. Default one day.

How a claim is admitted
-----------------------
By prepending `@self` to the message and handing it back to Hermes' own handler,
which then strips it again. The gate is not reimplemented: `allowed_users`, dedup,
attachments, threading and the free-response list all keep working, and a change
upstream cannot leave a second copy of the rules behind here.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # loaded as a package, which is the normal case
    from .policy import (
        CHANNEL,
        DEFAULT_ROOM,
        DIRECT,
        GROUP,
        Identity,
        Message,
        Policy,
        parse_room_owners,
    )
except ImportError:  # pragma: no cover
    # Hermes may load a plugin as a TOP-LEVEL module rather than as a package,
    # depending on how it was discovered, and then a relative import has no
    # parent to resolve against. Load the sibling by path instead of giving up:
    # the alternative is a plugin that works in one discovery mode and is absent
    # in the other, which is the hardest kind of absence to notice.
    import importlib.util
    import pathlib

    _NAME = "untagged_routing_policy"
    _spec = importlib.util.spec_from_file_location(
        _NAME, pathlib.Path(__file__).with_name("policy.py")
    )
    _policy = importlib.util.module_from_spec(_spec)
    # REGISTERED BEFORE EXECUTION, and that is not ceremony: `@dataclass` resolves
    # its fields through `sys.modules[cls.__module__]`, so a module executed while
    # absent from that table dies on `'NoneType' object has no attribute
    # '__dict__'` -- an error that names neither this file nor dataclasses.
    sys.modules[_NAME] = _policy
    _spec.loader.exec_module(_policy)
    CHANNEL, DEFAULT_ROOM, DIRECT, GROUP = _policy.CHANNEL, _policy.DEFAULT_ROOM, _policy.DIRECT, _policy.GROUP
    Identity, Message, Policy = _policy.Identity, _policy.Message, _policy.Policy
    parse_room_owners = _policy.parse_room_owners

INSTALL_POLL_SECONDS = 2.0
INSTALL_NAG_SECONDS = 180.0

# Mattermost's channel types, mapped onto the policy's three room shapes. `G` is
# the one that has cost real debugging: a group DM is a CHANNEL as far as the
# adapter's mention gate is concerned, and only `D` is exempt from it.
ROOM_KINDS = {"D": DIRECT, "G": GROUP}


def _ids(name: str) -> frozenset[str]:
    return frozenset(p.strip() for p in os.getenv(name, "").split(",") if p.strip())


PEOPLE = _ids("MATTERMOST_HUMANS")
THREAD_WINDOW_SECONDS = float(os.getenv("MATTERMOST_THREAD_WINDOW_SECONDS", "86400"))


def _room_owners(agent_name: str) -> Dict[str, str]:
    owners = parse_room_owners(os.getenv("AGENT_ROOM_OWNERS", ""))
    legacy = os.getenv("MATTERMOST_UNTAGGED_FALLBACK", "").lower() in {"1", "true", "yes"}
    if legacy and DEFAULT_ROOM not in owners:
        owners[DEFAULT_ROOM] = agent_name
    return owners


class MattermostWorld:
    """The policy's two questions, answered over the v4 REST API.

    Every failure answers None rather than raising. A deleted post or a channel
    this agent has lost access to must not turn into a dropped message: None means
    "unknowable", and the policy then declines to claim on that ground alone.
    """

    def __init__(self, adapter, people: frozenset[str]) -> None:
        self._adapter = adapter
        self._people = people

    async def _get(self, path: str) -> Optional[Any]:
        try:
            return await self._adapter._api_get(path)
        except Exception as exc:  # a deleted post, lost access, a network blip
            logger.debug("untagged-routing: GET %s failed: %s", path, exc)
            return None

    async def last_non_person_in_thread(self, thread: str):
        data = await self._get(f"posts/{thread}/thread")
        if not isinstance(data, dict):
            return None
        posts = [p for p in (data.get("posts") or {}).values() if isinstance(p, dict)]
        candidates = [
            p for p in posts
            if p.get("user_id")
            and p["user_id"] not in self._people
            and not p.get("delete_at")
            and not p.get("type")  # system posts belong to nobody
        ]
        if not candidates:
            return None
        last = max(candidates, key=lambda p: p.get("create_at") or 0)
        return last["user_id"], int(last.get("create_at") or 0)

    async def non_person_members(self, room: str):
        members = await self._get(f"channels/{room}/members")
        if not isinstance(members, list) or not members:
            return None
        return {m.get("user_id") for m in members if m.get("user_id") not in self._people}


def _mentions_us(adapter, text: str) -> bool:
    patterns = [f"@{adapter._bot_username}", f"@{adapter._bot_user_id}"]
    low = (text or "").lower()
    return any(p.lower() in low for p in patterns if p and p != "@")


def _policy_for(adapter) -> Policy:
    name = os.getenv("AGENT_NAME") or adapter._bot_username or ""
    return Policy(
        identity=Identity(id=adapter._bot_user_id, name=name),
        people=PEOPLE,
        room_owners=_room_owners(name),
        thread_window_seconds=THREAD_WINDOW_SECONDS,
    )


def _to_message(post: Dict[str, Any], channel_type: str) -> Message:
    props = post.get("props") or {}
    return Message(
        sender=post.get("user_id") or "",
        room=post.get("channel_id") or "",
        room_kind=ROOM_KINDS.get(channel_type, CHANNEL),
        thread=post.get("root_id") or "",
        # An incoming webhook (Alertmanager, CI) posts under the id of whoever
        # created it -- which may well be a person's. What wakes an agent on an
        # alert is a CRON that reads the channel, never the alert itself.
        # See ai/_.hint#the_fleet.
        machine=(
            str(props.get("from_webhook", "")).lower() == "true"
            or bool(props.get("override_username"))
        ),
        at_ms=int(post.get("create_at") or time.time() * 1000),
    )


async def _on_posted(adapter, event: Dict[str, Any], original) -> None:
    data = event.get("data") or {}
    raw = data.get("post")
    if not raw:
        return await original(adapter, event)
    try:
        post = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return await original(adapter, event)

    channel_type = data.get("channel_type", "O")
    text = post.get("message", "")

    # Leave alone everything the adapter already handles correctly: our own posts,
    # system posts, one-to-one DMs (no mention needed there), and anything that
    # already names us.
    if (
        channel_type == "D"
        or post.get("type")
        or post.get("user_id") == adapter._bot_user_id
        or _mentions_us(adapter, text)
    ):
        return await original(adapter, event)

    policy = _policy_for(adapter)
    reason = await policy.claim(_to_message(post, channel_type), MattermostWorld(adapter, PEOPLE))
    if reason is None:
        return await original(adapter, event)

    # Hand it back through the real gate with our own name on it. The gate strips
    # the mention again, so the agent sees the message the human actually wrote.
    post = dict(post)
    post["message"] = f"@{adapter._bot_username} {text}".strip()
    patched = dict(event)
    patched_data = dict(data)
    patched_data["post"] = json.dumps(post)
    patched["data"] = patched_data

    logger.info(
        "untagged-routing: claiming post %s in channel %s (%s)",
        post.get("id", "?"), post.get("channel_id", "?"), reason,
    )
    return await original(adapter, patched)


def _adapter_classes() -> List[type]:
    """Every live MattermostAdapter class, found by scanning loaded modules.

    Same reasoning as mention-on-edit: Hermes loads its bundled platforms with
    `spec_from_file_location` under synthetic names, so importing the module by
    its package path builds a SECOND class and patches nothing that runs.
    """
    seen: List[type] = []
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
    if getattr(original, "_untagged_routing", False):
        return False

    async def patched(self, event: Dict[str, Any]) -> None:
        if (event or {}).get("event") == "posted":
            try:
                return await _on_posted(self, event, original)
            except Exception:
                # A failure here must never swallow the message: fall through to
                # the untouched handler, which is the behaviour without us.
                logger.exception("untagged-routing: failed to route, passing through")
        return await original(self, event)

    patched._untagged_routing = True
    cls._handle_ws_event = patched
    return True


def _install() -> int:
    return sum(1 for cls in _adapter_classes() if _patch(cls))


def _describe() -> str:
    name = os.getenv("AGENT_NAME", "?")
    owners = _room_owners(name)
    mine = [room for room, agent in owners.items() if agent == name]
    return (
        f"{len(PEOPLE)} person id(s), "
        f"{len(owners)} room owner(s) declared, "
        f"this agent owns {mine or 'no rooms'}, "
        f"{THREAD_WINDOW_SECONDS:.0f}s thread window"
    )


def _install_when_loaded() -> None:
    waited = 0.0
    while True:
        if _install():
            logger.warning("untagged-routing: patched MattermostAdapter (%s)", _describe())
            return
        time.sleep(INSTALL_POLL_SECONDS)
        waited += INSTALL_POLL_SECONDS
        if waited % INSTALL_NAG_SECONDS < INSTALL_POLL_SECONDS:
            logger.warning(
                "untagged-routing: still waiting for MattermostAdapter (%.0fs). "
                "Untagged messages need an @mention until it loads.",
                waited,
            )


def register(ctx) -> None:
    # WARNING and not INFO, like mention-on-edit: this reaches into another
    # project's internals and should be visible without turning on debug.
    if not PEOPLE:
        logger.warning(
            "untagged-routing: MATTERMOST_HUMANS is empty -- doing nothing. "
            "Set it to the Mattermost user ids of the PEOPLE who may wake this "
            "agent without naming it."
        )
        return
    if _install():
        logger.warning("untagged-routing: patched MattermostAdapter (%s)", _describe())
        return
    threading.Thread(target=_install_when_loaded, name="untagged-routing", daemon=True).start()
