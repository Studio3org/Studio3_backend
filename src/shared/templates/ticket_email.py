"""The ticket confirmation email: the tickets and the receipt (ticketing spec, "Payment
succeeds")."""
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _local_time(event) -> str:
    """"Sat, Nov 21 · 8:00 PM CST" in the event's own zone (spec: every time shows its zone)."""
    starts = event.starts_at
    zone_label = ""
    if event.timezone:
        try:
            starts = starts.astimezone(ZoneInfo(event.timezone))
            zone_label = " " + starts.strftime("%Z")
        except (ZoneInfoNotFoundError, ValueError):
            pass
    hour = starts.strftime("%I").lstrip("0") or "12"
    return f"{starts.strftime('%a, %b')} {starts.day} · {hour}:{starts.strftime('%M %p')}{zone_label}"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def ticket_email_html(event, order, tickets: list[dict]) -> str:
    rows = "".join(
        f"<tr><td style='padding:6px 0;'>{escape(t['ticketTypeName'])}</td>"
        f"<td style='padding:6px 0;font-family:monospace;text-align:right;'>{escape(t['number'])}</td></tr>"
        for t in tickets
    )
    where = escape(event.venue_name or event.address or "")
    receipt = ""
    if order.total_cents:
        receipt = f"""
        <h3 style="margin:24px 0 8px;">Receipt</h3>
        <table style="width:100%;border-collapse:collapse;">
          <tr><td>Tickets</td><td style="text-align:right;">{_money(order.subtotal_cents)}</td></tr>
          <tr><td>Sales tax</td><td style="text-align:right;">{_money(order.tax_cents)}</td></tr>
          <tr><td><strong>Total</strong></td><td style="text-align:right;"><strong>{_money(order.total_cents)}</strong></td></tr>
        </table>"""
    age = ""
    if getattr(event, "age_limit", "all") in ("18", "21"):
        age = f"<p><strong>{event.age_limit}+ event.</strong> Bring your ID.</p>"
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Your tickets</title></head>
    <body style="font-family: sans-serif; padding: 20px; color: #1a1a1a;">
        <h2 style="margin:0 0 4px;">{escape(event.title)}</h2>
        <p style="margin:0 0 16px;color:#555;">{_local_time(event)}{' · ' + where if where else ''}</p>
        {age}
        <table style="width:100%;border-collapse:collapse;">{rows}</table>
        <p style="color:#555;">Open your tickets in the Studio 3 app to show the QR code at the door.
        Each ticket can be checked in once.</p>
        {receipt}
    </body>
    </html>
    """
