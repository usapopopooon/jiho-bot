from __future__ import annotations

from unittest.mock import MagicMock

import aiohttp
import pytest

from scripts.generate_voices import (
    DEFAULT_STATIC_CLIPS,
    DEFAULT_TEMPLATE,
    DEFAULT_TEMPLATE_HALF,
    DEFAULT_TEMPLATE_MINUTE,
    build_jobs,
    period_and_hour12,
    render_text,
    wait_for_engine,
)


def test_period_and_hour12_morning() -> None:
    assert period_and_hour12(0) == ("午前", 0)
    assert period_and_hour12(1) == ("午前", 1)
    assert period_and_hour12(11) == ("午前", 11)


def test_period_and_hour12_afternoon() -> None:
    assert period_and_hour12(12) == ("午後", 0)
    assert period_and_hour12(13) == ("午後", 1)
    assert period_and_hour12(23) == ("午後", 11)


def test_period_and_hour12_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        period_and_hour12(24)
    with pytest.raises(ValueError):
        period_and_hour12(-1)


def test_render_default_template() -> None:
    assert render_text(DEFAULT_TEMPLATE, 0) == "午前0時になったのだ"
    assert render_text(DEFAULT_TEMPLATE, 9) == "午前9時になったのだ"
    assert render_text(DEFAULT_TEMPLATE, 12) == "午後0時になったのだ"
    assert render_text(DEFAULT_TEMPLATE, 23) == "午後11時になったのだ"


def test_render_custom_template_keeps_24h_var() -> None:
    """{hour} (24-hour) も使えること — カスタムテンプレ用に残してある。"""
    assert render_text("{hour}時 ({period}{hour12}時)", 13) == "13時 (午後1時)"


# --- build_jobs --------------------------------------------------------


def test_build_jobs_full_set_count_is_148() -> None:
    """24 hour + 24 half + 24×4 minute + 4 static = 148 clips."""
    jobs = build_jobs(
        DEFAULT_TEMPLATE,
        DEFAULT_TEMPLATE_HALF,
        DEFAULT_TEMPLATE_MINUTE,
        DEFAULT_STATIC_CLIPS,
    )
    assert len(jobs) == 148


def test_build_jobs_includes_static_clips_at_end() -> None:
    """Static clips append after time-signal jobs so a partial run still
    gets the time signals first."""
    jobs = build_jobs(
        DEFAULT_TEMPLATE,
        DEFAULT_TEMPLATE_HALF,
        DEFAULT_TEMPLATE_MINUTE,
        {"connected": "go", "interval_30": "30"},
    )
    last_two = [stem for stem, _ in jobs[-2:]]
    assert sorted(last_two) == ["connected", "interval_30"]


def test_build_jobs_static_clips_optional() -> None:
    """No static clips → just the 144 time-signal jobs."""
    jobs = build_jobs(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_HALF, DEFAULT_TEMPLATE_MINUTE)
    assert len(jobs) == 144


@pytest.mark.asyncio
async def test_wait_for_engine_times_out_quickly() -> None:
    """常に失敗するセッションでは max_wait_seconds で打ち切ること。"""
    session = MagicMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(side_effect=aiohttp.ClientError("nope"))
    with pytest.raises(RuntimeError, match="unreachable"):
        # Tiny budget so the test isn't slow.
        await wait_for_engine(session, "http://x", max_wait_seconds=0.1)


@pytest.mark.asyncio
async def test_wait_for_engine_returns_version_on_success() -> None:
    """1 回目で /version が返ればそのまま戻ること。"""

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        async def text(self) -> str:
            return "0.99.0\n"

        async def __aenter__(self) -> _Resp:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    session = MagicMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(return_value=_Resp())
    version = await wait_for_engine(session, "http://x", max_wait_seconds=5)
    assert version == "0.99.0"
