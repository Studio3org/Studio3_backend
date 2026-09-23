"""Public, unauthenticated legal/support pages served straight from the API domain.

Root-mounted (no url_prefix), like well_known_routes — this needs to be reachable at a
plain, stable path a store listing can point at, not nested under /api or /share.

The web app (studio-3.co) has its own copy of this same page
(app/src/screens/DeleteAccountPage.jsx) for when it's actually deployed there. This one
exists because api.studio-3.co is the one domain in this project that is reliably live
today — Google Play's Data Safety form needs a URL that works *now*, not once DNS/hosting
for the web app is sorted out. If the wording ever needs to change, update both copies.
"""
from flask import Blueprint, render_template

legal_bp = Blueprint("legal", __name__, template_folder="../../templates")


@legal_bp.get("/delete-account")
def delete_account_page():
    return render_template("legal/delete_account.html")
