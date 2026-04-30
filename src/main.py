from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from src.bot import JihoBot
from src.config import load_settings

logger = logging.getLogger(__name__)

_bots: list[JihoBot] = []


def _setup_logging(level_name: str) -> None:
    level_name = os.environ.get("LOG_LEVEL", level_name).upper()
    level = getattr(logging, level_name, None) or logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


def _install_signal_handlers() -> None:
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("stop signal received; closing %d bot(s)", len(_bots))
        for bot in _bots:
            if not bot.is_closed():
                asyncio.create_task(bot.close(), name=f"bot-shutdown-{id(bot)}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


async def _run_bot(token: str, bot: JihoBot) -> None:
    try:
        await bot.start(token)
    finally:
        if not bot.is_closed():
            await bot.close()


async def _amain() -> None:
    settings = load_settings()
    _setup_logging(settings.log_level)
    _install_signal_handlers()

    tokens = settings.discord_tokens
    logger.info("starting %d bot instance(s)", len(tokens))

    bots = [JihoBot(settings) for _ in tokens]
    _bots.extend(bots)

    await asyncio.gather(
        *(_run_bot(t, b) for t, b in zip(tokens, bots, strict=True)),
        return_exceptions=True,
    )


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("interrupted")


if __name__ == "__main__":
    main()
