from decimal import Decimal

from app.domain import Offer

MIN_COMPLETION_RATE = Decimal("0.90")
MIN_ORDERS_30D = 50


def filter_by_amount(offers: list[Offer], amount: Decimal) -> list[Offer]:
    """Лише оголошення, у ліміти яких (у фіаті) вкладається сума угоди."""
    return [offer for offer in offers if offer.min_amount <= amount <= offer.max_amount]


def filter_by_payment(offers: list[Offer], payment: str) -> list[Offer]:
    """Лише оголошення з потрібним способом оплати (нормалізований ключ)."""
    return [offer for offer in offers if payment in offer.payment_methods]


def filter_reliable(offers: list[Offer]) -> list[Offer]:
    """Відкидає ненадійних продавців: completion_rate < 90 % або менше 50 угод."""
    return [
        offer
        for offer in offers
        if offer.merchant.completion_rate >= MIN_COMPLETION_RATE
        and offer.merchant.orders_30d >= MIN_ORDERS_30D
    ]


def filter_liquid(offers: list[Offer], min_available: Decimal) -> list[Offer]:
    """FR-13: лише оголошення, де доступно щонайменше min_available активу."""
    return [offer for offer in offers if offer.available >= min_available]
