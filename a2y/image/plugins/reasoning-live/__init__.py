"""Show the work while it is still work.

The gap this fills
------------------
Hermes streams the reasoning channel end to end and then does nothing with it on
a chat platform. Following one delta through its own source:

    agent/chat_completion_helpers.py:3461   reads delta.reasoning_content
                                            → agent._fire_reasoning_delta(text)
    run_agent.py:6528                       → self.reasoning_callback(text)
    cli.py:11043                            the CLI sets that callback
    gateway/run.py:5028                     the gateway sets every OTHER callback
                                            -- stream_delta, tool_progress,
                                            step, status, interim -- and not
                                            this one.

So on Mattermost `reasoning_callback` is None, `_fire_reasoning_delta` returns
without doing anything, and the text is kept only for the trajectory. That is why
acp2api's progress trace never appeared in a post: it was on the wire, it was
parsed, it was stored, and nobody was listening.

Hermes does render reasoning ONCE, at the end (gateway/run.py:18227), from
`last_reasoning` -- the last assistant message's reasoning, truncated to 15 lines.
That is a summary of a finished turn, not a view of a running one, and 15 lines of
a twenty-minute trace is the wrong 15 lines.

What this does
--------------
Subscribes. Every reasoning delta is split into complete lines and pushed onto the
gateway's own progress queue -- the queue behind the bubble Hermes already edits in
place while a turn runs. No new post, no second writer, no polling: the throttling,
the overflow-into-a-new-bubble, the ordering against streamed content, and the
`cleanup_progress` deletion at the end are all machinery that already exists and
already works. This only supplies it with the one channel the gateway forgot.

Requires `display.platforms.mattermost.thinking_progress: true`, which is what
creates that queue at all. Without it there is no bubble and this plugin stays
quiet -- see ai/config/agents/*/config.yaml.

Prose and notes are not the same thing
--------------------------------------
Two kinds of text share the reasoning channel and want opposite treatment:

* acp2api's progress NOTES -- `› npm test`, `⎿ 3 failing`, `± src/api.js +12/-4`.
  One line each, already the right shape, and the reason this plugin exists.
* the model's THINKING -- paragraphs, hundreds of lines in a long turn. Relayed
  line for line it would bury the notes it is interleaved with, and roll the
  bubble over several times a minute.

So a run of prose collapses to its first line and the rest is dropped from the
bubble. Nothing is lost that was ever going to be read here: the full reasoning is
in the transcript, and the answer is in the post.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time

logger = logging.getLogger(__name__)

# How often to repeat that it is still waiting. Repeated rather than said
# once: a single line at boot is gone from the scrollback by the time anyone
# wonders why the plugin is doing nothing.
INSTALL_NAG_SECONDS = 180.0
INSTALL_POLL_SECONDS = 2.0

# acp2api's note glyphs (acp2api/src/progress.js): a tool call starting, the
# command behind it, what it printed, a failure, a diff's line count, a plan step.
# A line beginning with one of these is a note; anything else is the model
# thinking out loud.
#
# `$` earns its place here: from acp2api 1.5.3 a command is announced as its
# DESCRIPTION -- "check what is holding the disk" -- with the command itself on a
# `$` line underneath. Left out of this tuple it would read as prose and be
# swallowed by the collapse below, which would hide the one line an operator
# actually reaches for when the answer looks wrong.
NOTE_PREFIXES = ("›", "$", "⎿", "✗", "±", "▸")

# A bubble line is read at a glance, next to everything else in the channel.
LINE_LIMIT = 200

# A ceiling on how much one turn may put in the bubble. The queue's own overflow
# handling rolls a full bubble into a new post, so an unbounded trace does not
# grow one message -- it posts a stream of them. Reached only by a turn with
# hundreds of tool calls; when it is, the trace stops and says so rather than
# trailing off, because a trace that ends silently reads like a crash.
MAX_LINES_PER_TURN = 500


def is_note(line: str) -> bool:
    """True when this line is one of acp2api's progress notes."""
    return line.startswith(NOTE_PREFIXES)


