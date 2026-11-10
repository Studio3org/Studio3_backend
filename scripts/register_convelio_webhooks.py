"""Register our Convelio webhook URL for every event (Phase 8). Run once per environment.

    FLASK_ENV=production python scripts/register_convelio_webhooks.py https://api.studio-3.co
    FLASK_ENV=production python scripts/register_convelio_webhooks.py https://api.studio-3.co --apply

Without --apply it only lists what's registered and what it would add. Uses CONVELIO_API_KEY
(sk_test_ → sandbox, sk_live_ → production). Never prints the key.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(f".env.{os.getenv('FLASK_ENV', 'development')}", override=False)

from src.modules.shipping import convelio_client, shipping_config  # noqa: E402

EVENTS = ("custom_quote_ready", "quote_paid", "order_created", "shipment_status_changed", "document_ready")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1].rstrip("/") + "/api/webhooks/convelio"
    apply = "--apply" in sys.argv
    print(f"Convelio: {shipping_config.base_url()}  →  {url}")
    existing = convelio_client.list_webhooks()
    have = {(w.get("triggering_event_name"), w.get("url")) for w in existing}
    for hook in existing:
        print(f"  registered: {hook.get('triggering_event_name'):<26} {hook.get('url')}")
    missing = [e for e in EVENTS if (e, url) not in have]
    for event in missing:
        if apply:
            convelio_client.create_webhook(url, event)
            print(f"  added:      {event}")
        else:
            print(f"  would add:  {event}")
    if not missing:
        print("  all five events are registered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
