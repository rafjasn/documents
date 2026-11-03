from datetime import date
from decimal import Decimal

import pytest

from docai.evals.golden import AnswerKind, GoldenItem, Relevant
from docai.evals.scoring import dates_in, normalise_text, numbers_in, score, yes_no


def item(kind: AnswerKind, value: str, accepted: tuple[str, ...] = ()) -> GoldenItem:
    return GoldenItem(
        id="t-1",
        split="dev",
        type="single_fact",
        language="en",
        question="?",
        answer=value,
        kind=kind,
        value=value,
        accepted=accepted,
        relevant=(Relevant(doc_id="d"),),
        source="test",
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Do zapłaty: 5 166,00 PLN", Decimal("5166.00")),
        ("The total is £1,665.40.", Decimal("1665.40")),
        ("€364.00", Decimal("364.00")),
        ("RM 75.00", Decimal("75.00")),
        ("1.234,56 EUR", Decimal("1234.56")),
        ("kwota 36,00 PLN netto", Decimal("36.00")),
        ("30 dni", Decimal("30")),
        ("1 915,00 PLN", Decimal("1915.00")),
    ],
)
def test_numbers_are_read_in_polish_and_english_formats(
    text: str, expected: Decimal
) -> None:
    assert expected in numbers_in(text)


def test_dates_are_not_read_as_numbers() -> None:
    assert Decimal("6") not in numbers_in("It was paid on 6 April 2026.")
    assert numbers_in("Paid on 06.04.2026, amount 6 units") >= {Decimal("6")}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Termin płatności: 17.04.2026", date(2026, 4, 17)),
        ("due on 17 April 2026", date(2026, 4, 17)),
        ("April 17, 2026", date(2026, 4, 17)),
        ("17 kwietnia 2026 r.", date(2026, 4, 17)),
        ("31 grudnia 2026", date(2026, 12, 31)),
        ("2026-04-17", date(2026, 4, 17)),
        ("signed 8/8/18", date(2018, 8, 8)),
        ("the 20th of October 2017", date(2017, 10, 20)),
        ("30 czerwca 2026 r.", date(2026, 6, 30)),
        ("22 października 2026", date(2026, 10, 22)),
    ],
)
def test_dates_in_polish_and_english(text: str, expected: date) -> None:
    assert expected in dates_in(text)[0]


def test_ambiguous_numeric_dates_accept_both_readings() -> None:
    assert dates_in("6/4/2026")[0] == {date(2026, 4, 6), date(2026, 6, 4)}


def test_yes_no_reads_the_first_word() -> None:
    assert yes_no("Yes, the cap was exceeded in Q3.") == "yes"
    assert yes_no("Nie, termin jest krótszy.") == "no"
    assert yes_no("The invoices exceeded the cap.") is None


def test_normalise_text_drops_accents_and_punctuation() -> None:
    assert normalise_text("Leżoń Serwis, S.A.") == "lezon serwis s a"


def test_number_answers() -> None:
    assert score(item("number", "1665.40"), "The amount due is £1,665.40.") is True
    assert score(item("number", "1665.40"), "The amount due is £1,247.40.") is False
    assert score(item("number", "1665.40"), "I could not find that invoice.") is False


def test_date_answers_go_to_the_judge_when_no_date_is_found() -> None:
    assert (
        score(item("date", "2026-06-30"), "Umowa obowiązuje do 30 czerwca 2026 r.")
        is True
    )
    assert score(item("date", "2026-06-30"), "It ends on 31 December 2026.") is False
    assert (
        score(item("date", "2026-06-30"), "It ends at the end of June next year.")
        is None
    )


def test_boolean_and_refusal_answers() -> None:
    assert score(item("boolean", "no"), "No — the invoice uses 7 days.") is True
    assert score(item("boolean", "no"), "Yes.") is False
    assert score(item("boolean", "no"), "The terms are 30 days.") is None
    assert (
        score(
            item("refusal", "refusal"),
            "The agreement does not specify a late-payment interest rate.",
        )
        is True
    )
    assert (
        score(item("refusal", "refusal"), "Faktura nie zawiera numeru telefonu.")
        is True
    )
    assert score(item("refusal", "refusal"), "The rate is 8%.") is None


def test_text_answers_match_accepted_forms_or_go_to_the_judge() -> None:
    law = item(
        "text",
        "British Columbia, Canada",
        ("British Columbia, Canada", "British Columbia"),
    )
    assert (
        score(law, "It is governed by the laws of the Province of British Columbia.")
        is True
    )
    assert score(law, "Ontario law applies.") is None
    nip = item("text", "275-490-93-79", ("275-490-93-79", "2754909379"))
    assert score(nip, "NIP sprzedawcy: 275-490-93-79") is True


@pytest.mark.parametrize(
    "answer",
    [
        "Umowa nr 43/2025 nie przewiduje żadnego rabatu za wcześniejszą płatność.",
        "Please provide the document you are referring to!",
        "You haven't included the text or file in your message.",
        "Nie podałeś treści dokumentu, do którego odnosi się pytanie.",
        "Dokument jest pusty.",
        "The agreement does not provide for any early-payment discount.",
    ],
)
def test_refusals_without_a_value(answer: str) -> None:
    assert score(item("refusal", "refusal"), answer) is True