# Characters that would fight the `**_..._**` wrapper the emphasis lines are built
# from. Backticks are NOT here: a code span nests inside emphasis perfectly well,
# and the agent writes them on purpose -- "my account is not in the `docker` group"
# is its own formatting, and escaping it prints the backticks instead.
_ESCAPE = str.maketrans({c: f"\\{c}" for c in "*_"})


def emphasis(text: str) -> str:
    """Prose made safe to wrap in emphasis, keeping the author's own markup.

    `*` and `_` always go, because a stray one re-pairs with the wrapper's own
    markers and the emphasis then ends somewhere the author never intended.

    Backticks survive when they BALANCE. An odd one out would open a code span
    that swallows the rest of the line including the closing `_**`, so in that
    case every one of them is escaped -- the author clearly did not mean a span.
    """
    escaped = text.translate(_ESCAPE)
    return escaped if escaped.count("`") % 2 == 0 else escaped.replace("`", "\\`")


def code(text: str) -> str:
    """`text` as an inline code span, whatever is inside it.

    A backtick in the content would close the span early -- and shell commands are
    full of them. CommonMark's own answer is a longer fence: any run of backticks
    can be enclosed by a run one longer, with a space of padding when the content
    itself starts or ends with one.
    """
    runs = [len(r) for r in re.findall(r"`+", text)]
    fence = "`" * ((max(runs) + 1) if runs else 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def ends_quote(previous: str, current: str) -> bool:
    """Whether a blank line has to go between these two rendered lines.

    Markdown's LAZY CONTINUATION: once a blockquote opens, every following
    non-blank line is read as a continuation of its paragraph, `>` or no `>`. So a
    single line of command output swallowed the entire rest of the trace -- the
    failures, the next command, the agent's own commentary -- all of it under one
    quote bar running to the bottom of the post. Observed live, and it looks like
    the renderer went mad rather than like a missing blank line.

    Only a blank line closes it. Not needed in the other direction: a quote may
    interrupt a paragraph, so output can follow its command with nothing between.
    """
    return previous.startswith("> ") and not current.startswith("> ")


def render(line: str) -> str:
    """One trace line as Mattermost markdown.

    This is presentation, and it lives HERE rather than in acp2api, because the
    trace acp2api emits is plain text for any caller -- a CLI, a log, a different
    chat platform. Markdown is what THIS deployment's reader happens to want.

    It is also a correctness fix, not only a nicety. Rendered raw, Mattermost read
    `$ ssh … 'L=$(mktemp); …'` as inline LaTeX and printed the command as an
    italic formula, and every `_` in a path or traceback opened an emphasis span.
    A command inside a code span cannot be reinterpreted at all -- which is why
    the `$` prompt marker goes INSIDE it, leaving no bare `$` on the line to pair
    with another one.

    No HTML: Mattermost strips it from messages, so there is no font size and no
    background to work with. The hierarchy here is everything the platform offers
    -- bold, italic, monospace -- and it is enough:

        💭 **_why this step_**          the agent's own commentary
        › _why this command_           what it is about to run, and what for
        **`$ the command`**            exactly what was executed
        > what it printed              evidence, quietest thing on the line
        ✗ **_how it failed_**          bold: a failure must not read as output

    Output is a BLOCKQUOTE, and the `⎿` glyph is dropped with it: the quote bar
    already says "this belongs to the line above", it does it down the whole run
    of output lines at once, and consecutive quoted lines merge into one block
    instead of repeating a marker per line.
    """
    body = line[2:].strip() if len(line) > 2 else ""
    if line.startswith("💭 "):
        return f"💭 **_{body.translate(_ESCAPE)}_**"
    if line.startswith("› "):
        return f"› _{body.translate(_ESCAPE)}_"
    if line.startswith("$ "):
        # The `$` rides inside the span: it reads as a shell prompt, and it leaves
        # no bare dollar on the line for Mattermost to treat as maths.
        return f"**{code(line.strip())}**"
    if line.startswith("⎿ "):
        # Quoted AND monospaced. The bar is what marks it as the result of the
        # line above; the code span is what keeps it INTACT -- this is raw command
        # output, and escaping it character by character would be a guess about
        # every markdown construct a program might print. Inside a span there is
        # nothing to guess.
        return f"> {code(body)}"
    if line.startswith("✗ "):
        return f"✗ **_{body.translate(_ESCAPE)}_**"
    if line.startswith("± "):
        return f"± {code(body)}"
    if line.startswith("▸ "):
        return f"▸ _{body.translate(_ESCAPE)}_"
    # Prose the agent wrote between its tool calls, reaching the card unabridged
    # and the bubble as its first line. Same weight as 💭 above, which is what it
    # is -- the 💭 is added only where a glyph helps a line stand out in a list.
    return f"**_{line.translate(_ESCAPE)}_**"


# chat id -> the WHOLE trace of the last turn there, unabridged.
#
# Read by the trace-to-card plugin, which puts it in the finished post's card.
# Hermes' own copy of the same thing is `last_reasoning`, and it is not usable for
# that: it is the last assistant message's reasoning only, truncated to 15 lines,
# and a turn of any size loses most of itself in it -- "... (25 more lines)" on a
# three-command answer, measured.
#
# One entry per channel, overwritten by each turn: bounded by how many channels
# the agent is in, and the newest turn is the only one anybody asks about.
TRACES: dict = {}


def trace_for(chat_id) -> str | None:
    """The last completed trace for a channel, and it is CONSUMED by reading.

    Consumed on purpose: it exists to be attached to exactly one post. Left in
    place it would ride along on the next answer in the channel -- including one
    that ran no tools at all -- and a card describing work that did not happen is
    worse than no card.
    """
    turn = TRACES.pop(str(chat_id), None)
    if turn is None or not turn.full:
        return None
    # Rendered here rather than stored rendered, so `full` stays the plain record
    # and one function decides what a line looks like for both readers.
    out = []
    for line in turn.full:
        rendered = render(line)
        if out and ends_quote(out[-1], rendered):
            out.append("")
        out.append(rendered)
    return "\n".join(out)


class _Turn:
    """What has been seen so far in ONE turn.

    Per turn rather than per agent: Hermes caches an agent across a whole
    conversation, so state hung on the agent alone would carry a previous turn's
    half-line and prose flag into the next one.
    """

    __slots__ = ("ctx", "buffer", "in_prose", "lines", "full", "last")

    def __init__(self, ctx):
        self.ctx = ctx
        self.buffer = ""
        self.in_prose = False
        self.lines = 0
        # The last line put on the queue, already rendered. Kept for one reason:
        # a blockquote is closed by a BLANK LINE, and whether one is needed is a
        # fact about the pair, not about either line. See `ends_quote`.
        self.last = ""
        # Every line, as it came: no prose collapsing, no clipping, no ceiling.
        # The bubble is skimmed while the turn runs and the card is read
        # afterwards by someone checking the work, so they want opposite things.
        self.full = []


def _context_of(agent):
    """The gateway's per-turn context, reached through a callback it already set.

    `tool_progress_callback` is `TurnRunner.progress_callback`, a BOUND method, so
    its `__self__` is the runner and `_ctx` is the context for the turn now
    running -- including `progress_queue`, which is the thing worth having.

    None whenever the gateway is not the caller (the CLI, a subagent, a batch run)
    or when no progress queue was created, and in both cases there is no bubble to
    write to and nothing for this plugin to do.
    """
    cb = getattr(agent, "tool_progress_callback", None)
    runner = getattr(cb, "__self__", None)
    ctx = getattr(runner, "_ctx", None)
    return ctx if getattr(ctx, "progress_queue", None) is not None else None


def _emit(turn, line: str) -> None:
    """Record one line of trace, and put it on the queue unless it is redundant."""
    line = line.strip()
    if not line:
        return
    turn.full.append(line)

    if is_note(line):
        turn.in_prose = False
    elif turn.in_prose:
        # Still inside the same run of thinking; its first line already went out.
        return
    else:
        turn.in_prose = True
        line = f"\U0001f4ad {line}"

    if turn.lines >= MAX_LINES_PER_TURN:
        return
    turn.lines += 1
    if turn.lines == MAX_LINES_PER_TURN:
        line = "… trace truncated; the turn is still running"

    # Clipped BEFORE it is rendered, never after: a cut through finished markdown
    # lands inside a code fence or an emphasis pair and breaks the rest of the post.
    if len(line) > LINE_LIMIT:
        line = f"{line[:LINE_LIMIT - 1]}…"

    rendered = render(line)
    # Hermes joins the queue with "\n", so a leading newline here IS the blank
    # line that closes the quote above -- there is no going back to edit a line
    # already sent, and none is needed.
    if turn.last and ends_quote(turn.last, rendered):
        turn.ctx.progress_queue.put(f"\n{rendered}")
    else:
        turn.ctx.progress_queue.put(rendered)
    turn.last = rendered


def _observe(agent, text: str) -> None:
    """Accumulate deltas and emit whole lines.

    Deltas do not arrive on line boundaries -- the model's thinking streams a few
    tokens at a time -- so a delta is appended to a buffer and only completed lines
    leave it. The tail stays until its newline arrives, which means the last line
    of a turn is never emitted. That is deliberate: it is a partial line, and the
    turn is over by then anyway.
    """
    ctx = _context_of(agent)
    if ctx is None or not ctx._run_still_current():
        return

    turn = getattr(agent, "_reasoning_live", None)
    if turn is None or turn.ctx is not ctx:
        turn = _Turn(ctx)
        agent._reasoning_live = turn
        # Published at the START of the turn and filled in place, not handed over
        # at the end -- there is no end-of-turn hook here, and a turn that is
        # cancelled or fails still has a trace worth keeping.
        chat_id = getattr(getattr(ctx, "source", None), "chat_id", None)
        if chat_id is not None:
            TRACES[str(chat_id)] = turn

    turn.buffer += text
    while "\n" in turn.buffer:
        line, turn.buffer = turn.buffer.split("\n", 1)
        _emit(turn, line)


def _agent_class():
    """The AIAgent class the gateway is running, once the gateway has loaded it.

    Found by SCANNING `sys.modules`, never by importing. Importing `run_agent`
    from here pulls a very large module into the gateway process at a moment of
    this plugin's choosing, and doing it on the loader's thread took the plugin out
    of several boots with no error anywhere.

    Waiting instead costs nothing and cannot fail: the gateway imports `run_agent`
    itself the first time it runs a turn, which is also the first moment this
    plugin has anything to do.
    """
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.startswith("run_agent"):
            continue
        cls = getattr(mod, "AIAgent", None)
        if cls is not None and hasattr(cls, "_fire_reasoning_delta"):
            return cls
    return None


def _install() -> bool:
    cls = _agent_class()
    if cls is None:
        return False
    if getattr(cls, "_reasoning_live_patched", False):
        return True

    original = cls._fire_reasoning_delta

    def _fire_reasoning_delta(self, text: str) -> None:
        # Ours first, then Hermes'. Ordering barely matters -- the original is a
        # no-op on the gateway, which is the entire point -- but a display nicety
        # must never be able to change what the agent does, so the original call
        # is made unconditionally and outside our try.
        try:
            if text:
                _observe(self, text)
        except Exception:  # noqa: BLE001
            logger.debug("reasoning-live: could not relay a delta", exc_info=True)
        return original(self, text)

    cls._fire_reasoning_delta = _fire_reasoning_delta
    cls._reasoning_live_patched = True
    return True


def _install_when_loaded() -> None:
    """Wait for the gateway to load the agent module, however long that takes.

    No deadline. The module arrives with the FIRST TURN, which may be hours after
    boot on a quiet agent -- and a plugin that gave up at three minutes is a plugin
    that is simply not there, which is exactly the failure this had. A daemon
    thread doing one dict lookup every couple of seconds costs nothing and dies
    with the process.
    """
    waited = 0.0
    while True:
        try:
            if _install():
                logger.warning("reasoning-live: patched AIAgent._fire_reasoning_delta")
                return
        except Exception:  # noqa: BLE001
            logger.debug("reasoning-live: install attempt failed", exc_info=True)
        time.sleep(INSTALL_POLL_SECONDS)
        waited += INSTALL_POLL_SECONDS
        if waited % INSTALL_NAG_SECONDS < INSTALL_POLL_SECONDS:
            logger.warning(
                "reasoning-live: still waiting for the gateway to load run_agent "
                "(%.0fs). The trace stays quiet until it does.", waited,
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
    threading.Thread(target=_install_when_loaded, name='reasoning-live', daemon=True).start()
