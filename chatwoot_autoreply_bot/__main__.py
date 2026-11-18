"""Entry point: ``python -m chatwoot_autoreply_bot``."""

from __future__ import annotations

import logging
import sys

import uvicorn

from chatwoot_autoreply_bot.app import create_app
from chatwoot_autoreply_bot.config import ConfigError, Settings

QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "openai")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        sys.exit(f"chatwoot-autoreply-bot: {error}")
    configure_logging()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.http_port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
