"""Generate every time-signal WAV the bot can play, in one bulk run.

This script is **local-only** — the production bot ships pre-rendered
WAVs and never talks to VOICEVOX itself. Run it once after a fresh
clone (or whenever you want to re-render with a different speaker /
template):

    docker compose -f docker-compose.gen.yml up --build \\
        --abort-on-container-exit --exit-code-from gen

Output is always the full set so any ``/jiho interval:`` choice (60 /
30 / 10 minutes) just works:

- ``voices/<H>.wav``       for HH:00      (24 files)   — "X時になったのだ"
- ``voices/<H>_30.wav``    for HH:30      (24 files)   — "X時半なのだ"
- ``voices/<H>_<M>.wav``   for HH:10/20/40/50  (96 files) — "X時M分なのだ"

= 144 WAVs in 48kHz/stereo/16bit so :class:`discord.PCMAudio` can play
them without shelling out to ffmpeg.

Speaker 3 is VOICEVOX's ずんだもん (ノーマル). To use a different style
or text, override ``--speaker`` / ``--template*``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import aiohttp

DEFAULT_ENGINE = "http://localhost:50021"
DEFAULT_SPEAKER = 3  # VOICEVOX: ずんだもん (ノーマル)

# Template variables across all three:
#   {period} → 午前/午後
#   {hour12} → 0..11 (mod 12; 12 maps to 0)
#   {hour}   → 0..23 (24-hour)
#   {minute} → only for the minute template (10/20/40/50)
DEFAULT_TEMPLATE = "{period}{hour12}時になったのだ"
DEFAULT_TEMPLATE_HALF = "{period}{hour12}時半なのだ"
DEFAULT_TEMPLATE_MINUTE = "{period}{hour12}時{minute}分なのだ"

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "voices"

# Discord wants 48kHz stereo PCM. Match it so the bot can skip ffmpeg.
DISCORD_SAMPLE_RATE = 48000
DISCORD_STEREO = True

# Minute marks the bot's scheduler can fire on (besides :00).
HALF_HOUR_MINUTE = 30
EVERY_TEN_MINUTES = (10, 20, 40, 50)

logger = logging.getLogger("generate_voices")


def period_and_hour12(hour: int) -> tuple[str, int]:
    """24時制の hour を (午前/午後, 0..11) に分解する。

    境界の慣例: 0時 → ("午前", 0), 12時 → ("午後", 0), 13時 → ("午後", 1)。
    "午前12時" / "午後12時" の代わりに 0 を採用する(数学的に明瞭)。
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0..23, got {hour}")
    period = "午前" if hour < 12 else "午後"
    return period, hour % 12


def render_text(template: str, hour: int, minute: int = 0) -> str:
    """テンプレートに hour / hour12 / period / minute を埋める。"""
    period, hour12 = period_and_hour12(hour)
    return template.format(hour=hour, hour12=hour12, period=period, minute=minute)


