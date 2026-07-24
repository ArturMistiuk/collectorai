"""
Corafone collections-agent backend.

Endpoints called BY Vapi (voice platform) during/after the call:

  POST /validate-offer   <- tool call mid-conversation
  POST /check-compliance <- guardrail before risky sentences
  POST /log-agreement    <- record final agreed terms
  POST /log-outcome      <- record dispute/DNC outcomes
  POST /call-ended       <- Vapi end-of-call webhook
  GET  /health           <- liveness check

Run locally:
  pip install fastapi uvicorn
  uvicorn main:app --reload --port 8000

Deploy: Railway / Render, see README.md.
"""

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from validation import ProposedOffer, validate_offer, Decision

DB_PATH = Path(os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "corafone.db")))

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("corafone")


# --------------------------------------------------------------------------- #
# App lifecycle & storage
# --------------------------------------------------------------------------- #

def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agreements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT,
            tier TEXT,
            total_amount REAL,
            payments TEXT,
            cadence TEXT,
            created_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT,
            transcript TEXT,
            ended_reason TEXT,
            outcome TEXT,
            created_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compliance_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT,
            flagged_text TEXT,
            rule_triggered TEXT,
            created_at REAL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    yield


app = FastAPI(title="Corafone Collections Agent Backend", lifespan=lifespan)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# /validate-offer
# --------------------------------------------------------------------------- #

class OfferPayload(BaseModel):
    call_id: Optional[str] = None
    total_amount: float
    payments: List[float]
    cadence: str = "lump_sum"
    span_weeks: float = 0.0


@app.post("/validate-offer")
def validate_offer_endpoint(payload: OfferPayload):
    logger.info("validate-offer call_id=%s total=%.2f payments=%s", payload.call_id, payload.total_amount, payload.payments)
    offer = ProposedOffer(
        total_amount=payload.total_amount,
        payments=payload.payments,
        cadence=payload.cadence,
        span_weeks=payload.span_weeks,
    )
    result = validate_offer(offer)
    return {
        "decision": result.decision.value,
        "tier": result.tier.value if result.tier else None,
        "accepted_terms": result.accepted_terms,
        "counter_terms": result.counter_terms,
        "reason": result.reason,
    }


# --------------------------------------------------------------------------- #
# /check-compliance
# --------------------------------------------------------------------------- #

BANNED_PATTERNS = [
    r"\bjail\b", r"\barrest(ed)?\b", r"\bsue\b", r"\blawsuit\b", r"\bcourt\b",
    r"\bcrimina?l\b", r"\bwage garnish", r"\bseize\b", r"\brepossess",
    r"\bwarrant\b", r"\bpolice\b",
    r"only (today|right now|in the next)", r"last chance", r"final warning",
    r"immediately or", r"within the next \d+ minutes",
    r"destroy(ed)? your (credit|life)", r"ruin(ed)? your (credit|life)",
    r"never be able to", r"guarantee(d)? (approval|denial)",
]
BANNED_RE = re.compile("|".join(BANNED_PATTERNS), re.IGNORECASE)


class ComplianceCheckPayload(BaseModel):
    call_id: Optional[str] = None
    text: str


@app.post("/check-compliance")
def check_compliance(payload: ComplianceCheckPayload):
    match = BANNED_RE.search(payload.text)
    if match:
        logger.warning("Compliance flag call_id=%s triggered='%s'", payload.call_id, match.group(0))
        with get_db() as conn:
            conn.execute(
                "INSERT INTO compliance_flags (call_id, flagged_text, rule_triggered, created_at) VALUES (?, ?, ?, ?)",
                (payload.call_id, payload.text, match.group(0), time.time()),
            )
            conn.commit()
        return {
            "allowed": False,
            "triggered": match.group(0),
            "instruction": (
                "Do not say this. Rephrase without threats, invented consequences, "
                "or false urgency. Stick to factual account status and the payment "
                "options already approved by validate_offer."
            ),
        }
    return {"allowed": True}


# --------------------------------------------------------------------------- #
# /log-agreement
# --------------------------------------------------------------------------- #

class AgreementPayload(BaseModel):
    call_id: Optional[str] = None
    tier: str
    total_amount: float
    payments: List[float]
    cadence: str = "lump_sum"


@app.post("/log-agreement")
def log_agreement(payload: AgreementPayload):
    logger.info("log-agreement call_id=%s tier=%s total=%.2f", payload.call_id, payload.tier, payload.total_amount)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agreements (call_id, tier, total_amount, payments, cadence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (payload.call_id, payload.tier, payload.total_amount, json.dumps(payload.payments), payload.cadence, time.time()),
        )
        conn.commit()
    return {"status": "logged"}


# --------------------------------------------------------------------------- #
# /log-outcome
# --------------------------------------------------------------------------- #

class OutcomePayload(BaseModel):
    call_id: str
    outcome: str  # DISPUTE | DNC_REQUEST
    notes: Optional[str] = None


@app.post("/log-outcome")
def log_outcome(payload: OutcomePayload):
    if payload.outcome not in ("DISPUTE", "DNC_REQUEST"):
        raise HTTPException(status_code=422, detail="outcome must be DISPUTE or DNC_REQUEST")
    logger.info("log-outcome call_id=%s outcome=%s", payload.call_id, payload.outcome)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO calls (call_id, transcript, ended_reason, outcome, created_at) VALUES (?, ?, ?, ?, ?)",
            (payload.call_id, None, "agent_ended", payload.outcome, time.time()),
        )
        conn.commit()
    return {"status": "logged", "outcome": payload.outcome}


# --------------------------------------------------------------------------- #
# /call-ended  (Vapi end-of-call-report webhook)
# --------------------------------------------------------------------------- #

@app.post("/call-ended")
def call_ended(payload: dict):
    """
    Vapi sends a broad payload here; we pull out what we need.
    See: https://docs.vapi.ai/server-url  (server messages -> end-of-call-report)
    """
    message = payload.get("message", payload)
    call_id = (message.get("call") or {}).get("id") or message.get("callId")
    transcript = message.get("transcript") or message.get("artifact", {}).get("transcript", "")
    ended_reason = message.get("endedReason", "unknown")

    # Determine outcome by checking if an agreement or outcome was already logged
    outcome = "NO_AGREEMENT"
    with get_db() as conn:
        row = conn.execute("SELECT tier FROM agreements WHERE call_id = ? LIMIT 1", (call_id,)).fetchone()
        if row:
            outcome = f"AGREED_{row[0]}"
        else:
            row = conn.execute("SELECT outcome FROM calls WHERE call_id = ? AND outcome IN ('DISPUTE', 'DNC_REQUEST') LIMIT 1", (call_id,)).fetchone()
            if row:
                outcome = row[0]

        conn.execute(
            "INSERT INTO calls (call_id, transcript, ended_reason, outcome, created_at) VALUES (?, ?, ?, ?, ?)",
            (call_id, transcript, ended_reason, outcome, time.time()),
        )
        conn.commit()

    logger.info("call-ended call_id=%s outcome=%s ended_reason=%s", call_id, outcome, ended_reason)
    return {"status": "stored", "outcome": outcome}


@app.get("/health")
def health():
    return {"status": "ok"}
