"""Application logging.

Two things here are deliberate and were previously wrong:

* **Console level.** It was ERROR outside development, which meant "Payout released",
  "CHARGEBACK opened" and "Duplicate Stripe event ignored" were all invisible in production
  — every INFO line the money paths emit went nowhere. It is INFO now.
* **File handlers.** `logs/` is gitignored and ephemeral on Render, so in production every
  file write was discarded. They are development-only.
"""
import logging
import os
from pathlib import Path

from src.shared.utils.request_id import RequestIdFilter

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_LOG_DIR = _BASE_DIR / "logs"

# request_id comes from the ContextVar in shared/utils/request_id; it reads '-' for
# records emitted outside any request (imports, scheduler ticks).
_FORMAT = "%(asctime)s [%(request_id)s] %(name)s %(levelname)s %(message)s"


def _is_development() -> bool:
    return os.getenv("FLASK_ENV", "development") == "development"


def _console_level() -> int:
    return logging.DEBUG if _is_development() else logging.INFO


def get_logger(name: str) -> logging.Logger:
    """Logger writing to stderr always, plus rotating files in development."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_FORMAT)

    # stderr is what the host actually collects.
    console = logging.StreamHandler()
    console.setLevel(_console_level())
    console.setFormatter(formatter)
    console.addFilter(RequestIdFilter())
    logger.addHandler(console)

    if _is_development():
        _LOG_DIR.mkdir(exist_ok=True)
        for filename, level in (("error.log", logging.ERROR), ("combined.log", logging.DEBUG)):
            handler = logging.FileHandler(_LOG_DIR / filename, encoding="utf-8")
            handler.setLevel(level)
            handler.setFormatter(formatter)
            handler.addFilter(RequestIdFilter())
            logger.addHandler(handler)

    return logger
