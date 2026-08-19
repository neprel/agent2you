"""Who owns an untagged message. Platform-neutral, on purpose.

This module knows nothing about Mattermost, Hermes, or any product name. It takes
a normalised `Message`, asks a `World` two questions about the room, and answers
"claim it" or "leave it alone". Everything platform-shaped -- websocket events,
REST paths, user-id formats, the mention syntax -- lives in the binding next door
(`__init__.py`), and a second platform would add a second binding rather than a
second copy of these rules.

That split is deliberate: the ROUTING POLICY is the thing this fleet has opinions
about and the thing that must not be re-derived per chat product, while the
transport is what we happen to run today. See ai/_.hint#untagged_routing.

The rules, in order:

1. the sender must be a PERSON. A message from another agent always needs an
   explicit mention. This is the loop guard, and it is expressed as a list of
   people rather than of agents because people are few and known -- a new agent
   must never silently become able to wake the fleet.
2. machine traffic never claims. An integration posting under a person's identity
   is not that person typing.
3. inside a thread -> whoever last spoke there that was not a person owns it.
   Exactly one agent can answer "that is me", so exactly one wakes; and it matches
   what a person means by replying in a thread. A claim goes stale after
   `thread_window_seconds`, so an old thread does not wake the agent that happened
   to close it.
4. a group room whose only non-person member is this agent -> it claims. There is
   nobody else it could be for.
5. otherwise -> the room's OWNER claims, from a fleet-wide map of room -> agent
   name with an optional `default`. Every agent is given the same map and each one
   decides independently whether it is the owner, so a single winner needs no
   coordinator and no shared state.

Nothing here guesses. There is no classifier and no keyword match on who "sounds"
right, because a wrong route is worse than no route: it spends a turn, answers
with the wrong agent's knowledge, and reads to the human as if the fleet had
decided something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

# Room shapes, as this policy needs to tell them apart. A binding maps its
# platform's own vocabulary onto these three.
DIRECT = "direct"    # one person, one agent. Never needs a mention at all.
GROUP = "group"      # a closed set of participants, no name of its own.
CHANNEL = "channel"  # a named room anybody in it may join.

# The key under which the map names the agent that takes anything unclaimed.
DEFAULT_ROOM = "default"


@dataclass(frozen=True)
class Message:
    """One incoming message, with everything the policy needs and nothing else."""

    sender: str
    room: str
    room_kind: str
    thread: str = ""          # empty for a top-level message
    machine: bool = False     # posted by an integration rather than by a person
    at_ms: int = 0            # when it arrived, for the staleness window


@dataclass(frozen=True)
class Identity:
    """This agent, as the two things the policy compares against."""

    id: str      # what the platform calls it in a message's sender field
    name: str    # what the fleet calls it, and what a room-owner map names


class World(Protocol):
    """The two questions the policy asks about a room it did not see happen."""

    async def last_non_person_in_thread(self, thread: str) -> Optional[tuple[str, int]]:
        """Sender id and timestamp of the last non-person post, or None."""

    async def non_person_members(self, room: str) -> Optional[set[str]]:
        """Sender ids of every non-person in the room, or None if unknowable."""


@dataclass(frozen=True)
class Policy:
    identity: Identity
    people: frozenset[str] = field(default_factory=frozenset)
    # room id -> agent name, plus the optional DEFAULT_ROOM key.
    room_owners: dict[str, str] = field(default_factory=dict)
    thread_window_seconds: float = 86400.0

    @property
    def active(self) -> bool:
        """False when this policy could only guess, so the binding stays inert.

        With no list of people, rule 1 cannot be evaluated, and admitting anything
        would mean agents waking on each other.
        """
        return bool(self.people)

    def owns_room(self, room: str) -> bool:
        owner = self.room_owners.get(room) or self.room_owners.get(DEFAULT_ROOM)
        return owner == self.identity.name

    async def claim(self, msg: Message, world: World) -> Optional[str]:
        """The reason this agent claims the message, or None to leave it alone.

        The reason is returned rather than a bare bool so the binding can log WHY
        a turn was spent -- the single most useful line when a route surprises
        somebody.
        """
        if not self.active:
            return None
        if msg.sender not in self.people:
            return None
        if msg.machine:
            return None
        if msg.room_kind == DIRECT:
            return None  # a one-to-one room needs no mention in the first place

        if msg.thread:
            owner = await world.last_non_person_in_thread(msg.thread)
            if owner is not None:
                who, when_ms = owner
                stale = (msg.at_ms - when_ms) / 1000.0 > self.thread_window_seconds
                if not stale:
                    return "answered last in this thread" if who == self.identity.id else None
                # A stale claim releases the thread: fall through and treat the
                # message as a new conversation in this room.

        if msg.room_kind == GROUP:
            members = await world.non_person_members(msg.room)
            if members == {self.identity.id}:
                return "sole agent in this group"

        if self.owns_room(msg.room):
            named = msg.room in self.room_owners
            return "owns this room" if named else "owns unassigned rooms"

        return None


def parse_room_owners(spec: str) -> dict[str, str]:
    """`default:assistant,<room-id>:manager` -> {"default": "assistant", ...}.

    Malformed entries are dropped rather than raised on: this comes from
    deployment configuration, and one typo must not take an agent's presence down
    with it. The binding logs what it parsed so a dropped entry is visible.
    """
    owners: dict[str, str] = {}
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        room, _, agent = entry.partition(":")
        room, agent = room.strip(), agent.strip()
        if room and agent:
            owners[room] = agent
    return owners