async def wait_for_engine(
    session: aiohttp.ClientSession,
    engine_url: str,
    max_wait_seconds: float,
) -> str:
    """Poll ``/version`` until the engine answers; return its version string.

    The engine container takes 5–15s to boot in CPU mode, so a single probe
    races the boot. Retrying with a short backoff lets us drop the docker
    healthcheck (the engine image doesn't ship curl) and rely entirely on
    the script's own readiness check — that's what makes the
    docker-compose.gen.yml flow a single ``up`` away.
    """
    deadline = time.monotonic() + max_wait_seconds
    last_err: Exception | None = None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            async with session.get(
                f"{engine_url}/version",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                resp.raise_for_status()
                return (await resp.text()).strip()
        except (aiohttp.ClientError, TimeoutError) as e:
            last_err = e
            if attempt == 1 or attempt % 5 == 0:
                logger.info(
                    "waiting for voicevox engine at %s (attempt=%d, last=%s)",
                    engine_url,
                    attempt,
                    e.__class__.__name__,
                )
            await asyncio.sleep(1.5)
    raise RuntimeError(
        f"voicevox engine unreachable at {engine_url} after "
        f"{max_wait_seconds:.0f}s: {last_err}"
    )


async def synthesize(
    session: aiohttp.ClientSession,
    engine_url: str,
    speaker: int,
    text: str,
) -> bytes:
    """One audio_query → synthesis round-trip; returns the WAV bytes."""
    async with session.post(
        f"{engine_url}/audio_query",
        params={"text": text, "speaker": speaker},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        query = await resp.json()

    query["outputSamplingRate"] = DISCORD_SAMPLE_RATE
    query["outputStereo"] = DISCORD_STEREO

    async with session.post(
        f"{engine_url}/synthesis",
        params={"speaker": speaker},
        json=query,
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        return await resp.read()


def build_jobs(
    template_hour: str,
    template_half: str,
    template_minute: str,
) -> list[tuple[str, str]]:
    """Return ``(file_stem, text)`` for every clip the bot might play.

    Stems: ``"<hour>"`` for the hour clip, ``"<hour>_<minute>"`` for the
    rest. Final path is ``out_dir / f"{stem}.wav"``.
    """
    jobs: list[tuple[str, str]] = []
    for hour in range(24):
        # :00 — the hour clip every interval lands on.
        jobs.append((str(hour), render_text(template_hour, hour)))
        # :30 — half-hour cue, kept in its own template so users can
        # render the natural "X時半" instead of "X時30分".
        jobs.append(
            (
                f"{hour}_{HALF_HOUR_MINUTE}",
                render_text(template_half, hour, HALF_HOUR_MINUTE),
            )
        )
        # :10/:20/:40/:50 — every-10-minute marks. Same template for all.
        for minute in EVERY_TEN_MINUTES:
            jobs.append(
                (
                    f"{hour}_{minute}",
                    render_text(template_minute, hour, minute),
                )
            )
    return jobs


async def _amain(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = build_jobs(args.template, args.template_half, args.template_minute)
    logger.info(
        "rendering %d clips engine=%s speaker=%d out=%s",
        len(jobs),
        args.engine,
        args.speaker,
        out_dir,
    )

    async with aiohttp.ClientSession() as session:
        try:
            version = await wait_for_engine(session, args.engine, args.wait_seconds)
        except RuntimeError as e:
            logger.error(
                "%s. Start it first via `docker compose -f "
                "docker-compose.gen.yml up` (recommended) or `docker run "
                "--rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest`.",
                e,
            )
            return 1
        logger.info("voicevox engine version: %s", version)

        for stem, text in jobs:
            target = out_dir / f"{stem}.wav"
            if target.exists() and not args.force:
                logger.info("skip existing %s (use --force to overwrite)", target.name)
                continue
            logger.info("synth stem=%s text=%r", stem, text)
            data = await synthesize(session, args.engine, args.speaker, text)
            # Atomic-ish write: the synth call can take a few seconds and
            # users sometimes Ctrl-C mid-render — a partial wav left at
            # the final path would be silently picked up by the bot.
            tmp = target.with_suffix(".wav.tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            logger.info("wrote %s (%d bytes)", target, len(data))

    logger.info("done")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default=DEFAULT_ENGINE)
    parser.add_argument("--speaker", type=int, default=DEFAULT_SPEAKER)
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help=(
            ":00 用の読み上げテンプレ。{period}/{hour12}/{hour} が使える。"
            "Default: %(default)r"
        ),
    )
    parser.add_argument(
        "--template-half",
        default=DEFAULT_TEMPLATE_HALF,
        help=":30 用テンプレ。Default: %(default)r",
    )
    parser.add_argument(
        "--template-minute",
        default=DEFAULT_TEMPLATE_MINUTE,
        help=(
            ":10/:20/:40/:50 用テンプレ。{minute} 変数も使える "
            "(10/20/40/50 が入る)。Default: %(default)r"
        ),
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing wavs (default: skip)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=15.0,
        help=(
            "VOICEVOX エンジンの起動を待つ最大秒数 (CPU 起動は 5〜15s)。"
            "docker-compose.gen.yml では 90 を渡している。Default: %(default)s"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
