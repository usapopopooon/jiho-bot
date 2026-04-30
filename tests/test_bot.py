"""Bot-level tests.

These exercise ``_IntervalSelect.callback`` and
``JihoBot.on_voice_state_update`` directly with mocked dependencies so
we don't need a live Discord connection.

Why not test ``JihoBot._cmd_jiho``? Constructing a ``JihoBot`` is fine
(it doesn't connect at __init__) but the ``Interaction`` object the
slash-callback consumes has many internal hooks; the marginal value
over manual integration testing in Discord isn't worth the mock
surface. The ``on_voice_state_update`` path below is mockable enough
that we cover it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot import JihoBot, _IntervalSelect
from src.config import Settings


def _make_interaction() -> MagicMock:
    """Minimal Interaction stub — only the attributes the callback touches."""
    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_select_with_choice(
    voice_manager: MagicMock, scheduler: MagicMock, choice: str
) -> _IntervalSelect:
    """Build a Select and inject the user's chosen value.

    discord.py's ``Select.values`` falls back to ``self._values`` when the
    ``selected_values`` contextvar isn't set (i.e. outside a real
    interaction). Setting ``_values`` directly is the canonical test hook.
    """
    select = _IntervalSelect(voice_manager, scheduler, guild_id=42, current=30)
    select._values = [choice]
    return select


@pytest.mark.asyncio
async def test_select_callback_wakes_scheduler_on_success() -> None:
    """Happy path: set_interval succeeded → wake() must fire so the
    scheduler picks up the new cadence at the next boundary instead of
    sleeping out the previous one."""
    vm = MagicMock()
    vm.set_interval = MagicMock()
    vm.is_connected = MagicMock(return_value=False)
    vm.play_clip = AsyncMock()
    sched = MagicMock()
    sched.wake = MagicMock()

    select = _make_select_with_choice(vm, sched, "10")
    await select.callback(_make_interaction())

    vm.set_interval.assert_called_once_with(42, 10)
    sched.wake.assert_called_once()


@pytest.mark.asyncio
async def test_select_callback_does_not_wake_on_invalid_value() -> None:
    """ValueError from set_interval → no schedule change → no wake.

    Defensive: if the dropdown payload is somehow malformed, the
    scheduler's deadline is still valid and shouldn't be disturbed.
    """
    vm = MagicMock()
    vm.set_interval = MagicMock(side_effect=ValueError("bad"))
    vm.is_connected = MagicMock(return_value=False)
    vm.play_clip = AsyncMock()
    sched = MagicMock()
    sched.wake = MagicMock()

    select = _make_select_with_choice(vm, sched, "10")
    await select.callback(_make_interaction())

    sched.wake.assert_not_called()
    # And we don't try to play a confirmation clip either.
    vm.play_clip.assert_not_called()


@pytest.mark.asyncio
async def test_select_callback_plays_interval_clip_when_connected() -> None:
    vm = MagicMock()
    vm.set_interval = MagicMock()
    vm.is_connected = MagicMock(return_value=True)
    vm.play_clip = AsyncMock(return_value=True)
    sched = MagicMock()
    sched.wake = MagicMock()

    select = _make_select_with_choice(vm, sched, "10")
    await select.callback(_make_interaction())

    vm.play_clip.assert_awaited_once_with(42, "interval_10")


@pytest.mark.asyncio
async def test_select_callback_skips_clip_when_disconnected() -> None:
    """No VC → silent: there's nowhere to play the cue."""
    vm = MagicMock()
    vm.set_interval = MagicMock()
    vm.is_connected = MagicMock(return_value=False)
    vm.play_clip = AsyncMock()
    sched = MagicMock()
    sched.wake = MagicMock()

    select = _make_select_with_choice(vm, sched, "10")
    await select.callback(_make_interaction())

    vm.play_clip.assert_not_called()
    # But wake still fires — set_interval is persisted regardless of VC state.
    sched.wake.assert_called_once()


@pytest.mark.asyncio
async def test_select_callback_handles_empty_values_defensively() -> None:
    """Discord enforces min_values=1, but a malformed payload shouldn't
    crash with IndexError — the callback must respond cleanly."""
    vm = MagicMock()
    vm.set_interval = MagicMock()
    sched = MagicMock()
    sched.wake = MagicMock()

    select = _IntervalSelect(vm, sched, guild_id=42, current=30)
    select._values = []
    interaction = _make_interaction()
    await select.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    vm.set_interval.assert_not_called()
    sched.wake.assert_not_called()


# ----------------------------------------------------------------------
# on_voice_state_update — auto-disconnect when the last human leaves
# ----------------------------------------------------------------------


