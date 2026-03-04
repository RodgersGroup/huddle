"""Logging configuration for Huddle."""

import logging
import logging.handlers
from contextvars import ContextVar
from pathlib import Path

from config import LOG_LEVEL

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Context variable holding the current request's correlation ID.
# Set per-request by the request_id middleware in app.py.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Inject the current request_id into every log record."""

    def filter(self, record):
        record.request_id = request_id_ctx.get("-")
        return True


def setup_logging():
    """Configure the 'huddle' logger with rotating file and console handlers."""
    logger = logging.getLogger("huddle")
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers on reload
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(request_id)s | %(module)s.%(funcName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Attach the request-id filter to each handler (not the logger) so it
    # applies to records from child loggers like "huddle.agents" too.
    rid_filter = RequestIdFilter()

    # Rotating file handler: 10MB per file, keep 5 backups
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "huddle.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(rid_filter)

    # Console handler (captured by systemd journal)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    console_handler.setFormatter(formatter)
    console_handler.addFilter(rid_filter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
