from decimal import Decimal
from typing import Any

import pytest

from docai.agent.metadata import (
    AggregatePlan,
    MetadataQuery,
    Record,
    describe_result,
    evaluate,
    names_match,
    run_plan,
    run_query,
)


def invoice(
    number: str,
    vendor: str,
    issued: str,
    net: str,
    vat: str,
    currency: str = "PLN",
    buyer: str = "Klęczar Handel Sp. z o.o.",
    **extra: Any,
) -> Record:
    gross = str(Decimal(net) + Decimal(vat))
    fields = {
        "kind": "invoice",
        "vendor": {"name": vendor},
        "buyer": {"name": buyer},
        "invoiceNumber": number,
        "issueDate": issued,
        "dueDate": extra.pop("due", None),
        "currency": currency,
        "netTotal": Decimal(net),
        "taxTotal": Decimal(vat),
        "grossTotal": Decimal(gross),
        **extra,
    }
    return Record(f"doc-{number}", f"{number}.pdf", "invoice", fields)


def contract(number: str, party: str, start: str, end: str, cap: str) -> Record:
    fields = {
        "kind": "contract",
        "parties": [{"name": party}, {"name": "KLĘCZAR HANDEL SP. Z O.O."}],
        "contractNumber": number,
        "effectiveDate": start,
        "endDate": end,
        "valueCap": {"amount": Decimal(cap), "currency": "PLN"},
        "paymentTermsDays": 14,
    }
    return Record(f"doc-{number}", f"{number}.pdf", "contract", fields)


RECORDS = [
    invoice("FV/1", "Herba Usługi Sp. z o.o.", "2026-02-10", "1000.00", "230.00", due="2026-02-24"),
    invoice("FV/2", "Herba Usługi Sp. z o.o.", "2026-05-03", "500.00", "115.00"),
    invoice("FV/3", "HERBA USLUGI", "2026-05-20", "250.50", "57.62"),
    invoice("INV-9", "O'Donnell Consulting Limited", "2026-03-01", "800.00", "0.00", "EUR", reverseCharge=True),
    invoice("INV-10", "O'Donnell Consulting Limited", "2025-12-30", "100.00", "0.00", "EUR", reverseCharge=True),
    contract("12/2025", "Herba Usługi Sp. z o.o.", "2025-11-01", "2026-12-31", "5000.00"),
    contract("13/2025", "Leżoń Serwis Sp. z o.o.", "2025-06-01", "2026-06-30", "20300.00"),
    Record("doc-r1", "paragon.jpg", "receipt", {"kind": "receipt", "merchant": {"name": "Bistro Bartkowicz"}, "date": "2026-04-12", "currency": "PLN", "total": Decimal("96.50")}),
]  # fmt: skip


def query(**changes: Any) -> MetadataQuery:
    return MetadataQuery.model_validate(
        {"name": "q", "category": "invoice", "operation": "sum"} | changes
    )


def test_names_match_without_case_accents_or_legal_forms() -> None:
    assert names_match("Herba Usługi Sp. z o.o.", "HERBA USLUGI")
    assert names_match("Klęczar Handel", "KLĘCZAR HANDEL SP. Z O.O.")
    assert names_match("O'Donnell Consulting", "O'Donnell Consulting Limited")
    assert not names_match("Herba Trans", "Herba Usługi Sp. z o.o.")
    assert not names_match("Sp. z o.o.", "Herba Usługi Sp. z o.o.")  # nothing to match


def test_quarter_sums_are_decimal_and_exact() -> None:
    q2 = run_query(
        RECORDS,
        query(
            field="net_total",
            issuer="Herba Usługi",
            date_from="2026-04-01",
            date_to="2026-06-30",
        ),
    )
    assert [r.number for r in q2.rows] == ["FV/2", "FV/3"]
    assert (q2.value, q2.currency) == (Decimal("750.50"), "PLN")


def test_counts_filter_on_reverse_charge_and_recipient() -> None:
    reverse = run_query(
        RECORDS,
        query(
            operation="count",
            recipient="Klęczar Handel Sp. z o.o.",
            reverse_charge=True,
        ),
    )
    assert reverse.value == 2
    in_2026 = run_query(
        RECORDS,
        query(
            operation="count",
            issuer="O'Donnell Consulting Limited",
            date_from="2026-01-01",
            date_to="2026-12-31",
        ),
    )
    assert in_2026.value == 1


def test_the_largest_invoice_names_its_document() -> None:
    largest = run_query(RECORDS, query(operation="max", issuer="Herba"))
    assert largest.best is not None and largest.best.number == "FV/1"
    assert "FV/1" in describe_result(largest)