def _make_bot() -> JihoBot:
    """Construct a JihoBot with mocked voice_manager / scheduler.

    ``commands.Bot.__init__`` is heavy but doesn't connect to Discord —
    safe to instantiate. We replace the manager and scheduler so the
    handler exercises only the auto-disconnect decision logic.
    """
    bot = JihoBot(Settings(discord_token="x"))
    bot.voice_manager = MagicMock()
    bot.scheduler = MagicMock()
    bot.scheduler.wake = MagicMock()
    bot.voice_manager.disconnect = AsyncMock()
    # ``self.user`` is set by the gateway in production; fake it so the
    # "is this me?" guard works without a connected client.
    bot._connection.user = MagicMock(id=999)
    return bot


def _voice_state(channel: object) -> MagicMock:
    state = MagicMock()
    state.channel = channel
    return state


def _channel(channel_id: int, members: list[MagicMock]) -> MagicMock:
    ch = MagicMock()
    ch.id = channel_id
    ch.members = members
    return ch


def _human(user_id: int) -> MagicMock:
    m = MagicMock()
    m.id = user_id
    m.bot = False
    return m


def _bot_member(user_id: int) -> MagicMock:
    m = MagicMock()
    m.id = user_id
    m.bot = True
    return m


def _member_event(
    user_id: int,
    guild_id: int,
    voice_channel: MagicMock | None,
    *,
    is_bot: bool = False,
) -> MagicMock:
    member = MagicMock()
    member.id = user_id
    member.bot = is_bot
    member.guild = MagicMock()
    member.guild.id = guild_id
    member.guild.voice_client = MagicMock()
    member.guild.voice_client.channel = voice_channel
    return member


@pytest.mark.asyncio
async def test_auto_disconnect_when_last_human_leaves() -> None:
    bot = _make_bot()
    bot_self = _bot_member(999)
    # Bot's channel had 1 human + bot; the human is leaving (so by the
    # time the event fires, the channel only contains the bot).
    channel = _channel(123, members=[bot_self])
    bot.voice_manager.is_connected = MagicMock(return_value=True)

    member = _member_event(7, guild_id=10, voice_channel=channel)
    await bot.on_voice_state_update(member, _voice_state(channel), _voice_state(None))

    bot.voice_manager.disconnect.assert_awaited_once_with(10)
    bot.scheduler.wake.assert_called_once()


@pytest.mark.asyncio
async def test_no_disconnect_when_humans_remain() -> None:
    bot = _make_bot()
    bot_self = _bot_member(999)
    other = _human(8)
    channel = _channel(123, members=[bot_self, other])
    bot.voice_manager.is_connected = MagicMock(return_value=True)

    member = _member_event(7, guild_id=10, voice_channel=channel)
    await bot.on_voice_state_update(member, _voice_state(channel), _voice_state(None))

    bot.voice_manager.disconnect.assert_not_called()
    bot.scheduler.wake.assert_not_called()


@pytest.mark.asyncio
async def test_no_disconnect_on_bot_own_event() -> None:
    """The bot's own state changes (joining / moving) must not trigger
    the auto-disconnect path."""
    bot = _make_bot()
    channel = _channel(123, members=[])
    bot.voice_manager.is_connected = MagicMock(return_value=True)

    me = _member_event(999, guild_id=10, voice_channel=channel, is_bot=True)
    await bot.on_voice_state_update(me, _voice_state(None), _voice_state(channel))

    bot.voice_manager.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_no_disconnect_when_event_is_for_a_different_channel() -> None:
    """A user leaving a *different* VC than the bot's must not affect us."""
    bot = _make_bot()
    bot_channel = _channel(123, members=[_bot_member(999), _human(8)])
    other_channel = _channel(456, members=[])
    bot.voice_manager.is_connected = MagicMock(return_value=True)

    member = _member_event(7, guild_id=10, voice_channel=bot_channel)
    await bot.on_voice_state_update(
        member, _voice_state(other_channel), _voice_state(None)
    )

    bot.voice_manager.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_no_disconnect_when_user_just_toggled_state_in_channel() -> None:
    """Mute toggle / video flip leaves before.channel == after.channel —
    we don't react to those."""
    bot = _make_bot()
    bot_channel = _channel(123, members=[_bot_member(999), _human(8)])
    bot.voice_manager.is_connected = MagicMock(return_value=True)

    member = _member_event(7, guild_id=10, voice_channel=bot_channel)
    await bot.on_voice_state_update(
        member, _voice_state(bot_channel), _voice_state(bot_channel)
    )

    bot.voice_manager.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_no_action_when_bot_not_connected_to_guild() -> None:
    bot = _make_bot()
    bot.voice_manager.is_connected = MagicMock(return_value=False)
    member = _member_event(7, guild_id=10, voice_channel=None)
    await bot.on_voice_state_update(
        member, _voice_state(MagicMock()), _voice_state(None)
    )
    bot.voice_manager.disconnect.assert_not_called()
