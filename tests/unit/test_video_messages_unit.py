"""Video messages (BACKEND_CHANGES B11): what a message attaches, and the upload size limits.

Runs without a database (`pytest tests/unit/test_video_messages.py --noconftest`); a video
message round-tripping through the API is in tests/api/test_video_messages.py.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.chat import chat_controller, chat_dao
from src.modules.media import media_controller
from src.shared.models.chat import ChatMessage, Conversation
from src.shared.storage import s3_service
from src.shared.utils.app_error import AppError

CDN = "https://cdn.test"
ME = SimpleNamespace(id=uuid.uuid4(), username="jordanlee", name="Jordan Lee", image=None)
OTHER = uuid.uuid4()
VIDEO = f"{CDN}/jordanlee/chat/clip.mp4"
POSTER = f"{CDN}/jordanlee/chat/clip.jpg"


@pytest.fixture(autouse=True)
def media_store(monkeypatch):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", CDN)


# --- what a message attaches ----------------------------------------------------------------

def test_a_video_with_its_poster():
    assert chat_dao.message_media({"videoUrl": f" {VIDEO} ", "thumbnailUrl": POSTER}) == {
        "image_url": None, "video_url": VIDEO, "thumbnail_url": POSTER,
    }
    assert chat_dao.message_media({"imageUrl": "", "videoUrl": None, "body": "hi"}) == {
        "image_url": None, "video_url": None, "thumbnail_url": None,
    }


def test_a_photo_and_a_video_together_or_a_poster_alone_are_refused():
    with pytest.raises(AppError):
        chat_dao.message_media({"imageUrl": POSTER, "videoUrl": VIDEO})
    with pytest.raises(AppError):
        chat_dao.message_media({"thumbnailUrl": POSTER})


def test_only_your_own_uploads():
    chat_dao.check_video_is_theirs("jordanlee", {"video_url": VIDEO, "thumbnail_url": POSTER})
    for url in (f"{CDN}/amarasart/chat/clip.mp4", "https://elsewhere.test/jordanlee/clip.mp4"):
        with pytest.raises(AppError):
            chat_dao.check_video_is_theirs("jordanlee", {"video_url": url})
    with pytest.raises(AppError):
        chat_dao.check_video_is_theirs("jordanlee", {"video_url": VIDEO, "thumbnail_url": f"{CDN}/x/p.jpg"})


def test_the_row_says_video_and_so_does_the_push():
    video = ChatMessage(body=None, image_url=None, video_url=VIDEO)
    assert chat_dao.message_preview(video) == "Video"
    assert chat_dao.message_preview(ChatMessage(body=None, image_url=POSTER)) == "Photo"
    assert chat_dao.message_preview(ChatMessage(body="Hi", video_url=VIDEO)) == "Hi"
    assert chat_dao.message_preview(None) is None
    assert chat_dao.push_text(None, {"video_url": VIDEO}) == "Sent a video"
    assert chat_dao.push_text(None, {"image_url": POSTER}) == "Sent a photo"
    assert chat_dao.push_text("Look", {"video_url": VIDEO}) == "Look"


def test_the_thread_returns_the_video_and_its_poster():
    message = ChatMessage(
        id=uuid.uuid4(), body=None, video_url=VIDEO, thumbnail_url=POSTER,
        created_at=datetime.now(timezone.utc),
    )
    data = chat_dao.message_to_dict(message, None)
    assert data["videoUrl"] == VIDEO
    assert data["thumbnailUrl"] == POSTER
    assert data["imageUrl"] is None


# --- sending one ----------------------------------------------------------------------------

@pytest.fixture
def send(monkeypatch):
    """Calls `send_message` in an open chat between ME and OTHER."""
    sent = {}
    conversation = Conversation(
        id=uuid.uuid4(), participant_one_id=ME.id, participant_two_id=OTHER, status="open",
        last_message_at=datetime.now(timezone.utc),
    )

    def create_message(_db, _conversation, _sender, body=None, **media):
        sent["body"], sent["media"] = body, media
        return ChatMessage(
            id=uuid.uuid4(), body=body, created_at=datetime.now(timezone.utc),
            image_url=media.get("image_url"), video_url=media.get("video_url"),
            thumbnail_url=media.get("thumbnail_url"),
        )

    db = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(chat_controller, "SessionLocal", lambda: db)
    monkeypatch.setattr(chat_controller, "get_user_by_id", lambda _db, _id: ME)
    monkeypatch.setattr(chat_dao, "get_conversation", lambda _db, _id: conversation)
    monkeypatch.setattr(chat_dao, "create_message", create_message)
    monkeypatch.setattr(chat_dao, "is_muted_for", lambda *_a: False)
    monkeypatch.setattr(chat_controller, "_broadcast_new_message", lambda *_a: None)
    monkeypatch.setattr(
        chat_controller.notifications_dao, "push_only",
        lambda _db, **kw: sent.setdefault("push", kw["body"]),
    )

    def run(body):
        with Flask(__name__).test_request_context(json=body):
            g.user = {"id": str(ME.id)}
            data, status = chat_controller.send_message(str(conversation.id))
        assert status == 201
        return data

    run.sent = sent
    return run


def test_a_video_message_is_stored_returned_and_pushed(send):
    data = send({"videoUrl": VIDEO, "thumbnailUrl": POSTER})
    assert send.sent["media"] == {"image_url": None, "video_url": VIDEO, "thumbnail_url": POSTER}
    assert data["videoUrl"] == VIDEO and data["thumbnailUrl"] == POSTER
    assert send.sent["push"] == "Jordan Lee: Sent a video"


def test_a_video_from_someone_elses_account_is_refused(send):
    with pytest.raises(AppError):
        send({"videoUrl": f"{CDN}/amarasart/chat/clip.mp4"})
    assert "media" not in send.sent


def test_an_empty_message_is_still_refused(send):
    with pytest.raises(AppError):
        send({"thumbnailUrl": None})


# --- the upload size --------------------------------------------------------------------------

MB = 1024 * 1024


def test_a_chat_video_must_say_its_size_and_stay_under_100_mb():
    with pytest.raises(AppError) as missing:
        s3_service.check_upload_size("chat", "video/mp4", None)
    assert missing.value.status_code == 400
    with pytest.raises(AppError) as big:
        s3_service.check_upload_size("chat", "video/mp4", 100 * MB + 1)
    assert big.value.status_code == 413
    s3_service.check_upload_size("chat", "video/mp4", 100 * MB)
    with pytest.raises(AppError):
        s3_service.check_upload_size("chat", "video/mp4", 0)


def test_photos_keep_20_mb_and_other_uploads_need_not_say_their_size():
    with pytest.raises(AppError) as big:
        s3_service.check_upload_size("chat", "image/jpeg", 20 * MB + 1)
    assert big.value.status_code == 413
    s3_service.check_upload_size("chat", "image/jpeg", None)
    s3_service.check_upload_size("post", "video/mp4", None)


@pytest.fixture
def s3(monkeypatch):
    """A configured bucket; records how each URL was signed."""
    signed = []

    class _Client:
        def __init__(self, version):
            self.version = version

        def generate_presigned_url(self, _op, Params, ExpiresIn):
            signed.append({"version": self.version, "params": Params})
            return "https://bucket.test/signed"

    monkeypatch.setattr(s3_service, "s3_configured", lambda: True)
    monkeypatch.setattr(s3_service, "get_bucket", lambda: "bucket")
    monkeypatch.setattr(s3_service, "get_s3_client", lambda version=None: _Client(version))
    return signed


def test_a_stated_size_is_signed_into_the_upload_url(s3):
    s3_service.presign_put("jordanlee", "chat", "video/mp4", content_length=5 * MB)
    assert s3[0]["version"] == "s3v4"
    assert s3[0]["params"]["ContentLength"] == 5 * MB


def test_uploads_without_a_size_are_signed_as_before(s3):
    s3_service.presign_put("jordanlee", "post", "image/jpeg")
    assert s3[0]["version"] is None
    assert "ContentLength" not in s3[0]["params"]


@pytest.mark.parametrize("length", ["5000", True, 1.5])
def test_presign_wants_a_whole_number_of_bytes(length):
    with Flask(__name__).test_request_context(
        json={"purpose": "chat", "contentType": "video/mp4", "contentLength": length}
    ):
        g.user = {"id": str(ME.id)}
        with pytest.raises(AppError) as error:
            media_controller.presign()
    assert error.value.status_code == 400
