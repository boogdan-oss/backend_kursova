from decimal import Decimal

from app.domain import Offer


def filter_by_amount(offers: list[Offer], amount: Decimal) -> list[Offer]:
    """Лише оголошення, у ліміти яких (у фіаті) вкладається сума угоди."""
    return [offer for offer in offers if offer.min_amount <= amount <= offer.max_amount]
