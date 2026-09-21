"""Where uploaded media lands in the bucket.

A key is not just a filename: anything that can exist more than once per user needs its own
id in the path. Event covers did not have one, and the resulting collision was invisible —
the upload succeeded, the URL resolved, and it served the wrong image.
"""
import pytest

from src.shared.storage.s3_paths import PURPOSE_DIRS, build_media_key

JPEG = "image/jpeg"


def test_a_profile_image_is_one_fixed_object_per_user():
    """Replacing an avatar should replace it, not accumulate orphans."""
    first = build_media_key("sahil", "profile", JPEG)
    second = build_media_key("sahil", "profile", JPEG)

    assert first == second == "sahil/profile/avatar.jpg"
    assert build_media_key("sahil", "cover", JPEG) == "sahil/cover/banner.jpg"


def test_an_event_cover_gets_its_own_object():
    """The regression this file exists for.

    Event covers were uploaded with the `cover` purpose, so every event a host published
    overwrote their profile banner and then displayed that banner as the event's cover —
    two unrelated things sharing one S3 object.
    """
    first = build_media_key("sahil", "event", JPEG)
    second = build_media_key("sahil", "event", JPEG)

    assert first != second, "two events by one host must not share an object"
    assert first.startswith("sahil/events/")
    assert "banner" not in first


def test_an_event_cover_never_collides_with_the_profile_banner():
    event = build_media_key("sahil", "event", JPEG)
    banner = build_media_key("sahil", "cover", JPEG)

    assert event != banner
    assert not event.startswith("sahil/cover/")


def test_a_named_event_keeps_a_stable_key():
    """Re-uploading a cover for the same event replaces it rather than leaving the old file
    behind, the same way pieces and posts behave."""
    assert build_media_key("sahil", "event", JPEG, "evt-1") == \
        build_media_key("sahil", "event", JPEG, "evt-1")


@pytest.mark.parametrize("purpose", ["piece", "post", "event", "chat"])
def test_every_repeatable_purpose_is_addressed_by_id(purpose):
    """A purpose that can happen twice for one user and has no id in its path is the bug
    above, waiting to be repeated."""
    assert build_media_key("u", purpose, JPEG) != build_media_key("u", purpose, JPEG)


def test_the_api_accepts_exactly_the_purposes_that_have_a_directory():
    """A purpose the controller accepts but PURPOSE_DIRS does not know falls back to using
    the raw purpose string as a folder name — which is how a typo becomes a new directory in
    the bucket rather than a 400."""
    import inspect

    from src.modules.media import media_controller

    source = inspect.getsource(media_controller.presign)
    accepted = {
        p for p in ("profile", "cover", "piece", "post", "chat", "event")
        if f'"{p}"' in source
    }
    assert accepted == set(PURPOSE_DIRS)


def test_the_username_is_lowercased_so_one_user_has_one_prefix():
    assert build_media_key("Sahil", "event", JPEG).startswith("sahil/")
