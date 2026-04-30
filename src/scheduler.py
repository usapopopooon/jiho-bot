"""Time-signal scheduler.

Each connected guild has an interval (in minutes — 60, 30 or 10) that
selects which boundaries it gets a cue on. The scheduler wakes at the
**finest** active interval and at fire time routes to the guilds whose
interval includes that boundary:

- minute 0  → all guilds (every interval includes ``0 % i == 0``)
- minute 30 → guilds with interval 30 or 10
- minute 10/20/40/50 → guilds with interval 10

A single asyncio task drives all guilds. The loop recomputes the wake
cadence each iteration so a fresh ``/setting`` change takes effect at
the next tick rather than waiting for the next hour.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.voice_manager import VoiceManager

logger = logging.getLogger(__name__)

# Wake cadence is the smallest minute granularity any guild can pick.
# The scheduler always snaps fire times to a multiple of this when it
# decides which clip to play.
_TICK_GRANULARITY = 10


def seconds_until_next_tick(now: datetime, interval_minutes: int) -> float:
    """Seconds from ``now`` until the next firing boundary for ``interval``.

    ``interval_minutes`` must divide 60 (10/12/15/20/30) or be 60 itself.
    Pure function so it's trivial to unit-test. Returns a strictly
    positive value: when ``now`` lands exactly on a boundary we still
    wait a full interval rather than firing twice.
    """
    if interval_minutes <= 0 or 60 % interval_minutes != 0:
        raise ValueError(
            f"interval must be a positive divisor of 60, got {interval_minutes}"
        )

    if interval_minutes >= 60:
        target = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        # Round the current minute down to its interval bucket, then add
        # one interval. If that pushes us past :59 we cross the hour.
        bucket = (now.minute // interval_minutes) * interval_minutes
        next_minute = bucket + interval_minutes
        if next_minute >= 60:
            target = (now + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
        else:
            target = now.replace(minute=next_minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(minutes=interval_minutes)
    return (target - now).total_seconds()


def intended_minute(fire_at: datetime) -> int:
    """Snap ``fire_at.minute`` to the nearest 10-min mark we fired on.

    Wakeup drift is always positive (a few ms late), so flooring to the
    last 10-min boundary gives the boundary we *intended* to fire on.
    """
    return (fire_at.minute // _TICK_GRANULARITY) * _TICK_GRANULARITY


def clip_name_for(fire_at: datetime) -> str:
    """Clip name for the boundary we just fired on.

    ``HH:00`` → ``"<hour>"``; ``HH:MM`` (MM in 10/20/30/40/50) →
    ``"<hour>_<MM>"``.
    """
    minute = intended_minute(fire_at)
    if minute == 0:
        return str(fire_at.hour)
    return f"{fire_at.hour}_{minute}"


class JihoScheduler:
    """Single-loop, multi-cadence time-signal scheduler."""

    def __init__(
        self,
        voice_manager: VoiceManager,
        timezone: ZoneInfo,
    ) -> None:
        self._voice_manager = voice_manager
        self._tz = timezone
        self._task: asyncio.Task[None] | None = None
        # Set whenever something happens that could change the wait
        # cadence (``/setting`` change, connect, disconnect). The run
        # loop wakes early, clears it, and recomputes — without this,
        # a fresh ``/setting`` sleeps through up to one full hour
        # before taking effect.
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Ask the run loop to recompute its sleep deadline now.

        Idempotent and cheap: callers don't need to track whether the
        scheduler actually changed cadence as a result.
        """
        self._wake.set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="jiho-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        logger.info("jiho scheduler started tz=%s", self._tz.key)
        try:
            while True:
                now = datetime.now(self._tz)
                interval = self._voice_manager.min_interval()
                wait = seconds_until_next_tick(now, interval)
                logger.debug("jiho sleep %.1fs (interval=%d)", wait, interval)
                # Sleep until either the boundary fires (TimeoutError) or
                # ``wake()`` was called from outside (Event set). On wake
                # we just recompute — *don't* fire, because the wait
                # might have been shortened. Without this, /setting
                # changes mid-sleep wait through the previous interval.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                    self._wake.clear()
                    logger.debug("jiho woken — recomputing")
                    continue
                except TimeoutError:
                    pass
                fire_at = datetime.now(self._tz)
                await self._fire(fire_at)
        except asyncio.CancelledError:
            logger.info("jiho scheduler cancelled")
            raise

    async def _fire(self, fire_at: datetime) -> None:
        clip = clip_name_for(fire_at)
        minute = intended_minute(fire_at)
        guild_ids = self._voice_manager.eligible_at(minute)
        if not guild_ids:
            logger.info(
                "jiho fire clip=%s skipped (no eligible guilds, minute=%d)",
                clip,
                minute,
            )
            return
        logger.info("jiho fire clip=%s guilds=%d at=%s", clip, len(guild_ids), fire_at)
        await asyncio.gather(
            *(self._voice_manager.play_clip(gid, clip) for gid in guild_ids),
            return_exceptions=True,
        )


__all__ = [
    "JihoScheduler",
    "clip_name_for",
    "intended_minute",
    "seconds_until_next_tick",
]