def test_mixed_currencies_have_no_single_total() -> None:
    total = run_query(RECORDS, query(recipient="Klęczar Handel"))
    assert total.value is None
    assert total.by_currency == {"PLN": Decimal("2153.12"), "EUR": Decimal("900.00")}
    assert "several currencies" in describe_result(total)


def test_contracts_by_end_date_and_in_force() -> None:
    year_end = run_query(
        RECORDS,
        query(
            category="contract",
            operation="count",
            party="Klęczar",
            date_field="end_date",
            date_from="2026-12-31",
            date_to="2026-12-31",
        ),
    )
    assert year_end.value == 1
    in_force = run_query(
        RECORDS, query(category="contract", operation="list", in_force_on="2026-08-01")
    )
    assert [r.number for r in in_force.rows] == ["12/2025"]


def test_an_expression_combines_results_in_code() -> None:
    plan = AggregatePlan(
        queries=[
            query(
                name="cap",
                category="contract",
                operation="sum",
                field="value_cap",
                party="Herba",
            ),
            query(
                name="used", field="net_total", issuer="Herba", numbers=["FV/1", "FV 2"]
            ),
        ],
        expression="cap - used",
    )
    result = run_plan(RECORDS, plan)
    assert result.expression_value == Decimal("3500.00")
    assert [r.doc_id for r in result.records] == ["doc-12/2025", "doc-FV/1", "doc-FV/2"]


@pytest.mark.parametrize(
    "expression",
    ["__import__('os').system('true')", "cap.real", "cap ** 2", "unknown - 1", "cap -"],
)
def test_expressions_allow_only_arithmetic_on_known_results(expression: str) -> None:
    with pytest.raises((ValueError, SyntaxError)):
        evaluate(expression, {"cap": Decimal("10")})


def test_a_failed_expression_is_reported_not_raised() -> None:
    plan = AggregatePlan(queries=[query(name="a")], expression="a - b")
    result = run_plan(RECORDS, plan)
    assert result.expression_value is None and result.error and "b" in result.error


def test_days_to_due_are_computed_for_the_answer() -> None:
    [first] = [r for r in RECORDS if r.number == "FV/1"]
    assert first.days_to_due() == 14


def test_up_to_a_document_includes_it_and_everything_dated_before() -> None:
    used = run_query(
        RECORDS, query(field="net_total", issuer="Herba", up_to_number="FV 2")
    )
    assert [r.number for r in used.rows] == ["FV/1", "FV/2"]
    assert used.value == Decimal("1500.00")
    missing = run_query(RECORDS, query(issuer="Herba", up_to_number="FV/99"))
    assert missing.rows == [] and "FV/99" in missing.note


def test_a_conversations_scope_limits_the_records_as_it_limits_the_search() -> None:
    from dataclasses import replace
    from datetime import date

    from docai.agent.records import in_scope
    from docai.core.contracts import ChatScope

    records = [
        replace(r, language="en" if r.doc_id.startswith("doc-INV") else "pl")
        for r in RECORDS
    ]

    def ids(**scope: Any) -> list[str]:
        return [r.doc_id for r in in_scope(records, ChatScope(**scope))]

    assert ids() == [r.doc_id for r in RECORDS]
    assert ids(languages=["en"]) == ["doc-INV-9", "doc-INV-10"]
    # Invoices by issue date, receipts by date, contracts by effective date.
    assert ids(date_from=date(2026, 3, 1), date_to=date(2026, 4, 30)) == [
        "doc-INV-9",
        "doc-r1",
    ]
    assert ids(categories=["CONTRACT"], date_to=date(2025, 12, 31)) == [
        "doc-12/2025",
        "doc-13/2025",
    ]
    assert ids(document_ids=["doc-FV/2", "doc-r1"], categories=["INVOICE"]) == [
        "doc-FV/2"
    ]


def test_names_match_across_polish_case_endings_but_not_other_names() -> None:
    assert names_match("Sąsiadki Dystrybucja", "Sąsiadek Dystrybucja Sp. z o.o.")
    assert names_match("Herby Usług", "Herba Usługi Sp. z o.o.")
    assert names_match("Klęczara Handlu", "KLĘCZAR HANDEL SP. Z O.O.")  # genitive
    assert not names_match("Nowak", "Nowakowski Serwis")
    assert not names_match("Kowalczyk Media", "Kowalski Media")
    assert not names_match("Herba", "Herbalife Polska")
