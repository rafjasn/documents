"""Seeded synthetic worlds: a Polish buyer with two Polish and two foreign vendors.

Each world holds 4 contracts, 12 invoices and 4 receipts. Every fact comes from a
seed, so documents and golden answers are reproducible. Four anomalies are planted
per world for cross-document questions:

- cap_exceeded: a vendor's invoiced net total passes its contract cap in Q3
- wrong_payment_terms: one invoice's due date ignores the contract's payment days
- after_contract_end: one invoice is issued after its contract ended
- price_mismatch: one invoice line uses a different unit price than the contract
"""

import random
import string
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal

from faker import Faker

WORLD_COUNT = 5
YEAR = 2026
CENT = Decimal("0.01")

Language = Literal["en", "pl"]
Anomaly = Literal[
    "cap_exceeded", "wrong_payment_terms", "after_contract_end", "price_mismatch"
]
ANOMALIES: tuple[Anomaly, ...] = (
    "cap_exceeded",
    "wrong_payment_terms",
    "after_contract_end",
    "price_mismatch",
)


@dataclass(frozen=True)
class CatalogService:
    en: str
    pl: str
    unit_en: str
    unit_pl: str
    pln_price: tuple[int, int]
    quantity: tuple[int, int]


CATALOG = (
    CatalogService(
        "Road transport Warsaw-Berlin",
        "Transport drogowy Warszawa-Berlin",
        "trip",
        "kurs",
        (2800, 4200),
        (1, 6),
    ),
    CatalogService(
        "Pallet storage",
        "Składowanie palet",
        "pallet-month",
        "paleta/mies.",
        (25, 45),
        (20, 120),
    ),
    CatalogService("IT support", "Wsparcie IT", "hour", "godz.", (120, 220), (5, 60)),
    CatalogService(
        "Software licence",
        "Licencja oprogramowania",
        "user-month",
        "użytkownik/mies.",
        (60, 140),
        (10, 50),
    ),
    CatalogService(
        "Office cleaning", "Sprzątanie biura", "month", "mies.", (1800, 3500), (1, 1)
    ),
    CatalogService(
        "Legal consulting", "Doradztwo prawne", "hour", "godz.", (280, 450), (3, 25)
    ),
    CatalogService(
        "Translation services",
        "Usługi tłumaczeniowe",
        "page",
        "strona",
        (45, 90),
        (10, 80),
    ),
    CatalogService(
        "Equipment rental", "Wynajem sprzętu", "day", "dzień", (150, 400), (2, 15)
    ),
    CatalogService(
        "Security monitoring",
        "Monitoring ochrony",
        "month",
        "mies.",
        (900, 1600),
        (1, 1),
    ),
    CatalogService(
        "Cloud hosting", "Hosting w chmurze", "month", "mies.", (1500, 4000), (1, 1)
    ),
)


@dataclass(frozen=True)
class ForeignCountry:
    country: str
    currency: str
    pln_rate: Decimal
    vat_prefix: str
    governing_law: str
    cities: tuple[str, ...]


FOREIGN = (
    ForeignCountry(
        "United Kingdom",
        "GBP",
        Decimal("5.0"),
        "GB",
        "the laws of England and Wales",
        ("London", "Manchester", "Leeds"),
    ),
    ForeignCountry(
        "Ireland",
        "EUR",
        Decimal("4.3"),
        "IE",
        "the laws of Ireland",
        ("Dublin", "Cork", "Galway"),
    ),
)


@dataclass(frozen=True)
class Party:
    name: str
    street: str
    city: str
    country: str
    tax_id: str
    language: Language
    currency: str
    bank_account: str


@dataclass(frozen=True)
class Service:
    description: str
    unit: str
    unit_price: Decimal


@dataclass(frozen=True)
class Contract:
    doc_id: str
    number: str
    vendor: Party
    buyer: Party
    language: Language
    signed: date
    start: date
    end: date
    auto_renewal: bool
    notice_days: int
    payment_days: int
    cap: Decimal
    currency: str
    governing_law: str
    services: tuple[Service, ...]
    format: Literal["pdf", "docx"]
    fee_table_as_image: bool


