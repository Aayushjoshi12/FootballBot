"""One shared edit scheduler for every message edit in a Discord channel.

ROOT CAUSE (18 Nov 2026, VM journal): the panels paced at exactly
1 edit/s, assuming they were the only editors in the channel, while
the scoreboard edited the same channel OUTSIDE the slot system.
~14.6 Discord 429s/min on PATCH edits; each 429 stalled ~4.2s and
re-anchored the slot timeline, so the panels landed only ~30
edits/min instead of ~60. The 1s tick loop was never the limiter —
the shared, uncoordinated per-channel edit budget was.

So every channel-message edit in the bot goes through this module:
one worker task per channel, one edit at a time, paced 1.0s
start-to-start (Discord's sustained budget is 5 edits per 5s,
refilling 1/s). Because ALL editors share the one timeline, the
bucket never sees more than 1 edit/s from this bot and the burst
reserve stays intact — no 429s in steady state, no cascade. The
scoreboard's rare edits take a slot like any panel edit, so one
delays a single panel heartbeat by one slot instead of blowing
the budget for everybody.

Why 1.0s is safe now (and was not before): the pacing is a FLOOR
on the gap between edit starts — the average rate can only ever
be slower than the budget, never faster — and the 5-token burst
reserve absorbs scheduling jitter. What broke before was not the
1.0s number but the edits nobody accounted for: the scoreboard
outside the timeline, plus discord.py retries landing ~4s after
their slot. Both are gone: every edit is on this timeline, and a
slow (retried) edit pushes the next one out instead of piling
into the same window.

- Coalescing: at most one pending job per message — a newer
  submit for the same message replaces the pending one (fresher
  wins); the replaced submitter is told "superseded".
- Priority: user taps (a button switching the real panel from
  an alert) jump ahead of queued normal jobs — still paced,
  still inside the budget, never stuck behind a full tick of
  heartbeats.
- 429s: if Discord still rate-limits an edit (its real budget
  may differ slightly from the model, or an edit outside this
  scheduler fired), the WHOLE channel pauses until retry_after
  — the job is reported "failed" and the next tick retries it.
  A 429 never re-anchors the timeline and never cascades into
  more 429s.
- Sends (channel.send) are NOT edits and do not go through
  here; interaction/webhook edits (edit_original_response) use
  a different Discord bucket and do not go through here either.
"""
import asyncio
import collections
import logging
import time

import discord

log = logging.getLogger("edits")

# Start-to-start gap between edits in one channel: Discord's
# sustained per-channel edit budget (5 per 5s, refilling 1/s).
# A floor, not a target — a slow edit pushes the next one out,
# so the average rate is never faster than the budget.
EDIT_GAP_S = 1.0
# Never fire the next edit in the same instant as a slow edit's
# completion (a discord.py retry landing ~4s after its slot, then
# the next edit right behind it, is what used to cascade).
EDIT_MIN_AFTER_END_S = 0.15
# Longest a caller waits for its edit to land. A queued job that
# is still waiting when this expires is dropped (a stale edit
# landing later would regress the message); the next tick
# resubmits fresh. Long enough for a normal edit plus one
# internal discord.py retry, short enough to never stall the
# scoreboard loop.
DEFAULT_TIMEOUT_S = 10.0

_MISSING = object()

# Patch points for tests: a virtual clock + virtual sleep let a
# 60s measurement run in milliseconds (see TestSharedEditBudget).
_now = time.monotonic
_sleep = asyncio.sleep


class _Job:
    __slots__ = ("channel_id", "message_id", "content", "view",
                 "priority", "future", "started")

    def __init__(self, channel_id, message_id, content, view,
                 priority, future):
        self.channel_id = channel_id
        self.message_id = message_id
        self.content = content
        self.view = view
        self.priority = priority
        self.future = future
        self.started = False


class _ChannelState:
    __slots__ = ("jobs", "pending", "worker", "wake", "last_start",
                 "last_end", "blocked_until", "in_flight")

    def __init__(self):
        # Pending jobs, FIFO; a priority job is picked first.
        self.jobs = collections.deque()
        # message_id -> queued _Job (coalescing); a job that has
        # been popped for editing is no longer in here.
        self.pending = {}
        self.worker = None
        self.wake = asyncio.Event()
        self.last_start = 0.0
        self.last_end = 0.0
        # Monotonic time until which the channel is paused after
        # a 429 (0 = not paused).
        self.blocked_until = 0.0
        self.in_flight = None


_channels: dict[int, _ChannelState] = {}


def channel_blocked_until(channel_id: int) -> float:
    """Monotonic time until which the channel is paused after a
    429 (0.0 = not paused). Tests / diagnostics only."""
    state = _channels.get(int(channel_id))
    return state.blocked_until if state is not None else 0.0


def _pending_count(channel_id: int) -> int:
    """Queued + in-flight jobs in a channel (tests only)."""
    state = _channels.get(int(channel_id))
    if state is None:
        return 0
    return len(state.jobs) + (1 if state.in_flight is not None else 0)


