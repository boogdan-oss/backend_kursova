import re

# Різні написання одного способу оплати → один ключ. Ключі — після базової нормалізації.
ALIASES: dict[str, str] = {
    "mono": "monobank",
    "monobankua": "monobank",
    "privat": "privatbank",
    "privat24": "privatbank",
    "privatbankua": "privatbank",
    "abankua": "abank",
    "pumbbank": "pumb",
    "raiffeisenaval": "raiffeisen",
    "raiffeisenbankaval": "raiffeisen",
    "sepainstant": "sepa_instant",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_payment(raw: str) -> str:
    """'Monobank', 'Mono', 'monobank' → 'monobank'.

    Невідомий спосіб оплати не відкидається — повертається його нормалізований ідентифікатор.
    """
    key = _NON_ALNUM.sub("", raw.lower())
    return ALIASES.get(key, key)
