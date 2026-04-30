from __future__ import annotations

import os
import sys
from pathlib import Path

# Tests are run from the project root via ``pytest``; make ``src`` importable
# without forcing an editable install.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Settings reads any matching env var at instantiation time. The dev's
# shell could carry — for example — a real ``DISCORD_TOKEN``, or a
# non-JST ``JIHO_TIMEZONE`` that would break ``test_default_timezone_is_jst``.
# Scrub every var Settings recognises so the suite always runs from a
# clean baseline.
#
# ``.env`` files are NOT explicitly disabled here. The repo's ``.gitignore``
# excludes ``.env`` so committed tests / CI never see one; if a developer
# creates a local ``.env`` they're opting into the same env vars below.
_LEAKY_VARS = (
    "DISCORD_TOKEN",
    "DISCORD_TOKENS",
    "DISCORD_GUILD_IDS",
    "JIHO_TIMEZONE",
    "LOG_LEVEL",
)
for _v in _LEAKY_VARS:
    os.environ.pop(_v, None)
