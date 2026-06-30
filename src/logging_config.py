import logging

_CONFIG_SENTINEL = "_spoty_scanner_logging_configured"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure logging once to avoid duplicated log lines."""
    root_logger = logging.getLogger()
    if getattr(root_logger, _CONFIG_SENTINEL, False):
        return

    if not root_logger.handlers:
        logging.basicConfig(level=level)
    else:
        root_logger.setLevel(level)

    logging.getLogger("discord").setLevel(logging.WARNING)
    setattr(root_logger, _CONFIG_SENTINEL, True)
