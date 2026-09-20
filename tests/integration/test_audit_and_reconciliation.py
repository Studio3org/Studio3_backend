"""The audit trail, and checking the books against Stripe.

Both exist for failures that do not announce themselves: an ops action nobody can later
account for, and a ledger that has quietly stopped matching the money.
"""
import uuid

from src.modules.admin import audit_service, reconciliation
from src.shared.models.audit import ACTOR_SYSTEM, AuditEvent
from tests.factories import make_order, make_piece, make_user


# --- the audit trail ------------------------------------------------------------------------

def test_an_action_is_recorded_against_the_person_who_took_it(db):
    admin = make_user(db, is_admin=True)
    seller, buyer = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="sold")
    order = make_order(db, buyer=buyer, seller=seller, piece=piece, artwork_cents=300_00)

    audit_service.record(
        db, "refund_issued",
        actor=admin, subject_type="order", subject_id=order.id,
        detail={"amountCents": 300_00}, note="Damaged in transit",
    )

    row = db.query(AuditEvent).filter_by(subject_id=order.id).one()
    assert row.action == "refund_issued"
    assert row.actor_id == admin.id
    assert row.note == "Damaged in transit"
    assert row.detail["amountCents"] == 300_00


def test_the_actor_label_survives_the_account_being_renamed(db):
    """An audit row has to still make sense years later. Reading the name through the FK
    would show whatever the account is called today, not who did it at the time."""
    admin = make_user(db, is_admin=True)
    admin.name = "Original Name"
    db.commit()
    audit_service.record(db, "refund_issued", actor=admin, subject_type="order",
                         subject_id=uuid.uuid4())

    admin.name = "Renamed Since"
    db.commit()

    row = db.query(AuditEvent).one()
    assert "Original Name" in row.actor_label


def test_a_scheduled_job_is_distinguishable_from_a_person(db):
    """"The sweep cancelled this auction" and "somebody cancelled this auction" are different
    events, and only one of them has a person to ask about it."""
    audit_service.system(db, "winner_cascaded", subject_type="auction",
                         subject_id=uuid.uuid4())

    row = db.query(AuditEvent).one()
    assert row.actor_type == ACTOR_SYSTEM
    assert row.actor_id is None
    assert row.actor_label == "system"


def test_personal_details_are_kept_out_of_the_detail(db):
    """An audit row says what was done. Copying an address or a card into it just doubles the
    number of places that data has to be protected."""
    admin = make_user(db, is_admin=True)

    audit_service.record(
        db, "refund_issued", actor=admin, subject_type="order", subject_id=uuid.uuid4(),
        detail={
            "amountCents": 5000,
            "shipping_address_snapshot": {"line1": "1 Test St"},
            "email": "buyer@example.com",
            "clientSecret": "pi_secret_xyz",
        },
    )

    detail = db.query(AuditEvent).one().detail
    assert detail == {"amountCents": 5000}


def test_a_failed_audit_write_never_takes_the_action_down_with_it(db, monkeypatch):
    """The trade-off this makes deliberately: the trail may have holes, because letting
    bookkeeping veto a refund would mean the operator retries — and a retried refund is a
    second refund."""
    def _explode(*a, **kw):
        raise RuntimeError("database is having a bad day")

    monkeypatch.setattr(db, "add", _explode)

    result = audit_service.record(db, "refund_issued", subject_type="order",
                                  subject_id=uuid.uuid4())

    assert result is None, "a failed audit write must be swallowed, not raised"


def test_history_for_one_subject_comes_back_newest_first(db):
    admin = make_user(db, is_admin=True)
    order_id = uuid.uuid4()
    for action in ("shipment_created", "shipment_updated", "refund_issued"):
        audit_service.record(db, action, actor=admin, subject_type="order",
                             subject_id=order_id)
    audit_service.record(db, "refund_issued", actor=admin, subject_type="order",
                         subject_id=uuid.uuid4())

    rows = audit_service.for_subject(db, "order", order_id)

    assert len(rows) == 3, "history leaked in from another order"
    assert rows[0].action == "refund_issued"


# --- reconciliation ---------------------------------------------------------------------------

def test_reconciliation_is_skipped_rather_than_failed_when_stripe_is_unconfigured(db):
    """Unconfigured is the normal state in dev and on staging. Reporting it as drift would
    train people to ignore this."""
    result = reconciliation.reconcile(db)

    assert result["status"] == "skipped"
    assert "differenceCents" not in result


