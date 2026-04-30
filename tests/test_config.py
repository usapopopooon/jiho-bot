from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings


def test_single_token_populates_list() -> None:
    s = Settings(discord_token="abc")
    assert s.discord_tokens == ["abc"]
    assert s.discord_token == "abc"


def test_csv_tokens_split_and_first_promoted() -> None:
    s = Settings(discord_tokens="a, b ,c")
    assert s.discord_tokens == ["a", "b", "c"]
    assert s.discord_token == "a"


def test_both_token_envs_merge_with_dedup() -> None:
    """Both DISCORD_TOKEN and DISCORD_TOKENS are honored; dupes drop."""
    s = Settings(discord_token="a", discord_tokens="b,a,c")
    assert s.discord_tokens == ["b", "a", "c"]
    # Singular echoes the first surviving entry.
    assert s.discord_token == "b"


def test_guild_ids_csv_parsed_to_ints() -> None:
    s = Settings(discord_token="x", discord_guild_ids="111, 222")
    assert s.discord_guild_ids == [111, 222]


def test_missing_token_raises() -> None:
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_timezone_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(discord_token="x", jiho_timezone="Mars/Olympus")


def test_default_timezone_is_jst() -> None:
    s = Settings(discord_token="x")
    assert s.jiho_timezone == "Asia/Tokyo"
    assert s.timezone.key == "Asia/Tokyo"
