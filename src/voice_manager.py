"""Per-bot voice connection + clip playback.

Discord allows a bot **at most one voice connection per guild**. This
manager keeps connection state keyed by guild id and serialises plays so
a fresh clip can't talk over a still-playing one.

Callers pass a clip name (``"0"`` / ``"13"`` / etc.) and we resolve it
against ``constants.VOICES_DIR``. Missing files are logged and skipped
— the time signal is best-effort, never crashing the scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import discord

from src.constants import VOICES_DIR

logger = logging.getLogger(__name__)


class VoiceManager:
    """Owns voice connections for one bot instance."""

    # Default interval (in minutes) when a guild hasn't picked one — fire
    # at every :00 and :30 (the half-hour cadence). Users can switch to
    # 60 (hourly) or 10 (every-10-min) via ``/setting``.
    DEFAULT_INTERVAL = 30

    def __init__(self, voices_dir: Path = VOICES_DIR) -> None:
        self._voices_dir = voices_dir
        self._connections: dict[int, discord.VoiceClient] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # Per-guild firing cadence in minutes. 60 = hour-only, 30 =
        # +half-hour, 10 = every 10 minutes. The interval must divide 60.
        # Set via ``/setting``; persists across disconnect/reconnect
        # within the same bot process. Absent guilds default to
        # ``DEFAULT_INTERVAL`` so callers can read without populating.
        self._interval: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def is_connected(self, guild_id: int) -> bool:
        client = self._connections.get(guild_id)
        return client is not None and client.is_connected()

    def connected_guild_ids(self) -> list[int]:
        return [
            gid
            for gid, c in self._connections.items()
            if c is not None and c.is_connected()
        ]

    # ------------------------------------------------------------------
    # Per-guild firing interval
    # ------------------------------------------------------------------

    def set_interval(self, guild_id: int, minutes: int) -> None:
        # Guard the zero/negative cases first — ``60 % 0`` would raise
        # ``ZeroDivisionError`` instead of the friendlier ValueError below.
        if minutes <= 0 or 60 % minutes != 0:
            raise ValueError(
                f"interval must be a positive divisor of 60 (got {minutes}); "
                "valid: 10/15/20/30/60"
            )
        if minutes == self.DEFAULT_INTERVAL:
            self._interval.pop(guild_id, None)
        else:
            self._interval[guild_id] = minutes

    def get_interval(self, guild_id: int) -> int:
        return self._interval.get(guild_id, self.DEFAULT_INTERVAL)

    def min_interval(self) -> int:
        """Smallest interval among connected guilds — drives wake cadence.

        With no guilds connected we still return ``DEFAULT_INTERVAL`` (the
        scheduler will sleep an hour and check again, which is fine).
        """
        intervals = [
            self._interval.get(gid, self.DEFAULT_INTERVAL)
            for gid in self.connected_guild_ids()
        ]
        return min(intervals) if intervals else self.DEFAULT_INTERVAL

    def eligible_at(self, intended_minute: int) -> list[int]:
        """Connected guilds whose interval includes ``intended_minute``.

        ``intended_minute`` is a multiple of 10 in ``[0, 60)``. A guild's
        interval ``i`` matches when ``intended_minute % i == 0`` —
        i.e. interval 60 fires only at 0, interval 30 at 0/30, interval
        10 at every 10-min mark.
        """
        return [
            gid
            for gid in self.connected_guild_ids()
            if intended_minute % self._interval.get(gid, self.DEFAULT_INTERVAL) == 0
        ]

    async def connect(self, voice_channel: discord.VoiceChannel) -> bool:
        guild_id = voice_channel.guild.id
        lock = self._lock_for(guild_id)
        async with lock:
            existing = self._connections.get(guild_id)
            if existing is not None and existing.is_connected():
                if existing.channel.id != voice_channel.id:
                    try:
                        await existing.move_to(voice_channel)
                    except discord.HTTPException:
                        logger.warning(
                            "voice.move failed guild=%s ch=%s",
                            guild_id,
                            voice_channel.id,
                        )
                        return False
                return True
            try:
                client: discord.VoiceClient = await voice_channel.connect(
                    self_deaf=True, timeout=15.0
                )
            except discord.ClientException:
                logger.warning(
                    "voice.connect duplicate guild=%s ch=%s",
                    guild_id,
                    voice_channel.id,
                )
                await self._kill_leftover_voice_client(voice_channel.guild)
                return False
            except (discord.HTTPException, TimeoutError) as e:
                logger.warning(
                    "voice.connect failed guild=%s ch=%s err=%r",
                    guild_id,
                    voice_channel.id,
                    e,
                )
                await self._kill_leftover_voice_client(voice_channel.guild)
                return False
            self._connections[guild_id] = client
            return True

    async def _kill_leftover_voice_client(self, guild: discord.Guild) -> None:
        leftover = guild.voice_client
        if leftover is None:
            return
        with contextlib.suppress(Exception):
            await leftover.disconnect(force=True)

    async def disconnect(self, guild_id: int) -> None:
        lock = self._lock_for(guild_id)
        async with lock:
            client = self._connections.pop(guild_id, None)
            # Interval is intentionally **not** cleared here — it's a
            # per-guild preference owned by ``/setting`` and survives
            # disconnect/reconnect cycles. Bot restart will lose it
            # along with all other in-memory state, that's expected.
            if client is None:
                return
            try:
                await client.disconnect(force=False)
            except discord.HTTPException:
                logger.debug("voice.disconnect failed guild=%s", guild_id)

    async def disconnect_all(self) -> None:
        for guild_id in list(self._connections):
            await self.disconnect(guild_id)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    async def play_clip(self, guild_id: int, clip_name: str) -> bool:
        client = self._connections.get(guild_id)
        if client is None or not client.is_connected():
            return False

        path = self._voices_dir / f"{clip_name}.wav"
        if not path.is_file():
            logger.warning("voice.clip missing path=%s", path)
            return False

        lock = self._lock_for(guild_id)
        async with lock:
            client = self._connections.get(guild_id)
            if client is None or not client.is_connected():
                return False
            if client.is_playing():
                client.stop()

            done = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _after(error: Exception | None) -> None:
                if error is not None:
                    logger.warning(
                        "voice.play errored guild=%s clip=%s err=%s",
                        guild_id,
                        clip_name,
                        error,
                    )
                loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegPCMAudio(str(path))
            try:
                client.play(source, after=_after)
            except discord.ClientException:
                logger.warning(
                    "voice.play rejected guild=%s clip=%s", guild_id, clip_name
                )
                return False

            await done.wait()
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
        return lock


__all__ = ["VoiceManager"]
