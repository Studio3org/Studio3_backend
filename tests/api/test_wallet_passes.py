"""Phase 3A: tickets in Google Wallet and Apple Wallet (BACKEND_CHANGES B25).

Real cryptography with throwaway credentials: a test CA stands in for Apple's WWDR and signs
a Pass Type ID certificate; a fresh RSA key stands in for the Google service account. The
Apple signature is verified with OpenSSL itself, the way Apple checks it.
"""
import base64
import hashlib
import io
import json
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from src.modules.events import ticketing_service
from src.modules.wallet import apple_pass, wallet_config, wallet_service
from src.shared.models.ticketing import TICKET_VOID, Ticket, TicketOrder
from tests.factories import make_event, make_user

PASS_TYPE = "pass.co.studio-3.test"


def _cert(subject, issuer_name, key, signing_key, *, ca=False):
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    return builder.sign(signing_key, hashes.SHA256())


@pytest.fixture
def apple_wallet(monkeypatch, tmp_path):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = _cert("Test WWDR", "Test WWDR", ca_key, ca_key, ca=True)
    pass_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pass_cert = _cert(f"Pass Type ID: {PASS_TYPE}", "Test WWDR", pass_key, ca_key)
    p12 = pkcs12.serialize_key_and_certificates(
        b"pass", pass_key, pass_cert, None, serialization.BestAvailableEncryption(b"secret"))
    monkeypatch.setenv("APPLE_PASS_TYPE_ID", PASS_TYPE)
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APPLE_PASS_CERT_P12", base64.b64encode(p12).decode())
    monkeypatch.setenv("APPLE_PASS_CERT_PASSWORD", "secret")
    monkeypatch.setenv("APPLE_WWDR_CERT",
                       base64.b64encode(ca.public_bytes(serialization.Encoding.DER)).decode())
    monkeypatch.setenv("BACKEND_URL", "https://api.studio-3.test")
    wallet_config.reset()
    ca_file = tmp_path / "wwdr.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    yield ca_file
    wallet_config.reset()


@pytest.fixture
def google_wallet(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "3388000000012345678")
    monkeypatch.setenv("GOOGLE_WALLET_SERVICE_ACCOUNT_JSON", json.dumps({
        "type": "service_account", "client_email": "wallet@studio3.iam.gserviceaccount.com",
        "private_key": pem, "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token",
    }))
    wallet_config.reset()
    yield key.public_key()
    wallet_config.reset()


def _bought(db, client, auth_headers, quantity=2):
    host, buyer = make_user(db, seller=True), make_user(db, name="Alex Buyer")
    event = make_event(db, host, status="published", latitude=32.78, longitude=-96.8,
                       address="1 Main St, Dallas")
    created = client.post(f"/api/events/{event.id}/ticket-types",
                          json={"name": "General Admission", "priceCents": 0},
                          headers=auth_headers(host)).get_json()["data"]
    order = client.post(f"/api/events/{event.id}/orders",
                        json={"items": [{"ticketTypeId": created["id"], "quantity": quantity}]},
                        headers=auth_headers(buyer)).get_json()["data"]
    tickets = client.get(f"/api/user/me/tickets?eventId={event.id}",
                         headers=auth_headers(buyer)).get_json()["data"]["tickets"]
    return event, buyer, order["id"], tickets


def _pass_url(event, order_id, platform=None, ticket=None):
    query = []
    if platform:
        query.append(f"platform={platform}")
    if ticket:
        query.append(f"ticketId={ticket}")
    return f"/api/events/{event.id}/orders/{order_id}/wallet-pass" + ("?" + "&".join(query) if query else "")


# --- availability ------------------------------------------------------------------------

def test_without_wallet_settings_nothing_is_offered(db, client, auth_headers):
    wallet_config.reset()
    event, buyer, order_id, tickets = _bought(db, client, auth_headers)

    assert tickets[0]["walletPassAvailable"] is False
    assert tickets[0]["walletPasses"] == {"apple": False, "google": False}
    assert client.get(_pass_url(event, order_id), headers=auth_headers(buyer)).status_code == 503


