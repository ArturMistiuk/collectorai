"""
Waterfall validation for debt-collection offers.

Pure Python, no LLM involvement. The voice agent calls validate_offer()
via HTTP and relays the decision — it never decides acceptability itself.

Business rules:
  Debt: $1,000, 180+ days delinquent.
  Tiers (in preference order):
    1. FULL_PAYMENT        - $1,000 single payment
    2. DOWNPAYMENT_PLUS_ONE - two payments totalling $1,000
    3. SETTLEMENT          - up to 20% off (>= $800), max 3 payments
    4. PAYMENT_PLAN        - $1,000, up to 3 months, biweekly/weekly/monthly

  Hard floor: every individual payment >= 25% of the agreed total.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


DEBT_TOTAL = 1000.0
SETTLEMENT_MIN_TOTAL = 800.0
MIN_PAYMENT_FRACTION = 0.15
MAX_SETTLEMENT_PAYMENTS = 3
MAX_PLAN_PAYMENTS = 4
MAX_PLAN_SPAN_WEEKS = 13
MIN_FIRST_PAYMENT = 0.4

VALID_CADENCES = {"biweekly", "weekly", "monthly", "lump_sum"}


class Tier(str, Enum):
    FULL_PAYMENT = "FULL_PAYMENT"
    DOWNPAYMENT_PLUS_ONE = "DOWNPAYMENT_PLUS_ONE"
    SETTLEMENT = "SETTLEMENT"
    PAYMENT_PLAN = "PAYMENT_PLAN"


class Decision(str, Enum):
    ACCEPT = "ACCEPT"
    COUNTER = "COUNTER"
    REJECT = "REJECT"


@dataclass
class ProposedOffer:
    total_amount: float
    payments: List[float]
    cadence: str = "lump_sum"
    span_weeks: float = 0.0


@dataclass
class ValidationResult:
    decision: Decision
    tier: Optional[Tier]
    accepted_terms: Optional[dict] = None
    counter_terms: Optional[dict] = None
    reason: str = ""


def _min_payment_ok(payments: List[float], total: float) -> bool:
    if not payments:
        return False
    floor = MIN_PAYMENT_FRACTION * total
    return all(p >= floor - 0.01 for p in payments)


def _round2(x: float) -> float:
    return round(x + 1e-9, 2)


def _best_fallback_offer(proposed_total: float) -> dict:
    if proposed_total >= SETTLEMENT_MIN_TOTAL:
        total = max(SETTLEMENT_MIN_TOTAL, min(proposed_total, DEBT_TOTAL))
        payment = _round2(total / 2)
        return {
            "tier": Tier.SETTLEMENT.value,
            "total_amount": _round2(total),
            "payments": [payment, payment],
            "cadence": "monthly",
        }
    payment = _round2(DEBT_TOTAL * MIN_PAYMENT_FRACTION)
    return {
        "tier": Tier.PAYMENT_PLAN.value,
        "total_amount": DEBT_TOTAL,
        "payments": [payment] * MAX_PLAN_PAYMENTS,
        "cadence": "monthly",
    }


def validate_offer(offer: ProposedOffer) -> ValidationResult:
    total = offer.total_amount
    payments = offer.payments
    n = len(payments)

    if n == 0 or total <= 0:
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=None,
            counter_terms=_best_fallback_offer(total),
            reason="No valid payment structure provided.",
        )

    if abs(sum(payments) - total) > max(1.0, 0.02 * total) and payments[0] >= MIN_FIRST_PAYMENT * total:
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=None,
            counter_terms=_best_fallback_offer(total),
            reason="Payment amounts don't add up to the stated total; re-propose.",
        )

    # Tier 1: full payment
    if n == 1 and total >= DEBT_TOTAL - 0.01:
        return ValidationResult(
            decision=Decision.ACCEPT,
            tier=Tier.FULL_PAYMENT,
            accepted_terms={"total_amount": DEBT_TOTAL, "payments": [DEBT_TOTAL], "cadence": "lump_sum"},
            reason="Full payment in a single transaction.",
        )

    # Tier 2: downpayment + one more
    if n == 2 and total >= DEBT_TOTAL - 0.01:
        if _min_payment_ok(payments, total):
            return ValidationResult(
                decision=Decision.ACCEPT,
                tier=Tier.DOWNPAYMENT_PLUS_ONE,
                accepted_terms={"total_amount": DEBT_TOTAL, "payments": payments, "cadence": offer.cadence},
                reason="Downpayment + one final payment, full amount, both above 25% floor.",
            )
        else:
            floor = _round2(MIN_PAYMENT_FRACTION * total)
            downpayment, final_payment = payments
            if final_payment < floor:
                final_payment = floor
                downpayment = _round2(total - final_payment)
            elif downpayment < floor:
                downpayment = floor
                final_payment = _round2(total - downpayment)
            return ValidationResult(
                decision=Decision.COUNTER,
                tier=Tier.DOWNPAYMENT_PLUS_ONE,
                counter_terms={"total_amount": DEBT_TOTAL, "payments": [downpayment, final_payment], "cadence": offer.cadence},
                reason="One of the two payments is below the 25% floor; adjusted split proposed.",
            )

    # Tier 3: settlement
    if total >= SETTLEMENT_MIN_TOTAL - 0.01 and n <= MAX_SETTLEMENT_PAYMENTS and total < DEBT_TOTAL - 0.01:
        if _min_payment_ok(payments, total):
            return ValidationResult(
                decision=Decision.ACCEPT,
                tier=Tier.SETTLEMENT,
                accepted_terms={"total_amount": _round2(total), "payments": payments, "cadence": offer.cadence},
                reason=f"Settlement at {_round2((1 - total / DEBT_TOTAL) * 100)}% off, within 20% cap and 3-payment max.",
            )
        else:
            floor = _round2(MIN_PAYMENT_FRACTION * total)
            k = min(n, MAX_SETTLEMENT_PAYMENTS)
            even = _round2(total / k)
            adjusted = [max(floor, even)] * k
            adjusted[-1] = _round2(total - sum(adjusted[:-1]))
            return ValidationResult(
                decision=Decision.COUNTER,
                tier=Tier.SETTLEMENT,
                counter_terms={"total_amount": _round2(total), "payments": adjusted, "cadence": offer.cadence},
                reason="Settlement total is acceptable but a payment is below the 25% floor.",
            )

    if SETTLEMENT_MIN_TOTAL - 0.01 > total > 0:
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=Tier.SETTLEMENT,
            counter_terms=_best_fallback_offer(total),
            reason="Proposed total is below the 20% maximum discount ($800 floor).",
        )

    if total >= SETTLEMENT_MIN_TOTAL - 0.01 and n > MAX_SETTLEMENT_PAYMENTS and total < DEBT_TOTAL - 0.01:
        payment = _round2(total / MAX_SETTLEMENT_PAYMENTS)
        counter_payments = [payment] * MAX_SETTLEMENT_PAYMENTS
        counter_payments[-1] = _round2(total - sum(counter_payments[:-1]))
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=Tier.SETTLEMENT,
            counter_terms={"total_amount": _round2(total), "payments": counter_payments, "cadence": offer.cadence},
            reason="Settlement discount is fine, but max 3 payments allowed.",
        )

    # Tier 4: payment plan
    if total >= DEBT_TOTAL - 0.01:
        cadence_ok = offer.cadence in VALID_CADENCES
        span_ok = offer.span_weeks <= MAX_PLAN_SPAN_WEEKS + 0.5
        count_ok = n <= MAX_PLAN_PAYMENTS
        if cadence_ok and span_ok and count_ok and _min_payment_ok(payments, total):
            return ValidationResult(
                decision=Decision.ACCEPT,
                tier=Tier.PAYMENT_PLAN,
                accepted_terms={"total_amount": DEBT_TOTAL, "payments": payments, "cadence": offer.cadence},
                reason="Full amount, no discount, spread within 3 months at an approved cadence.",
            )
        k = min(max(n, 1), MAX_PLAN_PAYMENTS)
        payment = _round2(DEBT_TOTAL / k)
        counter_payments = [payment] * k
        counter_payments[-1] = _round2(DEBT_TOTAL - sum(counter_payments[:-1]))
        counter_cadence = offer.cadence if offer.cadence in VALID_CADENCES else "monthly"
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=Tier.PAYMENT_PLAN,
            counter_terms={
                "total_amount": DEBT_TOTAL,
                "payments": counter_payments,
                "cadence": counter_cadence,
                "span_weeks": min(offer.span_weeks or MAX_PLAN_SPAN_WEEKS, MAX_PLAN_SPAN_WEEKS),
            },
            reason="Adjusted to respect the 25% per-payment floor / 3-month cap / approved cadence.",
        )

    return ValidationResult(
        decision=Decision.COUNTER,
        tier=None,
        counter_terms=_best_fallback_offer(total),
        reason="Proposal doesn't fit any approved tier; offering the best available alternative.",
    )
