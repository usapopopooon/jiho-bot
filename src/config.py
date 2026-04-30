from __future__ import annotations

from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.constants import DEFAULT_TIMEZONE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: str = ""
    discord_tokens: Annotated[list[str], NoDecode] = []
    discord_guild_ids: Annotated[list[int], NoDecode] = []

    # Time signal timezone — IANA name. Defaults to JST so the on-the-hour
    # boundary matches the listener's wall clock.
    jiho_timezone: str = DEFAULT_TIMEZONE

    log_level: str = "INFO"

    @field_validator("discord_guild_ids", mode="before")
    @classmethod
    def _split_guild_ids(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            return [int(s) for s in v.split(",") if s.strip()]
        return v

    @field_validator("discord_tokens", mode="before")
    @classmethod
    def _split_tokens(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("jiho_timezone", mode="after")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone: {v}") from e
        return v

    @model_validator(mode="after")
    def _validate_required(self) -> Settings:
        # Merge ``DISCORD_TOKEN`` and ``DISCORD_TOKENS`` into one deduped
        # list so users can set either, both, or split them however they
        # like. Order: CSV first (multi-bot deploys), then the single
        # token if it adds something new.
        merged: list[str] = []
        seen: set[str] = set()
        for tok in [*self.discord_tokens, self.discord_token.strip()]:
            if tok and tok not in seen:
                seen.add(tok)
                merged.append(tok)
        if not merged:
            raise ValueError(
                "DISCORD_TOKEN (or DISCORD_TOKENS as CSV) environment "
                "variable is required."
            )
        self.discord_tokens = merged
        # Keep ``discord_token`` echoing the first entry for back-compat
        # with any caller that still reads the singular field.
        self.discord_token = merged[0]
        return self

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.jiho_timezone)


def load_settings() -> Settings:
    return Settings()
