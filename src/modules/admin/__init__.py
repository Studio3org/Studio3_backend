"""Internal admin/ops tooling — server-rendered, browser-only, session-cookie auth.

Deliberately separate from the JSON API: this is an internal tool for the ops team (manual
shipment entry, dispute resolution, payout retries), not something the mobile app talks to.
It shares the Flask process but nothing else — different auth, different URL prefix, HTML
instead of JSON.
"""
