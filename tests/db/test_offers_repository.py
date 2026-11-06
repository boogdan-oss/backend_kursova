from app.db.repositories.offers import merchant_rows
from tests.factories import make_offer


def test_merchant_rows_keep_one_row_per_merchant() -> None:
    offers = [
        make_offer(merchant_id="a", orders=10),
        make_offer(merchant_id="a", orders=20),
        make_offer(merchant_id="b", orders=5),
    ]

    rows = merchant_rows(1, offers)

    assert sorted(row["external_id"] for row in rows) == ["a", "b"]
    assert next(row for row in rows if row["external_id"] == "a")["orders_30d"] == 20


def test_merchant_rows_are_sorted_to_avoid_deadlocks() -> None:
    offers = [make_offer(merchant_id=merchant_id) for merchant_id in ("c", "a", "b")]

    assert [row["external_id"] for row in merchant_rows(1, offers)] == ["a", "b", "c"]