@dataclass(frozen=True)
class InvoiceLine:
    description: str
    unit: str
    quantity: int
    unit_price: Decimal
    vat_rate: Decimal

    @property
    def net(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(CENT)

    @property
    def vat(self) -> Decimal:
        return (self.net * self.vat_rate / 100).quantize(CENT, ROUND_HALF_UP)

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat


@dataclass(frozen=True)
class Invoice:
    doc_id: str
    number: str
    vendor: Party
    buyer: Party
    contract_id: str
    language: Language
    issued: date
    due: date
    currency: str
    lines: tuple[InvoiceLine, ...]
    reverse_charge: bool
    paid_on: date | None
    format: Literal["pdf", "scanned_pdf"]

    @property
    def net(self) -> Decimal:
        return sum((line.net for line in self.lines), Decimal("0.00"))

    @property
    def vat(self) -> Decimal:
        return sum((line.vat for line in self.lines), Decimal("0.00"))

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat

    @property
    def quarter(self) -> int:
        return (self.issued.month - 1) // 3 + 1


@dataclass(frozen=True)
class ReceiptItem:
    description: str
    quantity: Decimal
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Receipt:
    doc_id: str
    number: str
    merchant: Party
    language: Language
    issued: date
    time: str
    currency: str
    items: tuple[ReceiptItem, ...]
    vat_rate: Decimal
    payment: Literal["card", "cash"]

    @property
    def total(self) -> Decimal:
        return sum((item.amount for item in self.items), Decimal("0.00"))

    @property
    def vat(self) -> Decimal:
        """VAT included in the gross total."""
        return (self.total * self.vat_rate / (100 + self.vat_rate)).quantize(
            CENT, ROUND_HALF_UP
        )


@dataclass(frozen=True)
class World:
    index: int
    buyer: Party
    vendors: tuple[Party, ...]
    contracts: tuple[Contract, ...]
    invoices: tuple[Invoice, ...]
    receipts: tuple[Receipt, ...]
    anomalies: tuple[tuple[Anomaly, str], ...]

    @property
    def group(self) -> str:
        return f"syn-w{self.index}"

    def contract_for(self, vendor: Party) -> Contract:
        return next(c for c in self.contracts if c.vendor == vendor)

    def invoices_for(self, vendor: Party) -> list[Invoice]:
        return [i for i in self.invoices if i.vendor == vendor]

    def anomaly_contract(self, anomaly: Anomaly) -> Contract:
        contract_id = dict(self.anomalies)[anomaly]
        return next(c for c in self.contracts if c.doc_id == contract_id)


def money(value: float | int | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, ROUND_HALF_UP)


def round_to(value: Decimal, step: int, rounding: str) -> Decimal:
    return money((value / step).to_integral_value(rounding) * step)


def polish_nip(rng: random.Random) -> str:
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    while True:
        digits = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(8)]
        check = sum(d * w for d, w in zip(digits, weights)) % 11
        if check < 10:
            raw = "".join(map(str, digits + [check]))
            return f"{raw[:3]}-{raw[3:6]}-{raw[6:8]}-{raw[8:]}"


def account_number(rng: random.Random, prefix: str) -> str:
    if prefix == "PL":
        digits = "".join(str(rng.randint(0, 9)) for _ in range(26))
        return "PL" + " ".join(
            [digits[:2]] + [digits[i : i + 4] for i in range(2, 26, 4)]
        )
    bank = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
    groups = " ".join(f"{rng.randint(1000, 9999)}" for _ in range(4))
    return f"{prefix}{rng.randint(10, 99)} {bank} {groups}"


def polish_party(fake: Faker, rng: random.Random, name: str) -> Party:
    return Party(
        name=name,
        street=fake.street_address(),
        city=f"{fake.postcode()} {fake.city()}",
        country="Polska",
        tax_id=polish_nip(rng),
        language="pl",
        currency="PLN",
        bank_account=account_number(rng, "PL"),
    )


def foreign_party(
    fake: Faker, rng: random.Random, country: ForeignCountry, name: str
) -> Party:
    city = rng.choice(country.cities)
    return Party(
        name=name,
        street=f"{rng.randint(1, 240)} {fake.street_name()}",
        city=f"{city} {fake.postcode()}" if country.vat_prefix == "GB" else city,
        country=country.country,
        tax_id=f"{country.vat_prefix}{rng.randint(100000000, 999999999)}",
        language="en",
        currency=country.currency,
        bank_account=account_number(rng, country.vat_prefix),
    )