def test_matching_balances_reconcile(db, stripe_enabled, monkeypatch):
    monkeypatch.setattr(
        reconciliation, "_stripe_balance_cents", lambda: reconciliation._ledger_clearing_balance(db)
    )

    result = reconciliation.reconcile(db)

    assert result["status"] == "ok"
    assert result["differenceCents"] == 0


def test_a_small_difference_is_tolerated(db, stripe_enabled, monkeypatch):
    """Stripe's balance moves continuously; a charge landing mid-read is an ordinary race,
    not a defect."""
    ledger = reconciliation._ledger_clearing_balance(db)
    monkeypatch.setattr(reconciliation, "_stripe_balance_cents", lambda: ledger - 50)

    assert reconciliation.reconcile(db)["status"] == "ok"


def test_real_drift_is_reported_and_recorded(db, stripe_enabled, monkeypatch):
    """The failure this exists for is silent — nothing errors, the books just stop matching
    the money. So the finding has to outlive the log line that noticed it."""
    ledger = reconciliation._ledger_clearing_balance(db)
    monkeypatch.setattr(reconciliation, "_stripe_balance_cents", lambda: ledger - 500_00)

    result = reconciliation.reconcile(db)

    assert result["status"] == "drift"
    assert result["differenceCents"] == 500_00
    row = db.query(AuditEvent).filter_by(action="ledger_drift_detected").one()
    assert row.actor_type == ACTOR_SYSTEM
    assert row.detail["differenceCents"] == 500_00


def test_reconciliation_never_corrects_anything(db, stripe_enabled, monkeypatch):
    """An automatic correction would paper over the bug that caused the drift and destroy
    the evidence of it."""
    from src.shared.models.ledger import LedgerEntry, LedgerTransaction

    before_txns = db.query(LedgerTransaction).count()
    before_entries = db.query(LedgerEntry).count()
    monkeypatch.setattr(reconciliation, "_stripe_balance_cents", lambda: -999_00)

    reconciliation.reconcile(db)

    assert db.query(LedgerTransaction).count() == before_txns
    assert db.query(LedgerEntry).count() == before_entries


def test_what_is_owed_is_reported_alongside(db, stripe_enabled, monkeypatch):
    """A clearing balance that matches Stripe is still a problem if it is smaller than what
    is owed out of it."""
    monkeypatch.setattr(reconciliation, "_stripe_balance_cents", lambda: 0)

    liabilities = reconciliation.reconcile(db)["liabilities"]

    assert set(liabilities) == {"seller_payable", "tax_payable", "auction_escrow"}


def test_every_declared_action_has_something_that_writes_it(db):
    """A filter option that can never match is worse than no filter option.

    This caught seven actions that were declared and never recorded — including
    auction_cancelled, which is exactly the question ("who withdrew this and refunded four
    bidders?") an audit log exists to answer.
    """
    import pathlib
    import re

    from src.shared.models import audit

    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    written = "".join(
        f.read_text() for f in src.rglob("*.py") if f.name != "audit.py"
    )
    declared = re.findall(
        r'^(AUDIT_[A-Z_]+) = "([a-z_]+)"', (src / "shared/models/audit.py").read_text(), re.M
    )

    unwritten = [v for n, v in declared if n not in written and f'"{v}"' not in written]
    assert unwritten == [], f"declared but never recorded: {unwritten}"
    # And the dropdown offers all of them.
    assert set(audit.AUDIT_ACTIONS) == {v for _, v in declared}


def test_cancelling_an_auction_is_recorded_against_whoever_did_it(db):
    """Releases every bidder's hold, so "who withdrew this and why" is the question asked a
    week later — by which time the application log is gone."""
    from src.modules.bids import auction_cancellation
    from tests.factories import make_auction, make_bid

    seller, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    auction = make_auction(db, piece, seller, starting_bid_cents=300_00)
    make_bid(db, auction, bidder, 350_00)

    auction_cancellation.cancel_auction(
        db, piece, auction_cancellation.REASON_SELLER_CANCELLED,
        actor_id=seller.id, commit=True,
    )

    row = db.query(AuditEvent).filter_by(action="auction_cancelled").one()
    assert row.actor_id == seller.id
    assert row.detail["cancelledBids"] == 1
    assert row.subject_id == auction.id
