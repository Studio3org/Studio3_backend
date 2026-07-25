"""File + optional console logging."""
import os
import logging
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_LOG_DIR = _BASE_DIR / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


def get_logger(name: str) -> logging.Logger:
    """Return logger with file (error.log, combined.log) and console in development."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Error log
    err_handler = logging.FileHandler(_LOG_DIR / "error.log", encoding="utf-8")
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(_formatter)
    logger.addHandler(err_handler)

    # Combined log
    combined_handler = logging.FileHandler(
        _LOG_DIR / "combined.log", encoding="utf-8"
    )
    combined_handler.setLevel(logging.DEBUG)
    combined_handler.setFormatter(_formatter)
    logger.addHandler(combined_handler)

    # Always log to stderr so hosts like Render surface stack traces.
    # DEBUG on console only in development to avoid noisy prod logs.
    console = logging.StreamHandler()
    console.setLevel(
        logging.DEBUG
        if os.getenv("FLASK_ENV", "development") == "development"
        else logging.ERROR
    )
    console.setFormatter(_formatter)
    logger.addHandler(console)

    return logger