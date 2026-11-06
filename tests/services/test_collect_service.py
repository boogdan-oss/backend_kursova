from datetime import timedelta
from decimal import Decimal

from app.collectors.scheduler import CollectTask
from app.services.collect_service import MarketStatsTracker
from tests.factories import FETCHED_AT, make_offer

BUY = CollectTask("binance", "USDT", "UAH", "BUY")
SELL = CollectTask("binance", "USDT", "UAH", "SELL")


def test_tracker_waits_for_second_side() -> None:
    tracker = MarketStatsTracker()

    assert tracker.update(BUY, [make_offer("41.20")], FETCHED_AT) is None


def test_tracker_combines_both_sides() -> None:
    tracker = MarketStatsTracker()
    tracker.update(BUY, [make_offer("41.20")], FETCHED_AT)

    snapshot = tracker.update(SELL, [make_offer("41.85", "SELL")], FETCHED_AT)

    assert snapshot is not None
    assert snapshot.buy_price == Decimal("41.20")
    assert snapshot.sell_price == Decimal("41.85")
    assert round(snapshot.spread_pct or 0, 2) == Decimal("1.58")


def test_tracker_uses_latest_offers_of_each_side() -> None:
    tracker = MarketStatsTracker()
    tracker.update(BUY, [make_offer("41.20")], FETCHED_AT)
    tracker.update(SELL, [make_offer("41.85", "SELL")], FETCHED_AT)

    snapshot = tracker.update(BUY, [make_offer("41.50")], FETCHED_AT)

    assert snapshot is not None
    assert snapshot.buy_price == Decimal("41.50")


def test_side_without_offers_counts_as_collected() -> None:
    tracker = MarketStatsTracker()
    tracker.update(BUY, [], FETCHED_AT)

    snapshot = tracker.update(SELL, [make_offer("41.85", "SELL")], FETCHED_AT)

    assert snapshot is not None
    assert snapshot.buy_price is None


def test_pairs_do_not_mix() -> None:
    tracker = MarketStatsTracker()
    tracker.update(BUY, [make_offer("41.20")], FETCHED_AT)

    other_sell = CollectTask("binance", "USDT", "EUR", "SELL")

    assert tracker.update(other_sell, [make_offer("0.95", "SELL")], FETCHED_AT) is None


def test_offers_since_skips_stale_batches() -> None:
    tracker = MarketStatsTracker()
    fresh = make_offer("41.20")
    stale = make_offer("41.85", "SELL").model_copy(
        update={"fetched_at": FETCHED_AT - timedelta(minutes=10)}
    )
    tracker.update(BUY, [fresh], FETCHED_AT)
    tracker.update(SELL, [stale], FETCHED_AT)

    assert tracker.offers_since(FETCHED_AT - timedelta(minutes=5)) == [fresh]
