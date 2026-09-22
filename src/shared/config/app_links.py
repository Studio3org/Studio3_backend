"""The app-association files that make a shared link open the app.

Served by this backend rather than dropped in a web app's `public/` folder, for one practical
reason: the backend is the only thing actually deployed. Whatever host serves the API serves
these too, so Universal Links work on the staging URL today and on the production domain the
day it is pointed here, with no file to remember to copy.

**What these files are about is the app, not the domain.** The contents are the same whatever
host serves them — a Team ID, a bundle id, a package name and its signing fingerprints. That
is why the host is nowhere in this module: it does not belong in the payload, and the same
response is correct from every domain the app is associated with.

Both files must be served over HTTPS, as `application/json`, with **no redirect**. Apple and
Google both fetch them unauthenticated and both treat a redirect as a failure, which is the
usual reason association silently does not work.
"""
import os

# The paths a link may take and still open the app. Kept here so the AASA file and anything
# else that reasons about link shapes cannot drift apart.
LINK_PATHS = (
    ("/piece/*", "A shared piece link"),
    ("/series/*", "A shared series link"),
    ("/event/*", "A shared event link"),
    # The backend's own share/preview routes, which render Open Graph tags and an open-app
    # interstitial. A link the app itself generates points here, so it has to match too —
    # otherwise the app's own share links open the app and then do nothing.
    ("/share/piece/*", "The share preview for a piece"),
    ("/share/series/*", "The share preview for a series"),
    ("/share/event/*", "The share preview for an event"),
    # Where Stripe returns an artist after Connect onboarding. Claimed so the platform
    # hands the URL straight to the app; without it the artist lands in a browser at the
    # end of payout setup, which is where this was found — as a 404, because neither the
    # route nor this entry existed.
    ("/connect/return", "Returning from Stripe Connect onboarding"),
    ("/connect/refresh", "A Connect onboarding link that expired"),
)


def ios_team_id() -> str:
    """The 10-character Apple Developer Team ID that prefixes the app id."""
    return (os.getenv("IOS_TEAM_ID") or "").strip()


def ios_bundle_id() -> str:
    return (os.getenv("IOS_BUNDLE_ID") or "com.studio3.discover").strip()


def android_package() -> str:
    return (os.getenv("ANDROID_PACKAGE") or "com.studio3.discover").strip()


def android_fingerprints() -> list[str]:
    """Every signing key whose builds should verify, comma-separated in the environment.

    Plural on purpose. A project normally has more than one legitimately: the release key,
    the debug key that local and internally-shared builds are signed with, and — once an app
    is on Play with Play App Signing — Google's own re-signing key. Listing only one means
    links verify for some of your own builds and silently not for others.
    """
    raw = os.getenv("ANDROID_CERT_FINGERPRINTS") or ""
    return [f.strip().upper() for f in raw.split(",") if f.strip()]


def apple_app_site_association() -> dict:
    """The AASA document.

    Returns an empty `details` list when no Team ID is configured rather than emitting
    `REPLACE_WITH_TEAM_ID`. A syntactically valid file with no app in it simply associates
    nothing; a file containing a placeholder looks configured and fails in a way that takes
    an afternoon to work out.
    """
    team = ios_team_id()
    if not team:
        return {"applinks": {"details": []}}

    return {
        "applinks": {
            "details": [
                {
                    "appIDs": [f"{team}.{ios_bundle_id()}"],
                    "components": [
                        {"/": path, "comment": comment} for path, comment in LINK_PATHS
                    ],
                }
            ]
        }
    }


def asset_links() -> list:
    """The Android Digital Asset Links document.

    Empty when no fingerprint is configured, for the same reason as above.
    """
    fingerprints = android_fingerprints()
    if not fingerprints:
        return []
    return [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": android_package(),
                "sha256_cert_fingerprints": fingerprints,
            },
        }
    ]


def is_configured() -> bool:
    """Whether app association can work at all on this deployment."""
    return bool(ios_team_id()) or bool(android_fingerprints())
