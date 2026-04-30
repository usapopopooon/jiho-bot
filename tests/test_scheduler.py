from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.scheduler import (
    JihoScheduler,
    clip_name_for,
    intended_minute,
    seconds_until_next_tick,
)

JST = ZoneInfo("Asia/Tokyo")


# --- seconds_until_next_tick: hour-only --------------------------------


def test_next_tick_hour_mid_hour() -> None:
    now = datetime(2026, 5, 1, 12, 30, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 60) == 30 * 60


def test_next_tick_hour_with_microseconds() -> None:
    now = datetime(2026, 5, 1, 12, 59, 59, 500_000, tzinfo=JST)
    assert seconds_until_next_tick(now, 60) == pytest.approx(0.5, rel=1e-3)


def test_next_tick_hour_on_boundary_waits_full_hour() -> None:
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 60) == 3600


def test_next_tick_hour_crosses_day() -> None:
    now = datetime(2026, 5, 1, 23, 30, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 60) == 30 * 60


# --- seconds_until_next_tick: half-hour --------------------------------


def test_next_tick_half_before_30_targets_30() -> None:
    now = datetime(2026, 5, 1, 12, 10, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 30) == 20 * 60


def test_next_tick_half_after_30_targets_next_hour() -> None:
    now = datetime(2026, 5, 1, 12, 45, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 30) == 15 * 60


def test_next_tick_half_on_30_boundary_waits_30_minutes() -> None:
    now = datetime(2026, 5, 1, 12, 30, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 30) == 30 * 60


def test_next_tick_half_crosses_day() -> None:
    now = datetime(2026, 5, 1, 23, 45, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 30) == 15 * 60


# --- seconds_until_next_tick: every 10 ---------------------------------


def test_next_tick_every10_within_bucket() -> None:
    now = datetime(2026, 5, 1, 12, 7, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 10) == 3 * 60


def test_next_tick_every10_on_boundary_waits_10_minutes() -> None:
    now = datetime(2026, 5, 1, 12, 20, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 10) == 10 * 60


def test_next_tick_every10_crosses_hour() -> None:
    now = datetime(2026, 5, 1, 12, 55, 0, tzinfo=JST)
    assert seconds_until_next_tick(now, 10) == 5 * 60


def test_next_tick_rejects_invalid_interval() -> None:
    """7 doesn't divide 60 — caller bug, not silent rounding."""
    with pytest.raises(ValueError):
        seconds_until_next_tick(datetime(2026, 5, 1, 12, 0, tzinfo=JST), 7)


# --- intended_minute / clip_name_for -----------------------------------


def test_intended_minute_floors_to_10() -> None:
    assert intended_minute(datetime(2026, 5, 1, 12, 0, tzinfo=JST)) == 0
    assert intended_minute(datetime(2026, 5, 1, 12, 10, tzinfo=JST)) == 10
    # Drift past the boundary still snaps back.
    assert intended_minute(datetime(2026, 5, 1, 12, 31, tzinfo=JST)) == 30
    assert intended_minute(datetime(2026, 5, 1, 12, 59, tzinfo=JST)) == 50


def test_clip_name_top_of_hour() -> None:
    assert clip_name_for(datetime(2026, 5, 1, 0, 0, tzinfo=JST)) == "0"
    assert clip_name_for(datetime(2026, 5, 1, 23, 0, tzinfo=JST)) == "23"


def test_clip_name_half_and_minute_marks() -> None:
    assert clip_name_for(datetime(2026, 5, 1, 13, 30, tzinfo=JST)) == "13_30"
    assert clip_name_for(datetime(2026, 5, 1, 9, 10, tzinfo=JST)) == "9_10"
    assert clip_name_for(datetime(2026, 5, 1, 9, 50, tzinfo=JST)) == "9_50"


def test_clip_name_tolerates_drift_into_next_minute() -> None:
    """Wakeup drift can land us a few ms past the boundary; still snaps."""
    assert clip_name_for(datetime(2026, 5, 1, 9, 31, tzinfo=JST)) == "9_30"


# --- broadcast / routing ------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_fire_skips_when_no_eligible_guilds() -> None:
    vm = MagicMock()
    vm.eligible_at = MagicMock(return_value=[])
    vm.play_clip = AsyncMock()
    sched = JihoScheduler(vm, JST)
    await sched._fire(datetime(2026, 5, 1, 12, 0, tzinfo=JST))
    vm.play_clip.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_fire_at_hour_includes_all_intervals() -> None:
    """At :00 every guild (60/30/10) is eligible — the manager returns them."""
    vm = MagicMock()
    vm.eligible_at = MagicMock(return_value=[111, 222, 333])
    vm.play_clip = AsyncMock(return_value=True)
    sched = JihoScheduler(vm, JST)
    await sched._fire(datetime(2026, 5, 1, 13, 0, tzinfo=JST))
    vm.eligible_at.assert_called_once_with(0)
    assert vm.play_clip.await_count == 3
    awaited = {call.args for call in vm.play_clip.await_args_list}
    assert awaited == {(111, "13"), (222, "13"), (333, "13")}


@pytest.mark.asyncio
async def test_scheduler_fire_at_30_routes_via_eligible_at() -> None:
    """At :30 only half/every-10 guilds are eligible — the manager filters."""
    vm = MagicMock()
    vm.eligible_at = MagicMock(return_value=[2])
    vm.play_clip = AsyncMock(return_value=True)
    sched = JihoScheduler(vm, JST)
    await sched._fire(datetime(2026, 5, 1, 13, 30, tzinfo=JST))
    vm.eligible_at.assert_called_once_with(30)
    vm.play_clip.assert_awaited_once_with(2, "13_30")


@pytest.mark.asyncio
async def test_scheduler_fire_at_10_only_every10_guilds() -> None:
    """At :10 only every-10 guilds are eligible."""
    vm = MagicMock()
    vm.eligible_at = MagicMock(return_value=[5])
    vm.play_clip = AsyncMock(return_value=True)
    sched = JihoScheduler(vm, JST)
    await sched._fire(datetime(2026, 5, 1, 13, 10, tzinfo=JST))
    vm.eligible_at.assert_called_once_with(10)
    vm.play_clip.assert_awaited_once_with(5, "13_10")


@pytest.mark.asyncio
async def test_scheduler_fire_isolates_failures() -> None:
    vm = MagicMock()
    vm.eligible_at = MagicMock(return_value=[1, 2])

    async def play(gid: int, _clip: str) -> bool:
        if gid == 1:
            raise RuntimeError("boom")
        return True

    vm.play_clip = AsyncMock(side_effect=play)
    sched = JihoScheduler(vm, JST)
    await sched._fire(datetime(2026, 5, 1, 9, 0, tzinfo=JST))
    assert vm.play_clip.await_count == 2


@pytest.mark.asyncio
async def test_scheduler_start_stop_idempotent() -> None:
    vm = MagicMock()
    vm.eligible_at = MagicMock(return_value=[])
    vm.min_interval = MagicMock(return_value=30)
    vm.play_clip = AsyncMock()
    sched = JihoScheduler(vm, JST)
    sched.start()
    sched.start()
    await asyncio.sleep(0)
    await sched.stop()
    await sched.stop()
