"""Flat-rate shipping + tax constants. No carrier integration — matches the product
spec's simple tier list (Standard/Express/Overnight/Free)."""

SHIPPING_RATES_CENTS = {
    "standard": 500,
    "express": 1500,
    "overnight": 3500,
    "free": 0,
}

# Sales tax moved to src/shared/config/tax.py (integer cents, configurable rate, BE-11).
