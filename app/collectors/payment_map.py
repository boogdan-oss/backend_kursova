import re

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
    "raiffeisenbank": "raiffeisen",
    "sepainstant": "sepa_instant",
    "monobankcard": "monobank",
    "privatbankcard": "privatbank",
    "otpbanknew": "otpbank",
    "sensesuperapp": "sense",
    "abankiban": "abank_iban",
    "bankiban": "bank_iban",
    "monobankiban": "monobank_iban",
    "oschadbankiban": "oschadbank_iban",
    "otpbankiban": "otpbank_iban",
    "privatbankiban": "privatbank_iban",
    "pumbiban": "pumb_iban",
    "sensesuperappiban": "sense_iban",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_CARD_SUFFIX = re.compile(r"\(\s*card\s*\)", re.IGNORECASE)


def normalize_payment(raw: str) -> str:
    """'Monobank', 'Mono', 'monobank' → 'monobank'.

    Невідомий спосіб оплати не відкидається — повертається його нормалізований ідентифікатор.
    """
    key = _NON_ALNUM.sub("", _CARD_SUFFIX.sub("", raw).lower())
    return ALIASES.get(key, key)
