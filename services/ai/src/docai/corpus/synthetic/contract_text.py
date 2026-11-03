"""Contract wording shared by the PDF and DOCX renderers.

Only the generated facts vary; the other clauses are fixed text, so every fact a
golden question asks about appears exactly once.
"""

from dataclasses import dataclass

from docai.corpus.synthetic.text import amount, long_day
from docai.corpus.synthetic.world import Contract


@dataclass(frozen=True)
class Section:
    heading: str
    paragraphs: tuple[str, ...]
    fee_table_after: int | None = None  # index of the paragraph the fee table follows


@dataclass(frozen=True)
class ContractText:
    title: str
    preamble: tuple[str, ...]
    sections: tuple[Section, ...]
    fee_header: tuple[str, str, str]
    fee_rows: tuple[tuple[str, str, str], ...]
    signatures: tuple[str, str]


def contract_text(c: Contract) -> ContractText:
    return polish(c) if c.language == "pl" else english(c)


def polish(c: Contract) -> ContractText:
    b, v = c.buyer, c.vendor
    services = ", ".join(s.description.lower() for s in c.services)
    renewal = (
        "Po upływie tego okresu Umowa ulega automatycznemu przedłużeniu na kolejne "
        "12 miesięcy, o ile żadna ze Stron nie złoży oświadczenia o jej nieprzedłużeniu "
        f"najpóźniej na {c.notice_days} dni przed upływem okresu obowiązywania."
        if c.auto_renewal
        else "Umowa nie ulega automatycznemu przedłużeniu."
    )
    return ContractText(
        title=f"UMOWA O ŚWIADCZENIE USŁUG nr {c.number}",
        preamble=(
            f"zawarta w dniu {long_day(c.signed, 'pl')} pomiędzy:",
            f"{b.name}, {b.street}, {b.city}, NIP {b.tax_id}, zwaną dalej „Zamawiającym”,",
            "a",
            f"{v.name}, {v.street}, {v.city}, NIP {v.tax_id}, zwaną dalej „Wykonawcą”.",
        ),
        sections=(
            Section(
                "§ 1. Przedmiot umowy",
                (
                    f"Wykonawca zobowiązuje się do świadczenia na rzecz Zamawiającego następujących usług: {services}.",
                    "Zakres usług w poszczególnych okresach rozliczeniowych Zamawiający określa w zamówieniach przekazywanych pocztą elektroniczną.",
                ),
            ),
            Section(
                "§ 2. Okres obowiązywania",
                (
                    f"Umowa zostaje zawarta na czas określony od dnia {long_day(c.start, 'pl')} do dnia {long_day(c.end, 'pl')} włącznie.",
                    renewal,
                    f"Każda ze Stron może rozwiązać Umowę z zachowaniem {c.notice_days}-dniowego okresu wypowiedzenia, ze skutkiem na koniec miesiąca kalendarzowego.",
                ),
            ),
            Section(
                "§ 3. Wynagrodzenie",
                (
                    "Za wykonanie usług Zamawiający zapłaci Wykonawcy wynagrodzenie według stawek netto określonych w poniższej tabeli, powiększonych o podatek VAT według obowiązującej stawki.",
                    f"Łączne wynagrodzenie netto należne Wykonawcy z tytułu Umowy nie może przekroczyć kwoty {amount(c.cap, c.currency, 'pl')} (limit wartości Umowy). Usługi ponad ten limit wymagają aneksu do Umowy.",
                ),
                fee_table_after=0,
            ),
            Section(
                "§ 4. Warunki płatności",
                (
                    "Wykonawca wystawia faktury raz na kwartał, po zakończeniu okresu rozliczeniowego.",
                    f"Wynagrodzenie płatne jest przelewem na rachunek bankowy Wykonawcy wskazany na fakturze, w terminie {c.payment_days} dni od daty wystawienia faktury.",
                ),
            ),
            Section(
                "§ 5. Poufność",
                (
                    "Strony zobowiązują się do zachowania w tajemnicy wszelkich informacji uzyskanych w związku z wykonywaniem Umowy, również po jej rozwiązaniu.",
                ),
            ),
            Section(
                "§ 6. Odpowiedzialność",
                (
                    "Odpowiedzialność Wykonawcy z tytułu niewykonania lub nienależytego wykonania Umowy jest ograniczona do wysokości wynagrodzenia netto zapłaconego w okresie 12 miesięcy poprzedzających zdarzenie.",
                ),
            ),
            Section(
                "§ 7. Postanowienia końcowe",
                (
                    "W sprawach nieuregulowanych Umową zastosowanie mają przepisy prawa polskiego, w szczególności Kodeksu cywilnego.",
                    "Wszelkie zmiany Umowy wymagają formy pisemnej pod rygorem nieważności.",
                    "Umowę sporządzono w dwóch jednobrzmiących egzemplarzach, po jednym dla każdej ze Stron.",
                ),
            ),
        ),
        fee_header=("Usługa", "Jednostka", "Stawka netto"),
        fee_rows=tuple(
            (s.description, s.unit, amount(s.unit_price, c.currency, "pl"))
            for s in c.services
        ),
        signatures=("Zamawiający", "Wykonawca"),
    )


