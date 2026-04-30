"""Discord bot — ``/jiho`` toggle + ``/setting`` interval picker."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.config import Settings
from src.scheduler import JihoScheduler
from src.voice_manager import VoiceManager

logger = logging.getLogger(__name__)

# Allowed firing cadences in minutes. Each must divide 60 — the
# scheduler / voice_manager assume that. 30 is listed first because it's
# the default (also matches ``VoiceManager.DEFAULT_INTERVAL``).
_INTERVAL_OPTIONS: list[tuple[int, str]] = [
    (30, "毎時0分・30分 (30分ごと) — 既定"),
    (60, "毎時0分のみ (60分ごと)"),
    (10, "10分ごと"),
]


def _interval_label(minutes: int) -> str:
    for value, label in _INTERVAL_OPTIONS:
        if value == minutes:
            return label
    return f"{minutes}分ごと"


class _IntervalSelect(discord.ui.Select["_IntervalSettingView"]):
    """Dropdown for ``/setting`` — picks the per-guild firing interval."""

    def __init__(
        self,
        voice_manager: VoiceManager,
        scheduler: JihoScheduler,
        guild_id: int,
        current: int,
    ):
        options = [
            discord.SelectOption(
                label=label,
                value=str(value),
                default=(value == current),
            )
            for value, label in _INTERVAL_OPTIONS
        ]
        super().__init__(
            placeholder="時報を流す間隔を選択",
            min_values=1,
            max_values=1,
            options=options,
        )
        self._voice_manager = voice_manager
        self._scheduler = scheduler
        self._guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Discord enforces ``min_values=1`` so ``self.values`` is always
        # populated, but be defensive: an unexpected empty list would
        # otherwise crash with IndexError instead of a clean message.
        if not self.values:
            await interaction.response.send_message(
                "選択が確認できませんでした。もう一度お試しください。",
                ephemeral=True,
            )
            return
        minutes = int(self.values[0])
        try:
            self._voice_manager.set_interval(self._guild_id, minutes)
        except ValueError as e:
            # ``set_interval`` rejects non-divisors of 60. Should be
            # impossible via the dropdown (we control the options) but a
            # malformed component payload would land here.
            logger.warning(
                "setting reject guild=%s minutes=%s err=%s", self._guild_id, minutes, e
            )
            await interaction.response.send_message("無効な値です。", ephemeral=True)
            return
        # Wake the scheduler so the new cadence takes effect on the next
        # boundary instead of waiting out the previous (longer) sleep.
        self._scheduler.wake()
        await interaction.response.send_message(
            f"時報の間隔を「{_interval_label(minutes)}」に変更しました。",
            ephemeral=True,
        )
        # Cue the change in VC if connected. ``play_clip`` is best-effort
        # — a missing wav (e.g. user re-deployed without re-rendering)
        # logs a warning and is a no-op instead of crashing the callback.
        if self._voice_manager.is_connected(self._guild_id):
            await self._voice_manager.play_clip(self._guild_id, f"interval_{minutes}")


class _IntervalSettingView(discord.ui.View):
    """Container for the dropdown so the timeout disables the select."""

    def __init__(
        self,
        voice_manager: VoiceManager,
        scheduler: JihoScheduler,
        guild_id: int,
        current: int,
    ):
        # 120s timeout: long enough to read the message and click, short
        # enough that abandoned panels don't linger as live components.
        super().__init__(timeout=120)
        self.add_item(_IntervalSelect(voice_manager, scheduler, guild_id, current))


class JihoBot(commands.Bot):
    """Two-command bot.

    - ``/jiho``   — toggle the bot's presence in the invoker's voice channel.
    - ``/setting`` — open a dropdown to pick this guild's firing interval
                    (30 / 60 / 10 minutes; 30 is the default). Persists
                    across disconnects.
    """

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        # voice_states is required for ``Member.voice`` to be populated
        # without an extra fetch — that's how we discover the user's VC.
        intents.voice_states = True
        super().__init__(
            command_prefix="!jiho-unused!",
            intents=intents,
            activity=discord.Game(name="/jiho"),
        )
        self._settings = settings
        self.voice_manager: VoiceManager = VoiceManager()
        self.scheduler: JihoScheduler = JihoScheduler(
            self.voice_manager,
            settings.timezone,
        )

    async def setup_hook(self) -> None:
        self.tree.add_command(
            app_commands.Command(
                name="jiho",
                description="ボイスチャンネルへの接続/切断をトグルします",
                callback=self._cmd_jiho,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="setting",
                description="時報の間隔を設定します (毎時/30分/10分)",
                callback=self._cmd_setting,
            )
        )

        if self._settings.discord_guild_ids:
            for gid in self._settings.discord_guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("synced commands to guild %d", gid)
        else:
            await self.tree.sync()
            logger.info("synced commands globally")

        self.scheduler.start()

    async def on_ready(self) -> None:
        logger.info("bot ready as %s (guilds=%d)", self.user, len(self.guilds))

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-disconnect when the last human leaves the bot's VC.

        Without this, the bot would broadcast to an empty channel forever
        after everyone goes to bed. We don't say goodbye — just leave
        quietly. Triggered for the *human's* state change, not the bot's,
        so we early-return on our own id.
        """
        if self.user is None or member.id == self.user.id:
            return
        if not self.voice_manager.is_connected(member.guild.id):
            return

        # Find the channel the bot is sitting in for this guild. Reading
        # via ``member.guild.voice_client`` is authoritative — our own
        # ``VoiceManager`` only caches a wrapper, which can lag the live
        # voice state during reconnects.
        voice_client = member.guild.voice_client
        bot_channel = getattr(voice_client, "channel", None)
        if bot_channel is None:
            return

        # We only care when someone *left* (or moved away from) our
        # channel. Joining, mute toggles, camera flips all leave
        # ``before.channel == after.channel`` and don't matter here.
        if before.channel != bot_channel or after.channel == bot_channel:
            return

        humans_left = [m for m in bot_channel.members if not m.bot]
        if humans_left:
            return

        logger.info(
            "auto-disconnect: last human left guild=%s channel=%s",
            member.guild.id,
            bot_channel.id,
        )
        await self.voice_manager.disconnect(member.guild.id)
        # Disconnect can raise min_interval (this guild may have been
        # the only 10-min subscriber). Wake the scheduler so it stops
        # waking too often unnecessarily.
        self.scheduler.wake()

    async def close(self) -> None:
        await self.scheduler.stop()
        await self.voice_manager.disconnect_all()
        await super().close()

    # ------------------------------------------------------------------
    # /jiho — connect/disconnect toggle
    # ------------------------------------------------------------------

    async def _cmd_jiho(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使えます。", ephemeral=True
            )
            return

        if self.voice_manager.is_connected(guild.id):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.voice_manager.disconnect(guild.id)
            # If this was the only fine-cadence guild, the scheduler can
            # now sleep longer. ``wake()`` is cheap and idempotent.
            self.scheduler.wake()
            await interaction.followup.send("切断しました。", ephemeral=True)
            return

        # Connect — invoker must be in a voice channel.
        member = interaction.user
        voice_state = getattr(member, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "先にボイスチャンネルに参加してください。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await self.voice_manager.connect(channel)
        if not ok:
            await interaction.followup.send(
                "接続に失敗しました。少し待ってから再度お試しください。",
                ephemeral=True,
            )
            return
        # Connecting can lower min_interval (e.g. this guild had 10-min
        # set previously and was disconnected). Recompute now.
        self.scheduler.wake()
        current = self.voice_manager.get_interval(guild.id)
        await interaction.followup.send(
            f"#{channel.name} に接続しました。現在の間隔: "
            f"「{_interval_label(current)}」 (`/setting` で変更できます)",
            ephemeral=True,
        )
        # Greet the channel after the followup is on its way so the
        # spinner doesn't hang while audio plays. ``play_clip`` is
        # best-effort and short (~1.5s for the connected cue).
        await self.voice_manager.play_clip(guild.id, "connected")

    # ------------------------------------------------------------------
    # /setting — open the interval-picker dropdown
    # ------------------------------------------------------------------

    async def _cmd_setting(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使えます。", ephemeral=True
            )
            return
        current = self.voice_manager.get_interval(guild.id)
        view = _IntervalSettingView(
            self.voice_manager, self.scheduler, guild.id, current
        )
        await interaction.response.send_message(
            f"現在の時報の間隔: 「{_interval_label(current)}」\n"
            "下のメニューから選択してください。",
            view=view,
            ephemeral=True,
        )
