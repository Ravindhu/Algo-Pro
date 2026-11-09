"""Order-rate throttle (flow item 24): 8 per second, waits up to 2 s, then refuses (not sent)."""
from quant.execution.openalgo_client import AnalyzeBroker
from quant.monitor.throttle import TokenBucket, acquire


def test_bucket_allows_capacity_then_refills_at_rate():
    b = TokenBucket(rate_per_sec=8, capacity=8)
    assert all(b.allow(0.0) for _ in range(8))
    assert b.allow(0.0) is False  # 9th in the same instant
    assert b.allow(0.125) is True  # one token back after 1/8 s


def test_acquire_waits_then_succeeds_with_fake_clock():
    now = [0.0]
    b = TokenBucket(8, 8)
    for _ in range(8):
        b.allow(0.0)
    waits = []

    def sleep(dt):
        waits.append(dt)
        now[0] += dt

    assert acquire(b, clock=lambda: now[0], sleep=sleep, timeout=2.0) is True
    assert sum(waits) > 0


def test_acquire_gives_up_after_timeout():
    b = TokenBucket(rate_per_sec=0.0, capacity=0.0)  # never refills
    now = [0.0]

    def sleep(dt):
        now[0] += dt

    assert acquire(b, clock=lambda: now[0], sleep=sleep, timeout=0.5) is False


def test_broker_rate_is_eighty_percent_of_openalgo_limit():
    assert AnalyzeBroker.ORDER_RATE_PER_SEC == 8