# --- Google -------------------------------------------------------------------------------

def test_google_save_link_carries_every_ticket_and_its_door_code(db, client, auth_headers, google_wallet):
    event, buyer, order_id, tickets = _bought(db, client, auth_headers)

    response = client.get(_pass_url(event, order_id, "google"), headers=auth_headers(buyer))

    assert response.status_code == 200
    url = response.get_json()["data"]["saveUrl"]
    assert url.startswith("https://pay.google.com/gp/v/save/")
    claims = jwt.decode(url.rsplit("/", 1)[1], google_wallet, algorithms=["RS256"], audience="google")
    assert claims["typ"] == "savetowallet"
    assert claims["iss"] == "wallet@studio3.iam.gserviceaccount.com"
    klass = claims["payload"]["eventTicketClasses"][0]
    objects = claims["payload"]["eventTicketObjects"]
    assert klass["id"] == f"3388000000012345678.event-{event.id}"
    assert sorted(o["barcode"]["value"] for o in objects) == sorted(t["code"] for t in tickets)
    assert {o["ticketHolderName"] for o in objects} == {"Alex Buyer"}
    assert all(o["classId"] == klass["id"] and o["state"] == "ACTIVE" for o in objects)


# --- Apple --------------------------------------------------------------------------------

def _verify_with_openssl(ca_file, manifest: bytes, signature: bytes, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    (tmp_path / "manifest.json").write_bytes(manifest)
    (tmp_path / "signature").write_bytes(signature)
    result = subprocess.run(
        ["openssl", "smime", "-verify", "-binary", "-inform", "DER",
         "-in", str(tmp_path / "signature"), "-content", str(tmp_path / "manifest.json"),
         "-CAfile", str(ca_file), "-purpose", "any", "-out", "/dev/null"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_an_apple_pass_is_signed_and_carries_the_door_code(db, client, auth_headers, apple_wallet, tmp_path):
    event, buyer, order_id, tickets = _bought(db, client, auth_headers, quantity=1)

    response = client.get(_pass_url(event, order_id), headers=auth_headers(buyer))

    assert response.status_code == 200
    assert response.mimetype == "application/vnd.apple.pkpass"
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    names = set(archive.namelist())
    assert {"pass.json", "manifest.json", "signature", "icon.png", "logo.png"} <= names
    manifest = json.loads(archive.read("manifest.json"))
    for name, digest in manifest.items():
        assert hashlib.sha1(archive.read(name)).hexdigest() == digest, name
    _verify_with_openssl(apple_wallet, archive.read("manifest.json"), archive.read("signature"), tmp_path)

    data = json.loads(archive.read("pass.json"))
    assert data["passTypeIdentifier"] == PASS_TYPE and data["teamIdentifier"] == "TEAM123456"
    assert data["serialNumber"] == tickets[0]["id"]
    assert data["barcodes"][0]["message"] == tickets[0]["code"]
    assert data["barcodes"][0]["altText"] == tickets[0]["number"]
    assert data["webServiceURL"] == "https://api.studio-3.test/api/wallet/apple"
    assert data["voided"] is False
    assert data["locations"] == [{"latitude": 32.78, "longitude": -96.8}]


def test_several_tickets_come_as_one_bundle(db, client, auth_headers, apple_wallet):
    event, buyer, order_id, tickets = _bought(db, client, auth_headers, quantity=3)

    bundle = client.get(_pass_url(event, order_id), headers=auth_headers(buyer))
    single = client.get(_pass_url(event, order_id, ticket=tickets[1]["id"]), headers=auth_headers(buyer))

    assert bundle.mimetype == "application/vnd.apple.pkpasses"
    assert len(zipfile.ZipFile(io.BytesIO(bundle.data)).namelist()) == 3
    assert single.mimetype == "application/vnd.apple.pkpass"


def test_only_the_buyer_gets_passes_and_never_for_dead_tickets(db, client, auth_headers, apple_wallet):
    event, buyer, order_id, _ = _bought(db, client, auth_headers)

    assert client.get(_pass_url(event, order_id), headers=auth_headers(make_user(db))).status_code == 404
    order = db.get(TicketOrder, uuid.UUID(order_id))
    order.status = "refunded"
    db.commit()
    assert client.get(_pass_url(event, order_id), headers=auth_headers(buyer)).status_code == 410


# --- Apple's pass web service --------------------------------------------------------------

def test_apple_devices_register_hear_of_changes_and_fetch_the_void_pass(
    db, client, auth_headers, apple_wallet, monkeypatch
):
    event, buyer, order_id, tickets = _bought(db, client, auth_headers, quantity=1)
    serial = tickets[0]["id"]
    auth = {"Authorization": f"ApplePass {apple_pass.authentication_token(serial)}"}
    base = "/api/wallet/apple/v1"
    pushed = []
    monkeypatch.setattr(wallet_service, "_push_apple", lambda tokens: pushed.extend(tokens))

    register = f"{base}/devices/dev1/registrations/{PASS_TYPE}/{serial}"
    assert client.post(register, json={"pushToken": "tok1"}, headers=auth).status_code == 201
    assert client.post(register, json={"pushToken": "tok1"}, headers=auth).status_code == 200
    assert client.post(register, json={"pushToken": "x"},
                       headers={"Authorization": "ApplePass wrong"}).status_code == 401

    listed = client.get(f"{base}/devices/dev1/registrations/{PASS_TYPE}").get_json()
    assert listed["serialNumbers"] == [serial]
    since = listed["lastUpdated"]
    assert client.get(f"{base}/devices/dev1/registrations/{PASS_TYPE}?passesUpdatedSince={since}").status_code == 204

    # Not going: the free ticket is voided, and the device is told.
    client.post(f"/api/events/{event.id}/rsvp", json={"going": False}, headers=auth_headers(buyer))

    assert pushed == ["tok1"]
    changed = client.get(f"{base}/devices/dev1/registrations/{PASS_TYPE}?passesUpdatedSince={since}")
    assert changed.get_json()["serialNumbers"] == [serial]
    latest = client.get(f"{base}/passes/{PASS_TYPE}/{serial}", headers=auth)
    assert json.loads(zipfile.ZipFile(io.BytesIO(latest.data)).read("pass.json"))["voided"] is True

    assert client.delete(register, headers=auth).status_code == 200
    assert client.get(f"{base}/devices/dev1/registrations/{PASS_TYPE}").status_code == 204


# --- the email ------------------------------------------------------------------------------

def test_the_ticket_email_has_wallet_buttons_and_the_apple_link_works_without_login(
    db, client, auth_headers, apple_wallet, google_wallet, monkeypatch
):
    sent = []
    monkeypatch.setattr("src.shared.notification.email_service.send_email",
                        lambda to, subject, html: sent.append(html) or True)
    event, buyer, order_id, _ = _bought(db, client, auth_headers, quantity=1)

    ticketing_service.send_confirmation(order_id)

    html = sent[-1]
    assert "Add to Apple Wallet" in html and "Add to Google Wallet" in html
    link = wallet_service.apple_download_url(order_id).replace("https://api.studio-3.test", "")
    assert client.get(link).mimetype == "application/vnd.apple.pkpass"
    assert client.get(link.replace("sig=", "sig=0")).status_code == 403


def test_google_passes_are_marked_inactive_when_a_ticket_is_voided(
    db, client, auth_headers, google_wallet, monkeypatch
):
    event, buyer, order_id, tickets = _bought(db, client, auth_headers, quantity=1)
    patched = []
    monkeypatch.setattr("src.modules.wallet.google_pass.patch_object",
                        lambda issuer, sa, ticket_id, body: patched.append((str(ticket_id), body)) or True)

    client.post(f"/api/events/{event.id}/rsvp", json={"going": False}, headers=auth_headers(buyer))

    assert patched == [(tickets[0]["id"], {"state": "INACTIVE"})]
    assert db.query(Ticket).filter_by(id=uuid.UUID(tickets[0]["id"])).one().status == TICKET_VOID
