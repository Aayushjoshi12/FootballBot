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

UPDATE (18 Nov 2026, VM journal, scheduler live at a0a9c3f2):
the 1.0s premise was FALSIFIED in production — Discord 429'd
nearly every edit from minute one at a steady 1 edit/s, and the
scheduler made things WORSE (1–3 edits/min, scoreboard landing
zero) instead of better. The mechanism: discord.py 2.4.0 retries
a 429 internally up to 5 times, sleeping retry_after (~3–5s)
each time (http.py: `for tries in range(5)`), and only raises
HTTPException after the 5th consecutive 429 — so the 429 branch
below almost never ran, blocked_until stayed 0.0, no "[edits]"
line ever appeared, and one blocked edit stalled the serial
worker for ~20s inside hidden retries (the 166 journal warnings
were mostly repeat retries of far fewer logical edits). The
scoreboard's 2.0s timeout was shorter than ONE retry sleep, so
every scoreboard edit timed out and was discarded, yet
resubmitted on the next tick into the same drained bucket.

So the PATCH is done directly here (own aiohttp session, Bot
token) instead of through discord.py's HTTPClient: a 429 comes
back at once with its exact retry_after, no hidden retries, no
worker stall, exactly one warning per logical edit. The gap
ADAPTS instead of assuming 1/s is safe: it grows ×1.5 on a 429
(cap 5s) and eases back down only after 10 consecutive clean
edits, so it settles just above the channel's real sustainable
rate instead of oscillating around it. A rate-limited job is
retried ONCE by the worker after the pause, never hammered,
and a 429 pauses the whole channel — the design intent that
discord.py's retries had made dead code.

- Coalescing: at most one pending job per message — a newer
  submit for the same message replaces the pending one (fresher
  wins); the replaced submitter is told "superseded".
- Priority: user taps (a button switching the real panel from
  an alert) jump ahead of queued normal jobs — still paced,
  still inside the budget, never stuck behind a full tick of
  heartbeats.
- 429s: the raw PATCH sees the FIRST 429 (exact retry_after
  from the response body — no discord.py retry loop in the
  way). The WHOLE channel pauses until retry_after, the gap
  doubles (adaptive backoff), and the worker retries that job
  ONCE after the pause. A second 429 reports "failed" and the
  caller retries on its next tick. A 429 never re-anchors the
  timeline and never cascades into more 429s.
- Sends (channel.send) are NOT edits and do not go through
  here; interaction/webhook edits (edit_original_response) use
  a different Discord bucket and do not go through here either.
