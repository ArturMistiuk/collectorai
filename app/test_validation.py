"""
Run: python -m pytest test_validation.py -v
(or just: python test_validation.py  -- has a __main__ runner too)
"""
from validation import validate_offer, ProposedOffer, Decision, Tier


def test_full_payment_accepted():
    r = validate_offer(ProposedOffer(total_amount=1000, payments=[1000], cadence="lump_sum"))
    assert r.decision == Decision.ACCEPT
    assert r.tier == Tier.FULL_PAYMENT


def test_downpayment_plus_one_accepted():
    r = validate_offer(ProposedOffer(total_amount=1000, payments=[600, 400]))
    assert r.decision == Decision.ACCEPT
    assert r.tier == Tier.DOWNPAYMENT_PLUS_ONE


def test_downpayment_below_floor_countered():
    # 100 is 10% of 1000, below the 25% floor -> must counter, not silently accept
    r = validate_offer(ProposedOffer(total_amount=1000, payments=[900, 100]))
    assert r.decision == Decision.COUNTER
    assert all(p >= 250 - 0.01 for p in r.counter_terms["payments"])


def test_settlement_20pct_off_accepted():
    r = validate_offer(ProposedOffer(total_amount=800, payments=[400, 400], cadence="monthly"))
    assert r.decision == Decision.ACCEPT
    assert r.tier == Tier.SETTLEMENT


def test_settlement_below_800_countered():
    # consumer lowballs to $500 -- must NOT be accepted, discount cap is 20%
    r = validate_offer(ProposedOffer(total_amount=500, payments=[500]))
    assert r.decision == Decision.COUNTER
    assert r.decision != Decision.ACCEPT


def test_settlement_too_many_payments_countered():
    r = validate_offer(ProposedOffer(total_amount=800, payments=[200, 200, 200, 200], cadence="monthly"))
    assert r.decision == Decision.COUNTER
    assert len(r.counter_terms["payments"]) <= 3


def test_payment_plan_accepted():
    r = validate_offer(ProposedOffer(
        total_amount=1000, payments=[250, 250, 250, 250], cadence="monthly", span_weeks=12
    ))
    assert r.decision == Decision.ACCEPT
    assert r.tier == Tier.PAYMENT_PLAN


def test_payment_plan_too_many_installments_countered():
    # consumer asks for 10 payments of 100 -- violates the 25% floor (100 < 250)
    r = validate_offer(ProposedOffer(
        total_amount=1000, payments=[100] * 10, cadence="monthly", span_weeks=40
    ))
    assert r.decision == Decision.COUNTER
    assert len(r.counter_terms["payments"]) <= 4


def test_lowball_10_dollars_countered_not_accepted():
    # the "uncooperative consumer" test case: $10 total
    r = validate_offer(ProposedOffer(total_amount=10, payments=[10]))
    assert r.decision != Decision.ACCEPT


def test_mismatched_sum_rejected_gracefully():
    r = validate_offer(ProposedOffer(total_amount=1000, payments=[100, 100]))  # doesn't sum to 1000
    assert r.decision == Decision.COUNTER


if __name__ == "__main__":
    import inspect
    tests = [f for name, f in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:  # noqa: F841
            print(f"FAIL: {t.__name__} -> {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
