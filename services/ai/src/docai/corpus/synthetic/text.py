"""Labels and number/date formatting for Polish and English documents."""

from datetime import date
from decimal import Decimal

from docai.corpus.synthetic.world import Language

PL_MONTHS = (
    "stycznia",
    "lutego",
    "marca",
    "kwietnia",
    "maja",
    "czerwca",
    "lipca",
    "sierpnia",
    "września",
    "października",
    "listopada",
    "grudnia",
)
CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€"}


def number(value: Decimal, language: Language) -> str:
    """5 166,00 in Polish documents, 5,166.00 in English ones."""
    text = f"{value:,.2f}"
    if language == "pl":
        return text.replace(",", " ").replace(".", ",")
    return text


def amount(value: Decimal, currency: str, language: Language) -> str:
    if language == "pl" or currency not in CURRENCY_SYMBOLS:
        return f"{number(value, language)} {currency}"
    return f"{CURRENCY_SYMBOLS[currency]}{number(value, language)}"


def quantity(value: Decimal | int, language: Language) -> str:
    if isinstance(value, int) or value == value.to_integral_value():
        return str(int(value))
    return number(Decimal(value), language)


def day(value: date, language: Language) -> str:
    """30.09.2026 in Polish documents, 30 September 2026 in English ones."""
    if language == "pl":
        return value.strftime("%d.%m.%Y")
    return f"{value.day} {value.strftime('%B %Y')}"


def long_day(value: date, language: Language) -> str:
    """30 września 2026 r. in Polish contracts, 30 September 2026 in English ones."""
    if language == "pl":
        return f"{value.day} {PL_MONTHS[value.month - 1]} {value.year} r."
    return day(value, language)


INVOICE_LABELS: dict[Language, dict[str, str]] = {
    "pl": {
        "title": "Faktura VAT nr {number}",
        "issued": "Data wystawienia",
        "sale": "Data sprzedaży",
        "due": "Termin płatności",
        "method": "Sposób płatności",
        "method_value": "przelew",
        "seller": "Sprzedawca",
        "buyer": "Nabywca",
        "tax_id": "NIP",
        "columns": "Lp.|Nazwa towaru lub usługi|Ilość|J.m.|Cena netto|Wartość netto|VAT %|Kwota VAT|Wartość brutto",
        "total_net": "Razem netto",
        "total_vat": "Razem VAT",
        "total_gross": "Razem brutto",
        "to_pay": "Do zapłaty",
        "account": "Numer rachunku",
        "contract": "Zgodnie z umową nr {number}",
        "reverse_charge": "Odwrotne obciążenie",
    },
    "en": {
        "title": "INVOICE {number}",
        "issued": "Invoice date",
        "sale": "Supply date",
        "due": "Due date",
        "method": "Payment method",
        "method_value": "Bank transfer",
        "seller": "Supplier",
        "buyer": "Bill to",
        "tax_id": "VAT No.",
        "columns": "#|Description|Qty|Unit|Unit price|Net|VAT %|VAT|Gross",
        "total_net": "Total net",
        "total_vat": "Total VAT",
        "total_gross": "Total",
        "to_pay": "Amount due",
        "account": "Bank account (IBAN)",
        "contract": "As per Service Agreement {number}",
        "reverse_charge": "Reverse charge: VAT to be accounted for by the recipient "
        "(Article 196 of Council Directive 2006/112/EC).",
    },
}

STAMP_TEXT: dict[Language, str] = {"pl": "ZAPŁACONO", "en": "PAID"}
