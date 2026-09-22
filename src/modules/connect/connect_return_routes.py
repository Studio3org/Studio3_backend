"""Where Stripe sends an artist after Connect onboarding.

Mounted at the **site root** with no url_prefix, and that is the point: Stripe redirects a
browser to `<CONNECT_ONBOARDING_BASE_URL>/return`, and the app claims those same paths as
Universal Links. Under `/api` neither would work — Apple fetches the association file from a
fixed path and matches the literal URL a link carries.

Three things can arrive here:

  - **The app, via a Universal Link.** The best case: iOS or Android hands the URL straight
    to the app and this page is never rendered. That needs `/connect/*` in
    `app_links.LINK_PATHS`, which is the other half of this change.
  - **A mobile browser** where association has not taken effect. The page tries the custom
    scheme `studio3://`, which needs no domain verification and so works even before
    Universal Links do.
  - **A desktop browser**, where there is no app to return to. It says so, rather than
    spinning on a deep link that cannot resolve.

Deliberately unauthenticated. Stripe redirects a bare browser with no session and no way to
prove who it is, so anything gated would 401 the artist at the last step of onboarding. The
pages carry no account information for that reason — they say "you are done" and get out of
the way. Whether the artist can actually be paid is decided by the `account.updated` webhook
and read back through `/api/artists/connect/status`, never from a URL somebody landed on.
"""
from flask import Blueprint, render_template

from src.modules.share.share_routes import _app_store_url, _play_store_url

connect_return_bp = Blueprint("connect_return", __name__, template_folder="../../templates")


def _page(*, title: str, description: str, deep_link: str):
    return render_template(
        "connect/return.html",
        title=title,
        description=description,
        deep_link=deep_link,
        app_store_url=_app_store_url(),
        play_store_url=_play_store_url(),
    )


@connect_return_bp.get("/connect/return")
def connect_return():
    """Onboarding finished, or the artist pressed done.

    Stripe sends everyone here regardless of whether they completed every requirement, so
    this cannot promise the account is ready — it says the form is submitted and lets the
    app read the real status.
    """
    return _page(
        title="Payout setup submitted",
        description="Stripe has your details. Head back to Studio 3 to see where it stands.",
        deep_link="studio3://connect/return",
    )


@connect_return_bp.get("/connect/refresh")
def connect_refresh():
    """The Account Link expired or was already used.

    They are single-use and last minutes, so this is reached by going back, or by leaving the
    tab open too long. A fresh link can only be minted for a signed-in artist, which a
    redirected browser is not — so the way forward is through the app, which can ask for one.
    """
    return _page(
        title="That link expired",
        description="Payout setup links last only a few minutes. Open Studio 3 and start it "
                    "again — nothing you entered was lost.",
        deep_link="studio3://connect/refresh",
    )
