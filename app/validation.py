"""
Waterfall validation logic for Corafone debt-collection agent.

This module is intentionally LLM-free. The voice agent NEVER decides on its own
whether an offer is acceptable — it always calls validate_offer() via the
/validate-offer HTTP endpoint (see main.py) and relays the result to the consumer.

Business rules (from task spec):
  Debt amount: $1000, 180+ days delinquent.

  Preference order (agent should always try to close the highest tier first):
    1. FULL_PAYMENT        - full $1000, single payment
    2. DOWNPAYMENT_PLUS_ONE - highest possible downpayment + exactly one more payment,
                              total = $1000 (no discount)
    3. SETTLEMENT          - discount up to 20% off (total >= $800), max 3 payments
    4. PAYMENT_PLAN        - no discount (total = $1000), spread over max 3 months,
                              cadence: biweekly / weekly / monthly

  Hard floor (applies to every tier, every payment in the plan, including the
  first/downpayment): no single payment may be less than 25% of the AGREED total.
  This is a design decision — see README "Key decisions" — 25% is measured against
  the agreed total for that tier, not the original $1000, so a $800 settlement has
  a $200 floor per payment.

  Consequence of the 25% floor: no tier can ever have more than 4 payments
  (4 x 25% = 100%), regardless of how many months/weeks the consumer asks for.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


DEBT_TOTAL = 1000.0
SETTLEMENT_MIN_TOTAL = 800.0  # 1000 * (1 - 0.20)
MIN_PAYMENT_FRACTION = 0.25
MAX_SETTLEMENT_PAYMENTS = 3
MAX_PLAN_PAYMENTS = 4  # implied by the 25% floor, not an independent rule
MAX_PLAN_SPAN_WEEKS = 13  # "3 months max"

VALID_CADENCES = {"biweekly", "weekly", "monthly", "lump_sum"}


class Tier(str, Enum):
    FULL_PAYMENT = "FULL_PAYMENT"
    DOWNPAYMENT_PLUS_ONE = "DOWNPAYMENT_PLUS_ONE"
    SETTLEMENT = "SETTLEMENT"
    PAYMENT_PLAN = "PAYMENT_PLAN"


class Decision(str, Enum):
    ACCEPT = "ACCEPT"
    COUNTER = "COUNTER"
    REJECT = "REJECT"  # only used when no viable counter exists (should be rare)


@dataclass
class ProposedOffer:
    """What the consumer proposed, as parsed by the voice agent from the call."""
    total_amount: float
    payments: List[float]          # ordered list of payment amounts, first = downpayment if >1
    cadence: str = "lump_sum"      # biweekly | weekly | monthly | lump_sum
    span_weeks: float = 0.0        # time from first to last payment


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
    # allow a small epsilon for floating point / rounded voice input
    return all(p >= floor - 0.01 for p in payments)


def _round2(x: float) -> float:
    return round(x + 1e-9, 2)


def _best_fallback_offer(proposed_total: float) -> dict:
    """
    Called when a proposal doesn't fit any tier. Builds the best realistic
    counter-offer we can still recommend, working DOWN the preference order.
    """
    # If they can plausibly do full $1000, counter with 2-payment split at the floor.
    if proposed_total >= SETTLEMENT_MIN_TOTAL:
        # Counter at settlement floor: $800 total, 2 payments of $400 each.
        total = max(SETTLEMENT_MIN_TOTAL, min(proposed_total, DEBT_TOTAL))
        payment = _round2(total / 2)
        return {
            "tier": Tier.SETTLEMENT.value,
            "total_amount": _round2(total),
            "payments": [payment, payment],
            "cadence": "monthly",
        }
    # Below settlement floor entirely: fall back to full payment plan, max payments,
    # each at the 25% floor of $1000 -> 4 payments of $250.
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

    # sanity: payments must sum to (approximately) the stated total
    if abs(sum(payments) - total) > max(1.0, 0.02 * total):
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=None,
            counter_terms=_best_fallback_offer(total),
            reason="Payment amounts don't add up to the stated total; re-propose.",
        )

    # --- Tier 1: FULL_PAYMENT ---
    if n == 1 and total >= DEBT_TOTAL - 0.01:
        return ValidationResult(
            decision=Decision.ACCEPT,
            tier=Tier.FULL_PAYMENT,
            accepted_terms={"total_amount": DEBT_TOTAL, "payments": [DEBT_TOTAL], "cadence": "lump_sum"},
            reason="Full payment in a single transaction.",
        )

    # --- Tier 2: DOWNPAYMENT_PLUS_ONE ---
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
            # Keep the downpayment as high as possible; only shrink it if that's
            # what's needed to bring the final payment up to the 25% floor.
            if final_payment < floor:
                final_payment = floor
                downpayment = _round2(total - final_payment)
            elif downpayment < floor:
                downpayment = floor
                final_payment = _round2(total - downpayment)
            adjusted = [downpayment, final_payment]
            return ValidationResult(
                decision=Decision.COUNTER,
                tier=Tier.DOWNPAYMENT_PLUS_ONE,
                counter_terms={"total_amount": DEBT_TOTAL, "payments": adjusted, "cadence": offer.cadence},
                reason="One of the two payments is below the 25% floor; adjusted split proposed (downpayment kept as high as possible).",
            )

    # --- Tier 3: SETTLEMENT ---
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

    # settlement total below the 20%-off cap ($800) -> can't accept as settlement
    if SETTLEMENT_MIN_TOTAL - 0.01 > total > 0:
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=Tier.SETTLEMENT,
            counter_terms=_best_fallback_offer(total),
            reason="Proposed total is below the 20% maximum discount ($800 floor).",
        )

    # settlement structure ok on amount but too many payments requested
    if total >= SETTLEMENT_MIN_TOTAL - 0.01 and n > MAX_SETTLEMENT_PAYMENTS and total < DEBT_TOTAL - 0.01:
        payment = _round2(total / MAX_SETTLEMENT_PAYMENTS)
        counter_payments = [payment] * MAX_SETTLEMENT_PAYMENTS
        counter_payments[-1] = _round2(total - sum(counter_payments[:-1]))
        return ValidationResult(
            decision=Decision.COUNTER,
            tier=Tier.SETTLEMENT,
            counter_terms={"total_amount": _round2(total), "payments": counter_payments, "cadence": offer.cadence},
            reason="Settlement discount is fine, but max 3 payments allowed; consolidated the schedule.",
        )

    # --- Tier 4: PAYMENT_PLAN ---
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
        # build a corrected plan: cap payments at 4, cap span at 13 weeks, valid cadence
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

    # Nothing fits (total below $800 with no other valid structure) -> best fallback
    return ValidationResult(
        decision=Decision.COUNTER,
        tier=None,
        counter_terms=_best_fallback_offer(total),
        reason="Proposal doesn't fit any approved tier; offering the best available alternative.",
    )
