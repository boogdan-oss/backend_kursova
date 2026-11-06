import asyncio

import pytest

from app.collectors.ratelimit import (
    BLOCK_PAUSE_SEC,
    BLOCKS_BEFORE_COOLDOWN,
    CEILING_FACTOR,
    MIN_RPS,
    RPS_STEP,
    SUCCESSES_PER_PROBE,
    SUCCESSES_PER_STEP,
    AdaptiveRateLimiter,
)


class FakeClock:
    """Час іде лише тоді, коли limiter спить: тести детерміновані й миттєві."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_limiter(
    clock: FakeClock, initial_rps: float = 2, max_rps: float = 10, cooldown_sec: float = 300
) -> AdaptiveRateLimiter:
    return AdaptiveRateLimiter(initial_rps, max_rps, cooldown_sec, clock, clock.sleep)


async def test_requests_are_spaced_by_rate(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=2)
    started = clock.now

    for _ in range(5):
        await limiter.acquire()

    # Перший запит одразу, далі кожні 0.5 с — без сплеску
    assert clock.now - started == pytest.approx(2.0)


async def test_concurrent_waiters_share_the_rate(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=4)
    started = clock.now

    await asyncio.gather(*(limiter.acquire() for _ in range(5)))

    assert clock.now - started >= 1.0


def test_rate_grows_after_success_streak(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=2)

    for _ in range(SUCCESSES_PER_STEP - 1):
        limiter.on_success()
    assert limiter.rps == 2

    limiter.on_success()
    assert limiter.rps == 2 + RPS_STEP


def test_rate_does_not_exceed_max(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=9.8, max_rps=10)

    for _ in range(SUCCESSES_PER_STEP * 3):
        limiter.on_success()

    assert limiter.rps == 10


def test_initial_rate_is_capped_by_max(clock: FakeClock) -> None:
    assert make_limiter(clock, initial_rps=50, max_rps=10).rps == 10


def test_error_resets_streak_but_keeps_rate(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=2)

    for _ in range(SUCCESSES_PER_STEP - 1):
        limiter.on_success()
    limiter.on_error()
    limiter.on_success()

    assert limiter.rps == 2


def test_block_halves_rate(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=8)

    limiter.on_blocked()

    assert limiter.rps == 4
    assert limiter.state == "paused"
    assert limiter.paused_for_sec == pytest.approx(BLOCK_PAUSE_SEC)


def test_rate_never_falls_below_minimum(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=0.15)

    limiter.on_blocked()

    assert limiter.rps == MIN_RPS


async def test_retry_after_is_respected(clock: FakeClock) -> None:
    limiter = make_limiter(clock)
    await limiter.acquire()
    limiter.on_blocked(retry_after=120)
    blocked_at = clock.now

    await limiter.acquire()

    assert clock.now - blocked_at >= 120
    assert limiter.state == "ok"


def test_zero_retry_after_still_counts_as_block(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=8)

    limiter.on_blocked(retry_after=0)

    assert limiter.rps == 4
    assert limiter.state == "ok"


def test_responses_in_flight_count_as_one_block(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=8)

    for _ in range(4):
        limiter.on_blocked()

    assert limiter.rps == 4
    assert limiter.state == "paused"


def test_longer_retry_after_in_flight_extends_pause(clock: FakeClock) -> None:
    limiter = make_limiter(clock)

    limiter.on_blocked(retry_after=10)
    limiter.on_blocked(retry_after=60)

    assert limiter.paused_for_sec == pytest.approx(60)


async def _block(limiter: AdaptiveRateLimiter, clock: FakeClock) -> None:
    clock.now += limiter.paused_for_sec
    limiter.on_blocked()


async def test_repeated_blocks_trigger_cooldown(clock: FakeClock) -> None:
    limiter = make_limiter(clock, cooldown_sec=300)

    for _ in range(BLOCKS_BEFORE_COOLDOWN):
        await _block(limiter, clock)

    assert limiter.state == "cooldown"
    assert limiter.paused_for_sec == pytest.approx(300)

    clock.now += 300
    assert limiter.state == "ok"


def succeed(limiter: AdaptiveRateLimiter, times: int) -> None:
    for _ in range(times):
        limiter.on_success()


def test_rate_recovers_only_up_to_ceiling_after_block(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=10)

    limiter.on_blocked()
    clock.now += limiter.paused_for_sec
    succeed(limiter, SUCCESSES_PER_STEP * 50)

    assert limiter.ceiling == pytest.approx(10 * CEILING_FACTOR)
    assert limiter.rps == pytest.approx(limiter.ceiling)


def test_ceiling_is_probed_only_rarely(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=10)
    limiter.on_blocked()
    clock.now += limiter.paused_for_sec
    succeed(limiter, SUCCESSES_PER_STEP * 4)
    assert limiter.rps == pytest.approx(limiter.ceiling)
    ceiling = limiter.ceiling

    succeed(limiter, SUCCESSES_PER_PROBE - 1)
    assert limiter.ceiling == ceiling

    limiter.on_success()
    assert limiter.ceiling == pytest.approx(ceiling + RPS_STEP)
    assert limiter.rps == pytest.approx(ceiling + RPS_STEP)


def test_probing_never_exceeds_configured_maximum(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=10, max_rps=10)

    succeed(limiter, SUCCESSES_PER_PROBE * 3)

    assert (limiter.rps, limiter.ceiling) == (10, 10)


async def test_only_first_block_of_series_lowers_ceiling(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=10)

    for _ in range(BLOCKS_BEFORE_COOLDOWN):
        await _block(limiter, clock)

    assert limiter.ceiling == pytest.approx(10 * CEILING_FACTOR)


async def test_block_right_after_cooldown_keeps_ceiling_and_extends_ban(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=10, cooldown_sec=300)
    for _ in range(BLOCKS_BEFORE_COOLDOWN):
        await _block(limiter, clock)

    await _block(limiter, clock)

    assert limiter.ceiling == pytest.approx(10 * CEILING_FACTOR)
    assert limiter.state == "cooldown"
    assert limiter.paused_for_sec == pytest.approx(300)


async def test_new_block_after_healthy_period_lowers_ceiling_again(clock: FakeClock) -> None:
    limiter = make_limiter(clock, initial_rps=10)
    await _block(limiter, clock)
    clock.now += limiter.paused_for_sec
    succeed(limiter, SUCCESSES_PER_STEP * 10)
    first_ceiling = limiter.ceiling

    limiter.on_blocked()

    assert limiter.ceiling == pytest.approx(first_ceiling * CEILING_FACTOR)


async def test_success_between_blocks_prevents_cooldown(clock: FakeClock) -> None:
    limiter = make_limiter(clock)

    for _ in range(BLOCKS_BEFORE_COOLDOWN * 2):
        await _block(limiter, clock)
        clock.now += limiter.paused_for_sec
        limiter.on_success()

    assert limiter.state == "ok"
