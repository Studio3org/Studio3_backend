"""The app-association files, served from the site root.

A blueprint of its own with **no url_prefix**, and that is the whole reason it exists rather
than living beside the share routes: Apple and Google fetch these from a fixed, unprefixed
path. Mounted under `/share` they would be served at `/share/.well-known/...`, which nothing
ever requests, and association would fail with no error anywhere to explain it.

Both files must come back as JSON, over HTTPS, with no redirect — a redirect is treated as a
failure by both platforms.
"""
from flask import Blueprint, jsonify

from src.shared.config import app_links

well_known_bp = Blueprint("well_known", __name__)


@well_known_bp.get("/.well-known/apple-app-site-association")
def apple_app_site_association():
    return jsonify(app_links.apple_app_site_association()), 200


@well_known_bp.get("/.well-known/assetlinks.json")
def android_asset_links():
    return jsonify(app_links.asset_links()), 200
