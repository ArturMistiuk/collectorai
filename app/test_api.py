import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_PATH"] = _tmp.name

from main import app, _init_db  # noqa: E402

_init_db()
client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_validate_offer_full_payment_accepted():
    r = client.post("/validate-offer", json={
        "total_amount": 1000, "payments": [1000], "cadence": "lump_sum",
    })
    assert r.status_code == 200
    assert r.json()["decision"] == "ACCEPT"
    assert r.json()["tier"] == "FULL_PAYMENT"


def test_validate_offer_lowball_countered():
    r = client.post("/validate-offer", json={
        "total_amount": 50, "payments": [50], "cadence": "lump_sum",
    })
    assert r.status_code == 200
    assert r.json()["decision"] == "COUNTER"


def test_validate_offer_handles_field_name_typo():
    r = client.post("/validate-offer", json={
        "total_amounts": 1000, "payments": [1000], "cadence": "lump_sum", "span_weeks": "",
    })
    assert r.status_code == 200
    assert r.json()["decision"] == "ACCEPT"


def test_check_compliance_allowed():
    r = client.post("/check-compliance", json={
        "text": "Your balance is $1,000 and has been outstanding for over 180 days.",
    })
    assert r.json()["allowed"] is True


def test_check_compliance_blocked():
    r = client.post("/check-compliance", json={
        "text": "We will have you arrested if you don't pay.",
    })
    data = r.json()
    assert data["allowed"] is False
    assert "arrested" in data["triggered"]


def test_log_agreement():
    r = client.post("/log-agreement", json={
        "call_id": "test-1", "tier": "FULL_PAYMENT",
        "total_amount": 1000, "payments": [1000], "cadence": "lump_sum",
    })
    assert r.json()["status"] == "logged"


def test_log_outcome_dispute():
    r = client.post("/log-outcome", json={
        "call_id": "test-2", "outcome": "DISPUTE", "notes": "Consumer says debt is not theirs",
    })
    assert r.json()["outcome"] == "DISPUTE"


def test_log_outcome_dnc():
    r = client.post("/log-outcome", json={
        "call_id": "test-3", "outcome": "DNC_REQUEST",
    })
    assert r.json()["outcome"] == "DNC_REQUEST"


def test_log_outcome_rejects_invalid():
    r = client.post("/log-outcome", json={"call_id": "test-4", "outcome": "INVALID"})
    assert r.status_code == 422


def test_call_ended_correlates_agreement():
    client.post("/log-agreement", json={
        "call_id": "end-1", "tier": "SETTLEMENT",
        "total_amount": 800, "payments": [400, 400], "cadence": "monthly",
    })
    r = client.post("/call-ended", json={
        "message": {"call": {"id": "end-1"}, "transcript": "done", "endedReason": "assistant-ended-call"}
    })
    assert r.json()["outcome"] == "AGREED_SETTLEMENT"


def test_call_ended_no_agreement():
    r = client.post("/call-ended", json={
        "message": {"call": {"id": "end-none"}, "transcript": "hung up", "endedReason": "customer-ended-call"}
    })
    assert r.json()["outcome"] == "NO_AGREEMENT"


def test_call_ended_correlates_dispute():
    client.post("/log-outcome", json={"call_id": "end-dispute", "outcome": "DISPUTE"})
    r = client.post("/call-ended", json={
        "message": {"call": {"id": "end-dispute"}, "transcript": "disputed", "endedReason": "assistant-ended-call"}
    })
    assert r.json()["outcome"] == "DISPUTE"