def build_world(index: int) -> World:
    """Generate world `index` (0-based). The same index always gives the same world."""
    rng = random.Random(f"documents-world-{index}")
    fake_pl, fake_en = Faker("pl_PL"), Faker("en_GB")
    fake_pl.seed_instance(1000 + index)
    fake_en.seed_instance(2000 + index)

    buyer_kind = rng.choice(["Handel", "Produkcja", "Dystrybucja"])
    buyer = polish_party(fake_pl, rng, f"{fake_pl.last_name()} {buyer_kind} Sp. z o.o.")
    vendors = (
        polish_party(fake_pl, rng, f"{fake_pl.last_name()} Usługi Sp. z o.o."),
        polish_party(fake_pl, rng, f"{fake_pl.last_name()} Serwis Sp. z o.o."),
        foreign_party(fake_en, rng, FOREIGN[0], f"{fake_en.last_name()} Services Ltd"),
        foreign_party(
            fake_en, rng, FOREIGN[1], f"{fake_en.last_name()} Consulting Limited"
        ),
    )
    vendor_order = list(range(4))
    rng.shuffle(vendor_order)
    assignment: dict[Anomaly, int] = dict(zip(ANOMALIES, vendor_order))
    catalog_order = list(range(len(CATALOG)))
    rng.shuffle(catalog_order)
    used_numbers: set[str] = set()

    contracts: list[Contract] = []
    invoices: list[Invoice] = []
    for v, vendor in enumerate(vendors):
        foreign = None if vendor.language == "pl" else FOREIGN[v - 2]
        services = []
        for catalog_index in catalog_order[2 * v : 2 * v + 2]:
            item = CATALOG[catalog_index]
            price = Decimal(rng.randint(*item.pln_price))
            if foreign is not None:
                price = (price / foreign.pln_rate).to_integral_value()
            services.append(
                (
                    item,
                    Service(
                        description=item.en if foreign else item.pl,
                        unit=item.unit_en if foreign else item.unit_pl,
                        unit_price=money(price),
                    ),
                )
            )
        payment_days = rng.choice([14, 21, 30, 45, 60])
        contract_id = f"syn-w{index}-ctr-{v}"

        vendor_invoices: list[Invoice] = []
        for q, month in enumerate((1, 4, 7)):
            issued = date(YEAR, month, rng.randint(3, 25))
            lines = []
            for s, (item, service) in enumerate(services):
                if s == 1 and rng.random() < 0.4:
                    continue
                unit_price = service.unit_price
                if assignment["price_mismatch"] == v and q == 1 and s == 0:
                    unit_price = money(unit_price * Decimal("1.1"))
                lines.append(
                    InvoiceLine(
                        description=service.description,
                        unit=service.unit,
                        quantity=rng.randint(*item.quantity),
                        unit_price=unit_price,
                        vat_rate=Decimal("0") if foreign else Decimal("23"),
                    )
                )
            days = (
                7 if assignment["wrong_payment_terms"] == v and q == 1 else payment_days
            )
            number = ""
            while not number or number in used_numbers:
                seq = rng.randint(1, 400)
                number = (
                    f"INV-{YEAR % 100}{seq:04d}"
                    if foreign
                    else f"FV/{YEAR}/{month:02d}/{seq:03d}"
                )
            used_numbers.add(number)
            vendor_invoices.append(
                Invoice(
                    doc_id=f"syn-w{index}-inv-{v}-q{q + 1}",
                    number=number,
                    vendor=vendor,
                    buyer=buyer,
                    contract_id=contract_id,
                    language=vendor.language,
                    issued=issued,
                    due=issued + timedelta(days=days),
                    currency=vendor.currency,
                    lines=tuple(lines),
                    reverse_charge=foreign is not None,
                    paid_on=None,
                    format="pdf",
                )
            )

        first_two = vendor_invoices[0].net + vendor_invoices[1].net
        total = first_two + vendor_invoices[2].net
        step = 1000 if total > 20000 else 100
        if assignment["cap_exceeded"] == v:
            cap = round_to((first_two + total) / 2, step, ROUND_FLOOR)
            if not first_two < cap < total:
                cap = money((first_two + total) / 2)
        else:
            cap = round_to(
                total * Decimal(str(round(rng.uniform(1.3, 1.8), 2))),
                step,
                ROUND_CEILING,
            )

        contracts.append(
            Contract(
                doc_id=contract_id,
                number=f"{rng.randint(10, 99)}/{YEAR - 1}"
                if not foreign
                else f"SA-{YEAR - 1}-{rng.randint(10, 99)}",
                vendor=vendor,
                buyer=buyer,
                language=vendor.language,
                signed=date(YEAR - 1, rng.randint(10, 12), rng.randint(1, 28)),
                start=date(YEAR, 1, 1),
                end=date(YEAR, 6, 30)
                if assignment["after_contract_end"] == v
                else date(YEAR, 12, 31),
                auto_renewal=rng.random() < 0.5,
                notice_days=rng.choice([30, 60, 90]),
                payment_days=payment_days,
                cap=cap,
                currency=vendor.currency,
                governing_law=foreign.governing_law if foreign else "prawo polskie",
                services=tuple(service for _, service in services),
                format="pdf" if v in (0, 2) else "docx",
                fee_table_as_image=v == 3,
            )
        )
        invoices.extend(vendor_invoices)

    invoices = assign_formats_and_payments(rng, invoices)
    receipts = build_receipts(index, rng, fake_pl, fake_en)
    return World(
        index=index,
        buyer=buyer,
        vendors=vendors,
        contracts=tuple(contracts),
        invoices=tuple(invoices),
        receipts=receipts,
        anomalies=tuple(
            (name, f"syn-w{index}-ctr-{v}") for name, v in assignment.items()
        ),
    )


