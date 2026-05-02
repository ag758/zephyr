"""
Structured logging setup for the arbitrage bot.

Provides JSON-formatted log output with contextual fields
for easy parsing and monitoring in Docker/cloud environments.
"""

import logging
import json
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }

        # Attach extra fields if present (e.g., basis, symbol, trade details)
        if hasattr(record, "extra_data"):
            log_entry["data"] = record.extra_data

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)


def setup_logger(name: str = "zephyr", level: str = "INFO") -> logging.Logger:
    """
    Create and configure a logger with JSON output to stdout.

    Args:
        name: Logger name
        level: Log level string (DEBUG, INFO, WARNING, ERROR)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers on repeated calls
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger


def log_with_data(logger: logging.Logger, level: str, message: str, **kwargs):
    """
    Log a message with structured extra data.

    Usage:
        log_with_data(logger, "info", "Basis detected", symbol="BTC/USDT", basis_pct=2.5)
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    extra_record = logger.makeRecord(
        logger.name, log_level, "(unknown)", 0, message, (), None
    )
    extra_record.extra_data = kwargs
    logger.handle(extra_record)
