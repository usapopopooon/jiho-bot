from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.voice_manager import VoiceManager


def _fake_client(connected: bool = True) -> MagicMock:
    client = MagicMock()
    client.is_connected = MagicMock(return_value=connected)
    client.is_playing = MagicMock(return_value=False)
    return client


def test_is_connected_false_for_unknown_guild(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    assert vm.is_connected(123) is False


def test_connected_guild_ids_filters_dropped(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    vm._connections[1] = _fake_client(connected=True)
    vm._connections[2] = _fake_client(connected=False)
    vm._connections[3] = _fake_client(connected=True)
    assert sorted(vm.connected_guild_ids()) == [1, 3]


@pytest.mark.asyncio
async def test_play_clip_returns_false_when_not_connected(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    assert await vm.play_clip(guild_id=999, clip_name="0") is False


@pytest.mark.asyncio
async def test_play_clip_returns_false_when_clip_missing(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    vm._connections[1] = _fake_client(connected=True)
    assert await vm.play_clip(guild_id=1, clip_name="missing") is False


# --- interval API -------------------------------------------------------


def test_get_interval_defaults_to_half_hour(tmp_path: Path) -> None:
    """Default cadence is 30 (half-hour), not 60."""
    vm = VoiceManager(voices_dir=tmp_path)
    assert vm.get_interval(42) == 30


def test_set_interval_persists_and_clears(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    vm.set_interval(1, 10)
    assert vm.get_interval(1) == 10
    # Resetting to default removes the entry rather than storing a redundant 30.
    vm.set_interval(1, 30)
    assert vm.get_interval(1) == 30
    assert 1 not in vm._interval


def test_set_interval_rejects_non_divisor(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    with pytest.raises(ValueError):
        vm.set_interval(1, 7)


def test_set_interval_rejects_zero_and_negative(tmp_path: Path) -> None:
    """``60 % 0`` would raise ZeroDivisionError if the guard is missing."""
    vm = VoiceManager(voices_dir=tmp_path)
    with pytest.raises(ValueError):
        vm.set_interval(1, 0)
    with pytest.raises(ValueError):
        vm.set_interval(1, -10)


def test_min_interval_with_no_guilds_is_default(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    assert vm.min_interval() == 30


def test_min_interval_picks_finest_among_connected(tmp_path: Path) -> None:
    vm = VoiceManager(voices_dir=tmp_path)
    vm._connections[1] = _fake_client(connected=True)
    vm._connections[2] = _fake_client(connected=True)
    vm._connections[3] = _fake_client(connected=False)  # disconnected
    vm.set_interval(1, 60)
    vm.set_interval(2, 10)
    vm.set_interval(3, 10)  # disconnected — should not influence min
    assert vm.min_interval() == 10


def test_eligible_at_routes_by_interval(tmp_path: Path) -> None:
    """:00 → all; :30 → default+30+10; :10 → 10 only."""
    vm = VoiceManager(voices_dir=tmp_path)
    vm._connections[100] = _fake_client(connected=True)  # default 30
    vm._connections[200] = _fake_client(connected=True)
    vm._connections[300] = _fake_client(connected=True)
    vm.set_interval(200, 60)
    vm.set_interval(300, 10)

    assert sorted(vm.eligible_at(0)) == [100, 200, 300]
    # 30 % 30 = 0 (default-guild eligible), 30 % 60 = 30 (200 not eligible),
    # 30 % 10 = 0 (300 eligible).
    assert sorted(vm.eligible_at(30)) == [100, 300]
    assert vm.eligible_at(10) == [300]
    assert vm.eligible_at(20) == [300]
