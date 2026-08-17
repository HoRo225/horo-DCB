"""Application entry point."""

import logging

from horo_dcb.bot import create_bot
from horo_dcb.config import ConfigError, load_discord_token

logger = logging.getLogger(__name__)


def main() -> None:
    """Start the Discord bot or fail cleanly when configuration is missing."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        token = load_discord_token()
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None

    bot = create_bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
