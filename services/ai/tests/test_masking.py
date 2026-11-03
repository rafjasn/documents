"""Masking of personal identifiers in traces (Stage 10a): every type is masked, and the
amounts, dates and document numbers traces are read for survive."""

import pytest
from pydantic import BaseModel

from docai.core.masking import MARKER, Masker
from docai.corpus.synthetic.text import amount, day, number
from docai.corpus.synthetic.world import WORLD_COUNT, build_world

masker = Masker(b"test-key")


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("NIP: 262-771-21-24", "NIP", "262-771-21-24"),
        ("NIP 2627712124", "NIP", "2627712124"),
        ("NIP: 262 771 21 24", "NIP", "262 771 21 24"),
        ("NIP 123-456-78-90, a typo in the checksum", "NIP", "123-456-78-90"),
        ("Sprzedawca 885-772-17-27 Polska", "NIP", "885-772-17-27"),
        ("VAT No: GB343884595", "VAT", "GB343884595"),
        ("VAT IE880926259", "VAT", "IE880926259"),
        ("NIP UE: PL2627712124", "VAT", "PL2627712124"),
        ("Konto: PL00 1665 5996 1140 2723 6734 1861", "ACCOUNT", "PL00 1665 5996 1140 2723 6734 1861"),
        ("Nr rachunku 00 1665 5996 1140 2723 6734 1861", "ACCOUNT", "00 1665 5996 1140 2723 6734 1861"),
        ("IBAN: GB49 ALYJ 8697 9294 7639 7521", "ACCOUNT", "GB49 ALYJ 8697 9294 7639 7521"),
        ("PESEL 44051401359", "PESEL", "44051401359"),
        ("Contact: jan.kowalski@example.pl", "EMAIL", "jan.kowalski@example.pl"),
        ("Call +48 601 234 567 today", "PHONE", "+48 601 234 567"),
        ("Phone: +44 20 7946 0958", "PHONE", "+44 20 7946 0958"),
        ("tel. 22 123 45 67", "PHONE", "22 123 45 67"),
    ],
)  # fmt: skip
def test_each_type_is_replaced_by_its_kind_and_a_keyed_hash(
    text: str, kind: str, value: str
) -> None:
    masked = masker.text(text)

    assert value not in masked
    assert f"[{kind}:" in masked
    assert MARKER.search(masked)


@pytest.mark.parametrize(
    "text",
    [
        "Razem do zapłaty: 22 212,00 PLN",
        "Total £2,460.00; net 3331.40; 1 234 567,89 zł; 1,234,567.89 EUR",
        "Stawka VAT 23%, kwota VAT 2 027,45",
        "Termin płatności 20.09.2026; issued 2026-09-20; 20 September 2026",
        "Faktura FV/2026/07/123, invoice INV-260082, agreement SA-2025-62, umowa nr 91/2025",
        "Quantity 12, unit price 623.70, 30 days, 400 tokens, chunk 1932",
        "Clark Services Ltd, Herba Usługi Sp. z o.o., London EC1A 1BB",
        # Ten digits that pass the NIP checksum, but with decimals: an amount.
        "Kwota 2627712124,00 PLN; total 2627712124.50",
    ],
)  # fmt: skip
def test_amounts_dates_and_document_numbers_survive(text: str) -> None:
    assert masker.text(text) == text


@pytest.mark.parametrize("index", range(WORLD_COUNT))
def test_every_synthetic_identifier_is_masked_and_every_total_survives(
    index: int,
) -> None:
    world = build_world(index)
    for party in (world.buyer, *world.vendors):
        label = "NIP" if party.language == "pl" else "VAT No"
        document = f"{party.name}\n{label}: {party.tax_id}\nKonto: {party.bank_account}"
        masked = masker.text(document)
        assert party.tax_id not in masked, party.tax_id
        assert party.bank_account not in masked, party.bank_account
        assert party.name in masked
    for invoice in world.invoices:
        language = invoice.language
        readable = [
            invoice.number,
            amount(invoice.gross, invoice.currency, language),
            number(invoice.net, language),
            day(invoice.issued, language),
            day(invoice.due, language),
        ]
        text = " | ".join(readable)
        assert masker.text(text) == text


def test_the_same_value_hashes_alike_whatever_its_formatting() -> None:
    dashed, plain = masker.text("262-771-21-24"), masker.text("NIP 2627712124")
    assert dashed in plain
    assert Masker(b"another-key").text("262-771-21-24") != dashed


def test_masking_twice_changes_nothing() -> None:
    once = masker.text("NIP 262-771-21-24, konto PL00 1665 5996 1140 2723 6734 1861")
    assert masker.text(once) == once


class Payload(BaseModel):
    text: str


def test_nested_data_is_masked_and_keys_and_scalars_kept() -> None:
    data = {
        "messages": [{"role": "user", "content": "NIP 262-771-21-24"}],
        "metadata": ("jan@example.pl", 3, None, True),
        "model": Payload(text="+48 601 234 567"),
        "NIP 262-771-21-24": 1.5,
    }
    masked = masker.mask(data)

    assert "262-771-21-24" not in masked["messages"][0]["content"]
    assert masked["metadata"][1:] == [3, None, True]
    assert "[EMAIL:" in masked["metadata"][0]
    assert "[PHONE:" in masked["model"]["text"]
    assert masked["NIP 262-771-21-24"] == 1.5
