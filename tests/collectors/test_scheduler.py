from collections import Counter

import pytest

from app.collectors.scheduler import CollectScheduler, CollectTask, Priority

POPULAR_MIN_OFFERS = 20


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_scheduler(clock: FakeClock, min_refresh_sec: float = 0.0) -> CollectScheduler:
    return CollectScheduler("binance", min_refresh_sec, POPULAR_MIN_OFFERS, clock)


def run(scheduler: CollectScheduler, steps: int) -> list[CollectTask]:
    tasks = []
    for _ in range(steps):
        task = scheduler.next_task()
        assert task is not None
        scheduler.finish(task, None)
        tasks.append(task)
    return tasks


def test_every_pair_produces_both_sides(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    scheduler.add_pairs([("USDT", "UAH")])

    assert {(task.pair, task.side) for task in run(scheduler, 2)} == {
        (("USDT", "UAH"), "BUY"),
        (("USDT", "UAH"), "SELL"),
    }


def test_empty_queue_returns_none(clock: FakeClock) -> None:
    assert make_scheduler(clock).next_task() is None


def test_rest_goes_round_robin(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    scheduler.add_pairs([("USDT", "UAH"), ("BTC", "UAH"), ("ETH", "EUR")])

    tasks = run(scheduler, 12)

    assert Counter(tasks).most_common()[0][1] == Counter(tasks).most_common()[-1][1] == 2


def test_tasks_in_flight_are_not_handed_out_twice(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    scheduler.add_pairs([("USDT", "UAH")])

    first, second = scheduler.next_task(), scheduler.next_task()

    assert first != second
    assert scheduler.next_task() is None


def test_finish_returns_task_to_queue(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    scheduler.add_pairs([("USDT", "UAH")])
    first, second = scheduler.next_task(), scheduler.next_task()
    assert first is not None and second is not None

    scheduler.finish(first, 5)

    assert scheduler.next_task() == first


def test_min_refresh_limits_repeated_task(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock, min_refresh_sec=1.0)
    scheduler.set_high([("USDT", "UAH")])
    run(scheduler, 2)

    assert scheduler.next_task() is None

    clock.now += 1.0
    assert scheduler.next_task() is not None


def test_budget_is_split_by_weights(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    high = [(f"H{i}", "UAH") for i in range(50)]
    popular = [(f"P{i}", "UAH") for i in range(50)]
    rest = [(f"R{i}", "UAH") for i in range(50)]
    scheduler.set_high(high)
    scheduler.add_pairs([*popular, *rest])
    for asset, fiat in popular:
        scheduler.finish(CollectTask("binance", asset, fiat, "BUY"), POPULAR_MIN_OFFERS)

    shares = Counter(scheduler.priority(task.pair) for task in run(scheduler, 100))

    assert shares == {Priority.HIGH: 60, Priority.POPULAR: 30, Priority.REST: 10}


def test_unused_budget_goes_to_lower_priorities(clock: FakeClock) -> None:
    """Запитів вистачає й на решту: високий пріоритет не забирає більше, ніж може використати."""
    scheduler = make_scheduler(clock, min_refresh_sec=1.0)
    scheduler.set_high([("USDT", "UAH")])
    scheduler.add_pairs([(f"R{i}", "UAH") for i in range(50)])

    tasks = run(scheduler, 20)

    assert sum(scheduler.priority(task.pair) == Priority.HIGH for task in tasks) == 2
    assert sum(scheduler.priority(task.pair) == Priority.REST for task in tasks) == 18


def test_idle_priority_does_not_take_whole_budget_on_return(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    scheduler.add_pairs([(f"R{i}", "UAH") for i in range(50)])
    run(scheduler, 100)

    scheduler.set_high([(f"H{i}", "UAH") for i in range(50)])
    shares = Counter(scheduler.priority(task.pair) for task in run(scheduler, 70))

    assert shares[Priority.HIGH] == pytest.approx(60, abs=2)
    assert shares[Priority.REST] == pytest.approx(10, abs=2)


def test_pair_becomes_popular_with_full_page(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    scheduler.add_pairs([("USDT", "EUR")])
    assert scheduler.priority(("USDT", "EUR")) == Priority.REST

    scheduler.finish(CollectTask("binance", "USDT", "EUR", "SELL"), POPULAR_MIN_OFFERS)

    assert scheduler.priority(("USDT", "EUR")) == Priority.POPULAR


def test_failed_collection_keeps_known_popularity(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)
    task = CollectTask("binance", "USDT", "EUR", "BUY")
    scheduler.add_pairs([task.pair])
    scheduler.finish(task, POPULAR_MIN_OFFERS)

    scheduler.finish(task, None)

    assert scheduler.priority(task.pair) == Priority.POPULAR


def test_set_high_adds_unknown_pair_and_replaces_previous(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock)

    scheduler.set_high([("BTC", "PLN")])
    assert scheduler.priority(("BTC", "PLN")) == Priority.HIGH

    scheduler.set_high([("USDT", "UAH")])
    assert scheduler.priority(("BTC", "PLN")) == Priority.REST
    assert scheduler.counts() == {"high": 2, "popular": 0, "rest": 2}


def test_add_pairs_keeps_refresh_history(clock: FakeClock) -> None:
    scheduler = make_scheduler(clock, min_refresh_sec=1.0)
    scheduler.add_pairs([("USDT", "UAH")])
    run(scheduler, 2)

    scheduler.add_pairs([("USDT", "UAH")])

    assert scheduler.next_task() is None
