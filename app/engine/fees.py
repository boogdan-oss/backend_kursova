from decimal import Decimal

from app.domain import Deal, FeeSchedule
from app.engine.pricing import quantity_for
from app.engine.spreads import net_spread_pct, roi_pct, round_money, spread_pct


def trade_fee_rate(fees: FeeSchedule) -> Decimal:
    """Частка від суми угоди: комісія біржі + P2P + додаткові."""
    return (fees.trade_fee_pct + fees.p2p_fee_pct + fees.extra_fee_pct) / 100


def net_profit(sell_amount: Decimal, buy_amount: Decimal, fees: Decimal) -> Decimal:
    """(1.2) Net Profit = Sell Amount − Buy Amount − Fees."""
    return sell_amount - buy_amount - fees


def cross_exchange_profit_pct(
    buy_price: Decimal,
    sell_price: Decimal,
    quantity: Decimal,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
    transfer_fee: Decimal,
) -> Decimal:
    """profit% = (P_sell·(1−f_sell)·(Q−f_transfer) / (P_buy·(1+f_buy)·Q) − 1) · 100."""
    proceeds = sell_price * (1 - sell_fee_rate) * (quantity - transfer_fee)
    cost = buy_price * (1 + buy_fee_rate) * quantity
    return (proceeds / cost - 1) * 100


def calculate_deal(
    investment: Decimal,
    buy_price: Decimal,
    sell_price: Decimal,
    buy_fees: FeeSchedule,
    sell_fees: FeeSchedule,
    transfer_fee: Decimal = Decimal(0),
    extra_fees: Decimal = Decimal(0),
) -> Deal:
    """Угода на суму інвестиції. transfer_fee — в активі, extra_fees — у фіаті.

    Net Spread рахується від повної вартості купівлі з комісією: так (1.3) збігається
    з формулою чистого прибутку міжбіржової угоди.
    """
    quantity = quantity_for(investment, buy_price)
    transferred = min(transfer_fee, quantity)
    sell_quantity = quantity - transferred

    buy_amount = quantity * buy_price
    sell_amount = quantity * sell_price
    buy_fee = buy_amount * trade_fee_rate(buy_fees)
    sell_fee = sell_quantity * sell_price * trade_fee_rate(sell_fees)
    fees = buy_fee + sell_fee + transferred * sell_price + extra_fees

    profit = net_profit(sell_amount, buy_amount, fees)
    return Deal(
        investment=investment,
        buy_price=buy_price,
        sell_price=sell_price,
        quantity=round_money(quantity),
        sell_quantity=round_money(sell_quantity),
        buy_amount=round_money(buy_amount),
        sell_amount=round_money(sell_amount),
        fees=round_money(fees),
        net_profit=round_money(profit),
        spread_pct=spread_pct(buy_price, sell_price),
        net_spread_pct=net_spread_pct(profit, buy_amount + buy_fee),
        roi_pct=roi_pct(profit, investment),
    )