def test_invented_values_are_not_refusals() -> None:
    assert (
        score(
            item("refusal", "refusal"),
            "The delivery address is al. Wyszyńskiego 77/87, Szczytno.",
        )
        is None
    )


# The rules credit only the value an answer asserts; what is unclear goes to the judge.

RECEIPT = item("number", "98.35")
RECEIPT_RM = GoldenItem(**RECEIPT.model_dump() | {"answer": "RM 98.35"})


@pytest.mark.parametrize(
    "answer",
    [
        "The answer is 999999. It is definitely not 98.35.",  # the review's case
        "It is not 98.35.",
        "Nie 98,35 PLN, tylko więcej.",
        "The total isn't £98.35.",
    ],
)
def test_a_negated_value_is_not_credited(answer: str) -> None:
    assert score(RECEIPT, answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        "The total is 98.35. Actually, it is 99.10.",
        "Either 98.35 or 99.10.",
        "98.35 MYR, not 99.10 MYR.",
        "Total RM 98.35, paid RM 100.00 in cash, change RM 1.65.",
        # A prompt rounding to hundreds, which the old rule passed: the value in the
        # explanation isn't the answer.
        "8000 PLN. Łączna kwota brutto wynosi 98,35 PLN.",
    ],
)
def test_contradictory_or_several_values_go_to_the_judge(answer: str) -> None:
    assert score(RECEIPT_RM, answer) is None


def test_a_wrong_value_is_wrong_even_with_others_around() -> None:
    assert score(RECEIPT_RM, "The answer is 999999.") is False
    assert score(RECEIPT_RM, "RM 98.30, paid with RM 100.00.") is False


@pytest.mark.parametrize(
    "answer",
    [
        "The total amount due on invoice INV-260056 from O'Donnell Consulting is RM 98.35.",
        "Zgodnie z umową nr 43/2025 (§ 4) z 2026 roku kwota wynosi **98,35** [1][6].",
        "98.35 MYR [1][13]. The receipt dated 22/04/2018 was issued in Q3 2018.",
        "98.35 MYR. This is the sum of 3 items and a 6% service tax.",
    ],
)
def test_identifiers_years_dates_and_other_units_are_not_rival_values(
    answer: str,
) -> None:
    assert score(RECEIPT_RM, answer) is True


def test_the_currency_or_unit_must_match_when_given() -> None:
    assert score(RECEIPT_RM, "The total is 98.35.") is True  # left out: fine
    assert score(RECEIPT_RM, "The total is 98.35 EUR.") is False
    notice = GoldenItem(**item("number", "30").model_dump() | {"answer": "30 dni"})
    assert score(notice, "30-dniowy okres wypowiedzenia [7].") is True
    assert score(notice, "30 days' written notice.") is True
    assert score(notice, "30 months' written notice.") is False


def test_a_count_ignores_amounts_and_identifiers_beside_it() -> None:
    count = item("number", "3")
    assert (
        score(count, "3. Wszystkie trzy umowy (nr 91/2025, SA-2025-62 oraz 43/2025) kończą się 31 grudnia 2026 r.")
        is True
    )  # fmt: skip
    assert score(count, "3 invoices, 2,308.00 EUR in total, all from 2026.") is True
    assert score(count, "Q3 had the most invoices.") is None  # only inside "Q3"


def test_dates_negated_or_contradicted_go_to_the_judge() -> None:
    end = item("date", "2026-12-31")
    assert score(end, "Umowa obowiązuje do 31 grudnia 2026 r.") is True
    assert score(end, "It ends on 30 June 2026, not 31 December 2026.") is None
    assert score(end, "It does not end on 31 December 2026.") is None
    assert score(end, "It ends on 31 December 2026, or maybe 30 June 2026.") is None
    assert (
        score(item("date", "2026-04-06"), "Paid 6/4/2026.") is True
    )  # one date, two readings


def test_a_negated_text_answer_goes_to_the_judge() -> None:
    law = item("text", "British Columbia", ("British Columbia",))
    assert score(law, "It is governed by the laws of British Columbia.") is True
    assert score(law, "It is not governed by British Columbia law.") is None


@pytest.mark.parametrize(
    "answer",
    [
        "There is no information missing. The answer is 999999.",  # the review's case
        "There is no doubt: the rate is 8%.",
        "The documents do not state the rate. The rate is 8%.",
        "The documents do not state the rate. However, it is likely 8%.",
        "The documents do not state the rate, but it is 8%.",
        "Dokumenty tego nie podają. Odpowiedź: 8%.",
        "Dokumenty nie zawierają tej informacji, ale stawka wynosi 8%.",
    ],
)
def test_a_refusal_followed_by_an_answer_is_not_certain(answer: str) -> None:
    assert score(item("refusal", "refusal"), answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        "The documents do not state a late-payment interest rate for SA-2025-62. The agreement only specifies that payment is due within 21 days [1].",
        "The documents don't state the total for invoice INV-268143.",
        "The service agreement does **not** specify a late-payment interest rate.",
        "Dokumenty nie zawierają tej informacji. Faktura FV/2026/07/161 podaje jedynie adres i NIP sprzedawcy [6][8].",
        "The documents do not state the total for invoice INV-268231. There is no invoice with that number in the sources.",
        "Please provide the document! Once you paste the content, I will look up the invoice.",
    ],
)
def test_a_refusal_with_context_only_is_certain(answer: str) -> None:
    assert score(item("refusal", "refusal"), answer) is True
