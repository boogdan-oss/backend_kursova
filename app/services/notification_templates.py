"""Тексти сповіщень для Telegram (HTML-розмітка бота) і листів (HTML + звичайний текст).

Цифри лише з даних можливості чи знімка ринку; усе, що приходить від користувача
(назва правила), екранується.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape

from app.core.config import get_settings
from app.domain import AlertRule, Opportunity

ACCENT = "#8B6CEF"
DISCLAIMER = "Аналітика, не фінансова порада. Перевірте умови оголошення на біржі перед угодою."
EXCHANGE_NAMES = {"okx": "OKX", "htx": "HTX", "mexc": "MEXC"}
KIND_NAMES = {
    "spread": "спред на одній біржі",
    "cross_exchange": "міжбіржовий арбітраж",
    "chain": "ланцюжок",
}
CHANNEL_NAMES = {"in_app": "на сайті", "telegram": "Telegram", "email": "пошта"}


@dataclass(frozen=True, slots=True)
class EmailContent:
    subject: str
    text: str
    html: str


@dataclass(frozen=True, slots=True)
class PairQuote:
    """Найкращі ціни пари для команди /pairs; exchange — якщо стежать за однією біржею."""

    asset: str
    fiat: str
    exchange: str | None
    buy_price: Decimal | None
    buy_exchange: str | None
    sell_price: Decimal | None
    sell_exchange: str | None
    age_sec: float | None


def exchange_name(code: str) -> str:
    return EXCHANGE_NAMES.get(code, code.capitalize())


def money(value: Decimal | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def price(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return money(value, 2 if abs(value) >= 100 else 4)


def site_url(path: str) -> str | None:
    base = get_settings().frontend_url
    return f"{base.rstrip('/')}{path}" if base else None


def _rule_title(rule: AlertRule) -> str:
    return rule.title or f"Правило #{rule.id}"


def _route_lines(item: Opportunity) -> list[str]:
    if item.kind == "chain":
        steps = " → ".join(
            f"{exchange_name(step.exchange)} {step.action.lower()} {step.asset}"
            for step in item.steps
        )
        return [f"Маршрут: {steps}"]
    return [
        f"Купити: {exchange_name(item.buy_exchange)} за {price(item.buy_price)} {item.fiat}",
        f"Продати: {exchange_name(item.sell_exchange)} за {price(item.sell_price)} {item.fiat}",
    ]


def _metrics(item: Opportunity) -> list[str]:
    lines = [
        f"Спред {money(item.spread_pct)} % · чистий {money(item.net_spread_pct)} % · "
        f"прибуток ≈ {money(item.estimated_profit)} {item.fiat}"
    ]
    extras = []
    if item.ai_score is not None:
        extras.append(f"AI Score {item.ai_score}")
    if item.rr is not None:
        extras.append(f"RR {money(item.rr)}")
    if item.max_volume:
        extras.append(f"обсяг до {money(item.max_volume)} {item.asset}")
    if extras:
        lines.append(" · ".join(extras))
    return lines


def alert_text(rule: AlertRule, item: Opportunity) -> str:
    """Звичайний текст: історія сповіщень на сайті й текстова версія листа."""
    lines = [
        f"🔔 {_rule_title(rule)}",
        f"{item.asset}/{item.fiat} · {KIND_NAMES.get(item.kind, item.kind)}",
        *_route_lines(item),
        *_metrics(item),
        DISCLAIMER,
    ]
    url = site_url("/arbitrage")
    if url:
        lines.append(url)
    return "\n".join(lines)


def telegram_alert(rule: AlertRule, item: Opportunity) -> str:
    lines = [
        f"🔔 <b>{escape(_rule_title(rule))}</b>",
        f"<b>{item.asset}/{item.fiat}</b> · {KIND_NAMES.get(item.kind, item.kind)}",
        *(escape(line) for line in _route_lines(item)),
        *(escape(line) for line in _metrics(item)),
        f"<i>{escape(DISCLAIMER)}</i>",
    ]
    return "\n".join(lines)


def _email_html(heading: str, rows: Sequence[str], button: tuple[str, str] | None) -> str:
    body = "".join(
        f'<p style="margin:0 0 8px;font-size:15px;line-height:1.5;color:#1f1f1f">{row}</p>'
        for row in rows
    )
    action = ""
    if button is not None:
        label, url = button
        action = (
            f'<p style="margin:20px 0 4px"><a href="{escape(url, quote=True)}" '
            f'style="background:{ACCENT};color:#ffffff;text-decoration:none;padding:10px 18px;'
            f'border-radius:6px;font-weight:600;font-size:14px;display:inline-block">'
            f"{escape(label)}</a></p>"
        )
    return (
        '<!doctype html><html lang="uk"><body style="margin:0;padding:24px;'
        'background:#f4f3f8;font-family:Inter,Segoe UI,Arial,sans-serif">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        '<tr><td align="center"><table role="presentation" width="560" cellpadding="0" '
        'cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:10px;'
        f'border-top:4px solid {ACCENT};padding:24px">'
        f'<tr><td><p style="margin:0 0 4px;font-size:12px;letter-spacing:.08em;'
        f'text-transform:uppercase;color:{ACCENT};font-weight:700">CryptoParser</p>'
        f'<h1 style="margin:0 0 16px;font-size:20px;color:#111111">{escape(heading)}</h1>'
        f"{body}{action}"
        '<p style="margin:20px 0 0;font-size:12px;line-height:1.5;color:#777777">'
        f"{escape(DISCLAIMER)}<br>Налаштувати сповіщення можна на сторінці «Сповіщення» "
        "на сайті.</p></td></tr></table></td></tr></table></body></html>"
    )


def email_alert(rule: AlertRule, item: Opportunity) -> EmailContent:
    heading = f"{item.asset}/{item.fiat}: {KIND_NAMES.get(item.kind, item.kind)}"
    rows = [
        f"<b>{escape(_rule_title(rule))}</b>",
        *(escape(line) for line in _route_lines(item)),
        *(escape(line) for line in _metrics(item)),
    ]
    url = site_url("/arbitrage")
    return EmailContent(
        subject=f"CryptoParser: {_rule_title(rule)} — {item.asset}/{item.fiat} "
        f"{money(item.net_spread_pct)} %",
        text=alert_text(rule, item),
        html=_email_html(heading, rows, ("Відкрити арбітраж", url) if url else None),
    )


def email_code(code: str, minutes: int) -> EmailContent:
    text = (
        f"Код підтвердження пошти для сповіщень CryptoParser: {code}\n"
        f"Код діє {minutes} хвилин. Якщо ви його не запитували, просто ігноруйте цей лист."
    )
    rows = [
        "Код підтвердження пошти для сповіщень:",
        f'<span style="font-size:28px;font-weight:700;letter-spacing:.2em">{code}</span>',
        f"Код діє {minutes} хвилин. Якщо ви його не запитували, просто ігноруйте цей лист.",
    ]
    return EmailContent(
        subject=f"CryptoParser: код підтвердження {code}",
        text=text,
        html=_email_html("Підтвердження пошти", rows, None),
    )


def email_test(username: str) -> EmailContent:
    text = (
        f"Вітаємо, {username}! Це тестовий лист CryptoParser: сповіщення поштою працюють.\n"
        "Сюди приходитимуть спрацювання ваших правил з каналом «пошта»."
    )
    rows = [
        f"Вітаємо, {escape(username)}!",
        "Сповіщення поштою працюють. Сюди приходитимуть спрацювання ваших правил "
        "з каналом «пошта».",
    ]
    url = site_url("/alerts")
    return EmailContent(
        subject="CryptoParser: тестове сповіщення",
        text=text,
        html=_email_html("Тестове сповіщення", rows, ("Мої правила", url) if url else None),
    )


def telegram_test(username: str) -> str:
    return (
        f"✅ <b>Тестове сповіщення</b>\nАкаунт {escape(username)}: Telegram підключено, "
        "сюди приходитимуть спрацювання правил з каналом Telegram.\n/pairs — ціни ваших пар"
    )


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "даних ще немає"
    if seconds < 90:
        return f"оновлено {int(seconds)} с тому"
    if seconds < 5400:
        return f"оновлено {int(seconds // 60)} хв тому"
    return f"оновлено {int(seconds // 3600)} год тому"


def pairs_message(quotes: Sequence[PairQuote], hidden: int = 0) -> str:
    if not quotes:
        return (
            "У вас ще немає відстежуваних пар. Додайте пару в обране чи створіть правило "
            "на сторінці «Сповіщення» на сайті."
        )
    blocks = ["📊 <b>Ваші пари</b>"]
    for quote in quotes:
        scope = f" · {exchange_name(quote.exchange)}" if quote.exchange else ""
        lines = [f"\n<b>{quote.asset}/{quote.fiat}</b>{escape(scope)}"]
        if quote.buy_price is None and quote.sell_price is None:
            lines.append("Немає надійних оголошень")
        else:
            buy_at = f" — {exchange_name(quote.buy_exchange)}" if quote.buy_exchange else ""
            sell_at = f" — {exchange_name(quote.sell_exchange)}" if quote.sell_exchange else ""
            lines.append(f"Купити: {price(quote.buy_price)} {quote.fiat}{escape(buy_at)}")
            lines.append(f"Продати: {price(quote.sell_price)} {quote.fiat}{escape(sell_at)}")
            if quote.buy_price and quote.sell_price:
                spread = (quote.sell_price - quote.buy_price) / quote.buy_price * 100
                lines.append(f"Спред {money(spread)} %")
        lines.append(f"<i>{_age(quote.age_sec)}</i>")
        blocks.append("\n".join(lines))
    if hidden:
        blocks.append(f"\n…і ще {hidden}: усі пари — на сайті.")
    return "\n".join(blocks)


def _thresholds(rule: AlertRule) -> str:
    parts = []
    if rule.min_spread is not None:
        parts.append(f"спред ≥ {money(rule.min_spread)} %")
    if rule.min_net_spread is not None:
        parts.append(f"чистий ≥ {money(rule.min_net_spread)} %")
    if rule.min_ai_score is not None:
        parts.append(f"AI ≥ {rule.min_ai_score}")
    if rule.min_rr is not None:
        parts.append(f"RR ≥ {money(rule.min_rr)}")
    if rule.min_net_profit is not None:
        parts.append(f"прибуток ≥ {money(rule.min_net_profit)} {rule.fiat}")
    return ", ".join(parts)


def rules_message(rules: Sequence[tuple[AlertRule, datetime | None]]) -> str:
    if not rules:
        return "Правил сповіщень ще немає. Створіть їх на сторінці «Сповіщення» на сайті."
    lines = ["🔔 <b>Ваші правила</b>"]
    for rule, last in rules:
        state = "✅" if rule.enabled else "⏸"
        scope = f" · {exchange_name(rule.exchange)}" if rule.exchange else ""
        channels = ", ".join(CHANNEL_NAMES.get(channel, channel) for channel in rule.channels)
        fired = f" · востаннє {last:%d.%m %H:%M} UTC" if last else ""
        lines.append(
            f"\n{state} <b>{escape(_rule_title(rule))}</b>\n"
            f"{rule.asset}/{rule.fiat}{escape(scope)}: {escape(_thresholds(rule))}\n"
            f"Канали: {escape(channels)}{fired}"
        )
    return "\n".join(lines)


HELP = (
    "Це бот сповіщень CryptoParser.\n"
    "/pairs — поточні ціни ваших відстежуваних пар\n"
    "/rules — ваші правила сповіщень\n"
    "/stop — вимкнути сповіщення в Telegram\n"
    "Пари й правила налаштовуються на сторінці «Сповіщення» на сайті."
)