def english(c: Contract) -> ContractText:
    b, v = c.buyer, c.vendor
    services = " and ".join(s.description.lower() for s in c.services)
    renewal = (
        "It will renew automatically for successive periods of 12 months unless either "
        f"party gives written notice of non-renewal at least {c.notice_days} days before "
        "the end of the then-current term."
        if c.auto_renewal
        else "It will not renew automatically."
    )
    return ContractText(
        title=f"SERVICE AGREEMENT No. {c.number}",
        preamble=(
            f"This Service Agreement (the “Agreement”) is made on {long_day(c.signed, 'en')} between:",
            f"(1) {b.name}, {b.street}, {b.city}, Poland, NIP {b.tax_id} (the “Customer”); and",
            f"(2) {v.name}, {v.street}, {v.city}, {v.country}, VAT No. {v.tax_id} (the “Supplier”).",
        ),
        sections=(
            Section(
                "1. Services",
                (
                    f"The Supplier shall provide the following services to the Customer: {services}.",
                    "The Customer will place orders for the services by email.",
                ),
            ),
            Section(
                "2. Term",
                (
                    f"This Agreement commences on {long_day(c.start, 'en')} and continues until {long_day(c.end, 'en')}.",
                    renewal,
                    f"Either party may terminate this Agreement by giving {c.notice_days} days' written notice.",
                ),
            ),
            Section(
                "3. Fees",
                (
                    "The Customer shall pay the Supplier the fees set out in the table below. Fees are exclusive of VAT; the reverse charge mechanism applies.",
                    f"The total fees payable under this Agreement shall not exceed {amount(c.cap, c.currency, 'en')} (the “Contract Cap”). Services beyond the Contract Cap require a written amendment.",
                ),
                fee_table_after=0,
            ),
            Section(
                "4. Payment",
                (
                    "The Supplier shall invoice quarterly in arrears.",
                    f"Invoices are payable by bank transfer within {c.payment_days} days of the invoice date.",
                ),
            ),
            Section(
                "5. Confidentiality",
                (
                    "Each party shall keep confidential all information received from the other party in connection with this Agreement, during its term and after its termination.",
                ),
            ),
            Section(
                "6. Liability",
                (
                    "The Supplier's total liability under this Agreement is limited to the fees paid in the twelve months preceding the event giving rise to the claim.",
                ),
            ),
            Section(
                "7. Governing law",
                (
                    f"This Agreement and any dispute arising out of it are governed by {c.governing_law}.",
                    "No amendment to this Agreement is effective unless made in writing and signed by both parties.",
                ),
            ),
        ),
        fee_header=("Service", "Unit", "Rate (excl. VAT)"),
        fee_rows=tuple(
            (s.description, s.unit, amount(s.unit_price, c.currency, "en"))
            for s in c.services
        ),
        signatures=("For the Customer", "For the Supplier"),
    )