def assign_formats_and_payments(
    rng: random.Random, invoices: list[Invoice]
) -> list[Invoice]:
    """Three invoices become scans; four Q1/Q2 invoices get a PAID stamp."""
    scanned = set(rng.sample(range(len(invoices)), 3))
    early = [i for i, invoice in enumerate(invoices) if invoice.quarter < 3]
    paid = set(rng.sample(early, 4))
    return [
        replace(
            invoice,
            format="scanned_pdf" if i in scanned else "pdf",
            paid_on=invoice.due - timedelta(days=rng.randint(0, 5))
            if i in paid
            else None,
        )
        for i, invoice in enumerate(invoices)
    ]


@dataclass(frozen=True)
class ReceiptProduct:
    description: str
    price: tuple[float, float]
    quantity: tuple[int, int]
    fuel: bool = False


PETROL = ReceiptProduct("Benzyna Pb95", (6.2, 7.1), (20, 60), fuel=True)
UNLEADED = ReceiptProduct("Unleaded petrol", (1.35, 1.65), (20, 60), fuel=True)
LATTE = ReceiptProduct("Kawa latte", (12, 19), (1, 4))
WATER = ReceiptProduct("Woda mineralna 1,5 l", (2.5, 4.9), (1, 12))
FLAT_WHITE = ReceiptProduct("Flat white", (3.1, 4.2), (1, 4))

# Merchant kind -> what it sells. Fuel, when sold, is always the first line.
MERCHANTS: dict[Language, tuple[tuple[str, tuple[ReceiptProduct, ...]], ...]] = {
    "pl": (
        ("Stacja Paliw", (PETROL, LATTE, WATER)),
        ("Bistro", (ReceiptProduct("Obiad dnia", (29, 49), (1, 4)), LATTE, WATER)),
        (
            "Papiernik",
            (
                ReceiptProduct("Papier A4 500 ark.", (18, 29), (1, 10)),
                ReceiptProduct("Toner do drukarki", (189, 349), (1, 2)),
            ),
        ),
    ),
    "en": (
        ("Service Station", (UNLEADED, FLAT_WHITE)),
        (
            "Cafe",
            (FLAT_WHITE, ReceiptProduct("Sandwich meal deal", (4.0, 6.5), (1, 4))),
        ),
        ("Cabs", (ReceiptProduct("Taxi fare", (18, 45), (1, 1)),)),
        (
            "Stationers",
            (
                ReceiptProduct("A4 paper ream", (4.5, 7.5), (1, 10)),
                ReceiptProduct("Notebook", (2.5, 6.0), (1, 6)),
            ),
        ),
    ),
}


def build_receipts(
    index: int, rng: random.Random, fake_pl: Faker, fake_en: Faker
) -> tuple[Receipt, ...]:
    receipts = []
    languages: tuple[Language, ...] = ("pl", "pl", "en", "en")
    for k, language in enumerate(languages):
        kind, products = rng.choice(MERCHANTS[language])
        if language == "pl":
            merchant = polish_party(fake_pl, rng, f"{kind} {fake_pl.last_name()}")
        else:
            merchant = foreign_party(
                fake_en, rng, FOREIGN[0], f"{fake_en.last_name()} {kind}"
            )
        count = rng.randint(1, len(products))
        chosen = (
            products[:1] + tuple(rng.sample(products[1:], count - 1))
            if products[0].fuel
            else tuple(rng.sample(products, count))
        )
        items = []
        for product in chosen:
            if product.fuel:
                litres = money(rng.uniform(*product.quantity))
                quantity, unit_price = litres, money(rng.uniform(*product.price))
            else:
                quantity = Decimal(rng.randint(*product.quantity))
                unit_price = money(rng.uniform(*product.price))
            items.append(ReceiptItem(product.description, quantity, unit_price))
        receipts.append(
            Receipt(
                doc_id=f"syn-w{index}-rec-{k}",
                number=f"{rng.randint(1000, 9999)}/{rng.randint(10, 99)}",
                merchant=merchant,
                language=language,
                issued=date(YEAR, rng.randint(1, 9), rng.randint(1, 28)),
                time=f"{rng.randint(7, 21):02d}:{rng.randint(0, 59):02d}",
                currency="PLN" if language == "pl" else "GBP",
                items=tuple(items),
                vat_rate=Decimal("23") if language == "pl" else Decimal("20"),
                payment=rng.choice(["card", "cash"]),
            )
        )
    return tuple(receipts)


def build_worlds() -> list[World]:
    return [build_world(index) for index in range(WORLD_COUNT)]
