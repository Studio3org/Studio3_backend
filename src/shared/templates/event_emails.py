"""Plain event emails: waitlist holds, a rescheduled or cancelled event, a ticket refund.

One layout, matching the ticket email (templates/ticket_email.py): the event's name and its
local time with the zone, then a few lines. The host's own message, when there is one, is
quoted as theirs.
"""
from html import escape
from typing import Iterable, Optional

from src.shared.templates.ticket_email import local_time_label


def simple_event_email(event, heading: str, lines: Iterable[str], *,
                       host_message: Optional[str] = None) -> str:
    where = escape(event.venue_name or event.address or "")
    paragraphs = "".join(f"<p style='margin:0 0 12px;'>{escape(line)}</p>" for line in lines if line)
    quote = ""
    if host_message:
        quote = ("<blockquote style='margin:16px 0;padding:8px 12px;border-left:3px solid #ccc;"
                 f"color:#333;'>{escape(host_message)}</blockquote>")
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>{escape(heading)}</title></head>
    <body style="font-family: sans-serif; padding: 20px; color: #1a1a1a;">
        <p style="margin:0 0 4px;color:#555;">{escape(heading)}</p>
        <h2 style="margin:0 0 4px;">{escape(event.title)}</h2>
        <p style="margin:0 0 16px;color:#555;">{local_time_label(event)}{' · ' + where if where else ''}</p>
        {paragraphs}
        {quote}
    </body>
    </html>
    """
