"""An event's time zone from where it is (ticketing spec "Time"; B28: "The event's time zone
comes from its location, not the host's phone").

Offline (timezonefinder ships the boundaries), so creating an event never waits on an
outside API. Loaded once, on first use.
"""
from functools import lru_cache
from typing import Optional

from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def _finder():
    from timezonefinder import TimezoneFinder

    return TimezoneFinder()


def zone_for(latitude, longitude) -> Optional[str]:
    """The IANA zone at a point ("America/Chicago"), or None when there's no point or no
    zone there (open sea)."""
    if latitude is None or longitude is None:
        return None
    try:
        lat, lng = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    try:
        return _finder().timezone_at(lat=lat, lng=lng)
    except Exception:  # noqa: BLE001 — a missing zone falls back to what the host sent
        logger.exception("Time zone lookup failed for %s,%s", lat, lng)
        return None
