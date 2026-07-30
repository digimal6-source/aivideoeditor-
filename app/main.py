"""Application entry point.

Run with:  python3 -m app.main
"""

from __future__ import annotations

import logging
import sys

from .api.http_server import serve
from .settings import get_settings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # FFmpeg command lines are logged at INFO; keep third-party noise down.
    logging.getLogger("PIL").setLevel(logging.WARNING)


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    serve(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
