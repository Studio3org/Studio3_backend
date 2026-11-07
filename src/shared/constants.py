"""Shared constants (e.g. user roles, activity kinds).

User role is primary interest / primary use only (artist, collector, enthusiast).
It is not used for permission checks—artists can purchase, collectors can post.
"""

ALLOWED_ROLES = ("artist", "collector", "enthusiast")

# Activity kinds for role-from-activity (increment_activity).
ACTIVITY_KINDS = ("post", "purchase", "save")

# Terms of Use / EULA version (App Store guideline 1.2). Bump whenever the terms text at
# /terms (src/templates/legal/terms.html) and the app's in-app copy change materially —
# the client re-prompts any account whose recorded terms_version differs.
CURRENT_TERMS_VERSION = "2026-10-10"

# Privacy Policy version agreed to with the Terms (BACKEND_CHANGES B30, `NoteSignIn3Profile`:
# record which version of each document). The privacy terms are part of the Terms document
# today, so this moves with it; bump it on its own once a separate policy page ships.
CURRENT_PRIVACY_VERSION = "2026-10-10"