"""
import asyncio
import collections
import logging
import time

import aiohttp
import discord

log = logging.getLogger("edits")

# Start-to-start gap between edits in one channel: Discord's
# sustained per-channel edit budget (5 per 5s, refilling 1/s).
# A STARTING point, not a promise — the gap adapts per channel
# (see _ChannelState.gap): it doubles on a 429 and decays back
# toward this floor on success, because production proved 1.0s
# is not always safe (see the module docstring).
EDIT_GAP_S = 1.0
# Widest the adaptive gap ever grows: even a channel whose real
# budget is far below the model still lands an edit every 5s.
MAX_GAP_S = 5.0
# Never fire the next edit in the same instant as a slow edit's
# completion (a discord.py retry landing ~4s after its slot, then
# the next edit right behind it, is what used to cascade).
EDIT_MIN_AFTER_END_S = 0.15
# Longest the worker waits out a 429 pause to retry a job once:
# retry_after in production runs ~3–5s, so a longer pause is
# left to the caller's next tick instead of stalling the queue.
_RETRY_WAIT_CAP_S = 10.0
# Longest a caller waits for its edit to land. A queued job that
# is still waiting when this expires is dropped (a stale edit
# landing later would regress the message); the next tick
# resubmits fresh. Long enough for a normal edit plus one
# internal discord.py retry, short enough to never stall the
# scoreboard loop.
DEFAULT_TIMEOUT_S = 10.0
# Discord API base for the raw edit PATCH (see _raw_patch).
_API_BASE = "https://discord.com/api/v10"

_MISSING = object()

# Patch points for tests: a virtual clock + virtual sleep let a
# 60s measurement run in milliseconds (see TestSharedEditBudget).
_now = time.monotonic
_sleep = asyncio.sleep


class _Job:
    __slots__ = ("channel_id", "message_id", "content", "view",
                 "priority", "future", "started", "retried")

    def __init__(self, channel_id, message_id, content, view,
                 priority, future):
        self.channel_id = channel_id
        self.message_id = message_id
        self.content = content
        self.view = view
        self.priority = priority
        self.future = future
        self.started = False
        # True once the worker has retried this job after a 429
        # pause — a second 429 reports "failed" instead of
        # retrying forever.
        self.retried = False


class _ChannelState:
    __slots__ = ("jobs", "pending", "worker", "wake", "last_start",
                 "last_end", "blocked_until", "in_flight", "gap",
                 "ok_streak")

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
        # Current start-to-start pacing for this channel: starts
        # at EDIT_GAP_S, grows ×1.5 on a 429 (cap MAX_GAP_S),
        # decays back toward EDIT_GAP_S only after a run of
        # successes (see ok_streak). The channel's real
        # sustainable rate is measured, not assumed.
        self.gap = EDIT_GAP_S
        # Consecutive successful edits since the last 429: the
        # gap only decays after a run of these, so it settles
        # just above the sustainable rate instead of decaying
        # back down and 429ing again every few seconds.
        self.ok_streak = 0


_channels: dict[int, _ChannelState] = {}


def channel_blocked_until(channel_id: int) -> float:
    """Monotonic time until which the channel is paused after a
    429 (0.0 = not paused). Tests / diagnostics only."""
    state = _channels.get(int(channel_id))
    return state.blocked_until if state is not None else 0.0


def channel_gap(channel_id: int) -> float:
    """Current start-to-start pacing for a channel (EDIT_GAP_S
    when no 429 has ever adapted it). Tests / diagnostics only."""
    state = _channels.get(int(channel_id))
    return state.gap if state is not None else EDIT_GAP_S


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
    """The one PATCH. Returns "ok" | "gone" | "failed" |
    "rate_limited". The worker retries a "rate_limited" job
    once after the pause; anything else is final for this tick.

    The PATCH is raw (see _raw_patch), NOT message.edit:
    discord.py retries a 429 up to 5 times inside the call, so
    the scheduler never saw the FIRST 429 and the worker stalled
    ~20s in hidden retries. Here a 429 is observed on its first
    occurrence: the whole channel pauses until retry_after, the
    gap doubles, and the job is retried once after the pause.
    A global 429 pauses every channel, not just this one."""
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
    payload = {"content": job.content,
               "allowed_mentions": {"parse": []}}
    if job.view is not _MISSING:
        try:
            payload["components"] = ([] if job.view is None
                                     else job.view.to_components())
        except Exception as exc:
            log.warning("[edits] view serialise failed in %s (%s)",
                        channel_id, type(exc).__name__)
            return "failed"
    msg_state = getattr(message, "_state", None)
    if job.view is not _MISSING and msg_state is not None:
        # Mirror Message.edit: drop the message's view tracking
        # while the edit is in flight, then re-register the new
        # view below once the PATCH has landed.
        try:
            msg_state.prevent_view_updates_for(job.message_id)
        except Exception:
            pass
    status, data = (0, None)
    try:
        status, data = await _raw_patch(bot, message, payload,
                                        view=job.view)
    except Exception as exc:
        log.warning("[edits] patch failed in %s (%s)",
                    channel_id, type(exc).__name__)
        return "failed"
    if status == 200:
        # Success: count the streak; only after 10 clean edits
        # in a row does the gap ease back down (×0.95). A
        # per-success decay re-probed the budget every few
        # seconds and 429'd again — the gap must settle just
        # above the sustainable rate, not oscillate around it.
        state.ok_streak += 1
        if state.ok_streak >= 10:
            state.ok_streak = 0
            state.gap = max(EDIT_GAP_S, state.gap * 0.95)
        if (job.view is not _MISSING and job.view is not None
                and not job.view.is_finished()
                and msg_state is not None):
            try:
                msg_state.store_view(job.view, job.message_id)
            except Exception:
                pass
        return "ok"
    if status == 404:
        return "gone"
    if status == 429:
        retry_after = 0.0
        if data:
            try:
                retry_after = float(data.get("retry_after") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
        if retry_after <= 0:
            retry_after = 5.0
        state.blocked_until = _now() + retry_after
        state.gap = min(state.gap * 1.5, MAX_GAP_S)
        state.ok_streak = 0
        if data and data.get("global"):
            for other in _channels.values():
                other.blocked_until = state.blocked_until
                other.gap = min(other.gap * 1.5, MAX_GAP_S)
                other.ok_streak = 0
        log.warning("[edits] channel %s rate-limited; pausing %ss "
                    "(gap now %.1fs)",
                    channel_id, retry_after, state.gap)
        return "rate_limited"
    if status == 0:
        # Transport failure (already logged by _raw_patch).
        return "failed"
    log.warning("[edits] edit failed in %s (HTTP %s)", channel_id, status)
    return "failed"


_SESSION: aiohttp.ClientSession | None = None


async def _get_session(bot) -> aiohttp.ClientSession:
    """The scheduler's own HTTP session (Bot token). Separate
    from discord.py's session on purpose: the raw PATCH must
    never pass through discord.py's retry loop. Created lazily
    on first use; left open for the bot's lifetime (the process
    exits on shutdown, there is no graceful session close)."""
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        token = getattr(getattr(bot, "http", None), "token", None)
        if not token:
            raise RuntimeError(
                "channel_edits: no bot token for raw edit PATCH")
        _SESSION = aiohttp.ClientSession(
            headers={"Authorization": f"Bot {token}"})
    return _SESSION


async def _raw_patch(bot, message, payload: dict,
                     view=_MISSING) -> tuple[int, dict | None]:
    """The one Discord PATCH, done directly (not via discord.py's
    HTTPClient). discord.py retries a 429 up to 5 times inside
    the call, sleeping retry_after each time, and only raises
    after the 5th — so the scheduler never saw the FIRST 429
    and the worker stalled ~20s in hidden retries. Here a 429
    comes back at once with its exact retry_after, and the
    caller (see _perform) decides what to do with it.
    Returns (HTTP status, response JSON dict or None).

    No token (tests / offline — production always has one):
    edit through message.edit instead, mapping its exceptions
    onto the exact same contract, so the scheduler's 429
    handling is exercised identically either way. `view` is
    passed through to that fallback edit; the real transport
    ignores it — the view is already serialised into
    payload["components"]."""
    token = getattr(getattr(bot, "http", None), "token", None)
    if token is None:
        kwargs = {"content": payload.get("content"),
                  "allowed_mentions": discord.AllowedMentions.none()}
        if view is not _MISSING:
            kwargs["view"] = view
        try:
            await message.edit(**kwargs)
        except discord.NotFound:
            return 404, None
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            if status == 429:
                return 429, {"retry_after":
                             getattr(exc, "retry_after", 1.0)}
            return status, None
        return 200, {}
    session = await _get_session(bot)
    url = (f"{_API_BASE}/channels/{message.channel.id}"
           f"/messages/{message.id}")
    try:
        async with session.patch(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            return resp.status, (data if isinstance(data, dict)
                                 else None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("[edits] patch transport failed (%s)",
                    type(exc).__name__)
        return 0, None
    return "ok"


async def _channel_worker(channel_id: int, bot) -> None:
    """One channel's edit loop: pop jobs, pace them `state.gap`
    apart start-to-start, perform them. Runs forever once started;
    reset_for_tests() cancels it (tests only). Never raises —
    a failed edit is reported to its submitter, and the loop
    keeps going.

    A rate-limited job is retried ONCE after the pause (the
    pause is bounded by _RETRY_WAIT_CAP_S — a longer pause is
    left to the caller's next tick instead of stalling the
    queue). A second 429 is reported "failed" like any other
    failure, so the documented statuses stay "ok" | "gone" |
    "failed" | "superseded"."""
    state = _channels[channel_id]
    log.info("[edits] channel %s edit worker started", channel_id)
    try:
        while True:
            while not state.jobs:
                await state.wake.wait()
                state.wake.clear()
            job = _pop_next(state)
            now = _now()
            target = max(state.last_start + state.gap,
                         state.last_end + EDIT_MIN_AFTER_END_S,
                         state.blocked_until)
            if target > now:
                await _sleep(target - now)
            state.last_start = _now()
            state.in_flight = job
            job.started = True
            try:
                status = await _perform(bot, channel_id, state, job)
                if status == "rate_limited" and not job.retried:
                    job.retried = True
                    delay = state.blocked_until - _now()
                    if 0 < delay <= _RETRY_WAIT_CAP_S:
                        await _sleep(delay)
                        state.last_start = _now()
                        status = await _perform(
                            bot, channel_id, state, job)
                if status == "rate_limited":
                    status = "failed"
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
      caller retries on the next tick. A 429 is first retried
      ONCE by the worker after the pause, so "failed" after a
      429 means the retry also failed or the pause outlasted
      the retry cap.
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