async def reset_for_tests() -> None:
    """Cancel every channel worker and drop all scheduler state —
    test hygiene only, never called in production."""
    tasks = [s.worker for s in list(_channels.values())
             if s.worker is not None and not s.worker.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _channels.clear()


def _pop_next(state: _ChannelState) -> _Job:
    """The next job to perform: a priority job first, else the
    oldest queued one. Removes it from the pending map (it is
    in flight now — a newer submit for the same message queues
    separately instead of replacing it)."""
    for job in state.jobs:
        if job.priority:
            state.jobs.remove(job)
            if state.pending.get(job.message_id) is job:
                del state.pending[job.message_id]
            return job
    job = state.jobs.popleft()
    if state.pending.get(job.message_id) is job:
        del state.pending[job.message_id]
    return job


async def _perform(bot, channel_id: int, state: _ChannelState,
                   job: _Job) -> str:
    """The one PATCH. Returns "ok" | "gone" | "failed".
    A 429 pauses the WHOLE channel until retry_after (the bucket
    is per-channel, so any other edit in the meantime would 429
    too — that is what used to cascade)."""
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await asyncio.wait_for(
                bot.fetch_channel(channel_id), 20)
    except Exception as exc:
        log.warning("[edits] channel %s unavailable (%s)",
                    channel_id, type(exc).__name__)
        return "failed"
    try:
        message = await channel.fetch_message(job.message_id)
    except discord.NotFound:
        return "gone"
    except discord.HTTPException as exc:
        log.warning("[edits] fetch failed in %s (%s)", channel_id, exc)
        return "failed"
    try:
        if job.view is _MISSING:
            await message.edit(
                content=job.content,
                allowed_mentions=discord.AllowedMentions.none())
        else:
            await message.edit(
                content=job.content, view=job.view,
                allowed_mentions=discord.AllowedMentions.none())
    except discord.NotFound:
        return "gone"
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            retry_after = float(getattr(exc, "retry_after", None) or 60)
            state.blocked_until = _now() + retry_after
            log.warning("[edits] channel %s rate-limited; pausing %ss",
                        channel_id, retry_after)
            return "failed"
        log.warning("[edits] edit failed in %s (%s)", channel_id, exc)
        return "failed"
    return "ok"


async def _channel_worker(channel_id: int, bot) -> None:
    """One channel's edit loop: pop jobs, pace them 1.0s apart
    start-to-start, perform them. Runs forever once started;
    reset_for_tests() cancels it (tests only). Never raises —
    a failed edit is reported to its submitter, and the loop
    keeps going."""
    state = _channels[channel_id]
    try:
        while True:
            while not state.jobs:
                await state.wake.wait()
                state.wake.clear()
            job = _pop_next(state)
            now = _now()
            target = max(state.last_start + EDIT_GAP_S,
                         state.last_end + EDIT_MIN_AFTER_END_S,
                         state.blocked_until)
            if target > now:
                await _sleep(target - now)
            state.last_start = _now()
            state.in_flight = job
            job.started = True
            try:
                status = await _perform(bot, channel_id, state, job)
            finally:
                state.in_flight = None
                state.last_end = _now()
            if not job.future.done():
                job.future.set_result(
                    (status, job.content if status == "ok" else None))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[edits] channel worker failed for %s (%s)",
                    channel_id, type(exc).__name__)


async def edit_message(bot, channel_id: int, message_id: int, *,
                       content: str, view=_MISSING, priority: bool = False,
                       timeout: float = DEFAULT_TIMEOUT_S):
    """Edit a channel message through the shared per-channel
    scheduler. Returns (status, landed_content).

    - "ok": the edit landed; landed_content is the content that
      landed (always the submitted content).
    - "gone": the message was deleted; the caller decides
      (re-post once / stop quietly).
    - "failed": any other Discord error, a channel-wide 429
      pause longer than `timeout`, or the job still waiting when
      `timeout` expired (dropped, never lands stale) — the
      caller retries on the next tick.
    - "superseded": a newer submit for the same message replaced
      this job while it was still queued — the newer edit is
      the one that lands.

    `view` defaults to "no view change" (the scoreboard has no
    buttons); panels pass their WatchAlertView. `priority`
    jumps ahead of queued normal jobs (user taps only) — still
    paced, still inside the budget."""
    channel_id = int(channel_id)
    state = _channels.get(channel_id)
    if state is None:
        state = _ChannelState()
        _channels[channel_id] = state
    if state.worker is None or state.worker.done():
        state.worker = asyncio.create_task(
            _channel_worker(channel_id, bot), name=f"edits-{channel_id}")
    # A channel-wide 429 pause longer than the timeout: refuse
    # at once instead of queueing a job that would land stale.
    if state.blocked_until - _now() > timeout:
        return "failed", None
    loop = asyncio.get_running_loop()
    job = _Job(channel_id, int(message_id), content, view, priority,
               loop.create_future())
    old = state.pending.get(job.message_id)
    if old is not None and old in state.jobs:
        # Fresher wins: replace the pending job in place (keeps
        # its queue position); the old submitter is told it was
        # superseded and moves on.
        state.jobs[state.jobs.index(old)] = job
        if not old.future.done():
            old.future.set_result(("superseded", None))
    else:
        state.jobs.append(job)
    state.pending[job.message_id] = job
    state.wake.set()
    try:
        return await asyncio.wait_for(job.future, timeout)
    except asyncio.TimeoutError:
        # Still queued: drop it (a stale edit landing later
        # would regress the message); the next tick resubmits
        # fresh. Already in flight: leave it — its PATCH will
        # land, and this caller just reports "failed" this time.
        if not job.started and job in state.jobs:
            state.jobs.remove(job)
            if state.pending.get(job.message_id) is job:
                del state.pending[job.message_id]
        return "failed", None
