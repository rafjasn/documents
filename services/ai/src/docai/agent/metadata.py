"""The metadata tool: questions answered from extracted fields, computed in code.

The model plans one or more MetadataQuery objects (filters and an operation) and,
optionally, an arithmetic expression over their results; this module filters the
user's documents and does every sum, count, average, comparison and subtraction with
Decimal. The user's documents are loaded by the caller for the user in the token:
nothing the model writes can name a user.
"""

import ast
import os
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

Category = Literal["invoice", "receipt", "contract"]
Operation = Literal["sum", "count", "average", "max", "min", "list"]
AmountField = Literal[
    "net_total",
    "tax_total",
    "gross_total",
    "total",
    "value_cap",
    "notice_period_days",
    "payment_terms_days",
]
DateField = Literal[
    "issue_date", "due_date", "paid_date", "date", "effective_date", "end_date"
]
DEFAULT_DATE_FIELD: dict[str, str] = {
    "invoice": "issue_date",
    "receipt": "date",
    "contract": "effective_date",
}
DEFAULT_AMOUNT: dict[str, str] = {
    "invoice": "gross_total",
    "receipt": "total",
    "contract": "value_cap",
}
MAX_ROWS = 30
# Legal forms and filler words that don't identify a company.
NAME_NOISE = {
    "sp", "z", "o", "oo", "s", "a", "sa", "ltd", "limited", "inc", "llc", "plc",
    "gmbh", "co", "corp", "corporation", "company", "the", "and", "k", "j", "spolka",
    "sdn", "bhd", "m",
}  # fmt: skip


class MetadataQuery(BaseModel):
    """One computation over the user's documents (filters, then an operation)."""

    name: str = Field(
        description="Short identifier for the result, used in `expression`, e.g. q1_total"
    )
    category: Category
    operation: Operation = Field(
        description="sum, count, average, max or min of `field`, or list the documents"
    )
    field: AmountField | None = Field(
        None,
        description="Amount to sum, average or rank by. Invoices: net_total, tax_total "
        "(VAT), gross_total. Receipts: total, tax_total. Contracts: value_cap, "
        "notice_period_days, payment_terms_days. Default: the gross total, receipt "
        "total or value cap",
    )
    issuer: str | None = Field(
        None, description="Vendor that issued the invoice, or merchant on a receipt"
    )
    recipient: str | None = Field(None, description="Buyer an invoice is addressed to")
    party: str | None = Field(
        None, description="Any party: a contract party, or an invoice's vendor or buyer"
    )
    numbers: list[str] = Field(
        [], description="Exact document numbers, e.g. invoice or contract numbers"
    )
    date_field: DateField | None = Field(
        None,
        description="Date the range applies to. Default: issue_date for invoices, date "
        "for receipts, effective_date for contracts",
    )
    date_from: str | None = Field(None, description="Inclusive start, YYYY-MM-DD")
    date_to: str | None = Field(None, description="Inclusive end, YYYY-MM-DD")
    currency: str | None = Field(None, description="ISO currency code, e.g. PLN, EUR")
    reverse_charge: bool | None = Field(
        None, description="Only invoices issued under the reverse charge (or only not)"
    )
    in_force_on: str | None = Field(
        None,
        description="Contracts in force on this date (YYYY-MM-DD): effective on or "
        "before it and not ended before it",
    )
    up_to_number: str | None = Field(
        None,
        description="Only documents dated on or before the document with this number, "
        "inclusive: e.g. the invoices up to and including invoice INV-1",
    )


class AggregatePlan(BaseModel):
    queries: list[MetadataQuery] = Field(min_length=1, max_length=4)
    expression: str | None = Field(
        None,
        description="Optional arithmetic over the query names with + - * / and "
        "parentheses, e.g. 'cap - used'. Leave empty if one query answers the question",
    )


@dataclass(frozen=True)
class Record:
    """A processed document of the user, as the metadata tool sees it."""

    doc_id: str
    title: str
    category: str  # invoice, receipt, contract or other
    fields: dict[str, Any]
    language: str | None = None

    @property
    def number(self) -> str | None:
        f = self.fields
        value = (
            f.get("invoiceNumber") or f.get("receiptNumber") or f.get("contractNumber")
        )
        return str(value) if value else None

    def issuer(self) -> str:
        party = self.fields.get("vendor") or self.fields.get("merchant") or {}
        return str(party.get("name") or "")

    def recipient(self) -> str:
        return str((self.fields.get("buyer") or {}).get("name") or "")

    def parties(self) -> list[str]:
        names = [str(p.get("name") or "") for p in self.fields.get("parties") or []]
        return [n for n in (*names, self.issuer(), self.recipient()) if n]

    def currency(self) -> str | None:
        f = self.fields
        currency = f.get("currency") or (f.get("valueCap") or {}).get("currency")
        return str(currency).upper() if currency else None

    def amount(self, name: str) -> Decimal | None:
        f = self.fields
        if name == "value_cap":
            value = (f.get("valueCap") or {}).get("amount")
        else:
            value = f.get(camel(name))
        return to_decimal(value)

    def date(self, name: str) -> date | None:
        return to_date(self.fields.get(camel(name)))

    def days_to_due(self) -> int | None:
        issued, due = self.date("issue_date"), self.date("due_date")
        return (due - issued).days if issued and due else None


@dataclass
class QueryResult:
    query: MetadataQuery
    rows: list[Record]
    value: Decimal | None = None  # None when there is no single value
    currency: str | None = None
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    best: Record | None = None  # the document holding a max or min
    note: str = ""


@dataclass
class AggregateResult:
    results: list[QueryResult]
    expression: str | None = None
    expression_value: Decimal | None = None
    error: str | None = None

    @property
    def records(self) -> list[Record]:
        """Every document the results rest on, once, in order."""
        seen: dict[str, Record] = {}
        for result in self.results:
            for row in result.rows:
                seen.setdefault(row.doc_id, row)
        return list(seen.values())


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def to_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def name_tokens(text: str) -> set[str]:
    decomposed = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return {t for t in re.split(r"[^\w]+", plain) if t and t not in NAME_NOISE}


def same_word(a: str, b: str) -> bool:
    """The same word, allowing a Polish case ending: 'sasiadki' and 'sasiadek', 'herby'
    and 'herba'. The words share all but the last two letters of the shorter (at least
    four) and differ in length by at most three, so 'nowak' doesn't match 'nowakowski'."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if len(short) < 4 or len(long) - len(short) > 3 or short.isdigit():
        return False
    common = len(os.path.commonprefix([short, long]))
    return common >= max(4, len(short) - 2)


def names_match(wanted: str, actual: str) -> bool:
    """Every identifying word of `wanted` is in `actual`, allowing case endings:
    'Klęczar Handel' matches 'KLĘCZAR HANDEL SP. Z O.O.', 'Herba' matches 'Herba Usługi
    Sp. z o.o.', and 'Sąsiadki Dystrybucja' matches 'Sąsiadek Dystrybucja'."""
    tokens, words = name_tokens(wanted), name_tokens(actual)
    return bool(tokens) and all(any(same_word(t, w) for w in words) for t in tokens)


def normalise_number(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def matches(record: Record, query: MetadataQuery) -> bool:
    if record.category != query.category:
        return False
    if query.issuer and not names_match(query.issuer, record.issuer()):
        return False
    if query.recipient and not names_match(query.recipient, record.recipient()):
        return False
    if query.party and not any(names_match(query.party, p) for p in record.parties()):
        return False
    if query.numbers:
        wanted = {normalise_number(n) for n in query.numbers}
        if normalise_number(record.number or "") not in wanted:
            return False
    if query.currency and record.currency() != query.currency.upper():
        return False
    if query.reverse_charge is not None and (
        bool(record.fields.get("reverseCharge")) != query.reverse_charge
    ):
        return False
    if query.date_from or query.date_to:
        when = record.date(query.date_field or DEFAULT_DATE_FIELD[query.category])
        start, end = to_date(query.date_from), to_date(query.date_to)
        if when is None or (start and when < start) or (end and when > end):
            return False
    if query.in_force_on:
        day = to_date(query.in_force_on)
        start, end = record.date("effective_date"), record.date("end_date")
        if day is None or (start and start > day) or (end and end < day):
            return False
    return True


def date_of(record: Record) -> date | None:
    return record.date(DEFAULT_DATE_FIELD.get(record.category, "issue_date"))


def run_query(records: Sequence[Record], query: MetadataQuery) -> QueryResult:
    rows = [r for r in records if matches(r, query)]
    if query.up_to_number:
        wanted = normalise_number(query.up_to_number)
        anchors = [r for r in records if normalise_number(r.number or "") == wanted]
        until = date_of(anchors[0]) if anchors else None
        if until is None:
            result = QueryResult(query, [])
            result.note = f"no dated document numbered {query.up_to_number}"
            return result
        rows = [r for r in rows if (d := date_of(r)) is not None and d <= until]
    result = QueryResult(query, rows)
    if query.operation == "count":
        result.value = Decimal(len(rows))
        return result
    if query.operation == "list":
        return result
    amount = query.field or DEFAULT_AMOUNT[query.category]
    valued = [(r, v) for r in rows if (v := r.amount(amount)) is not None]
    if len(valued) < len(rows):
        result.note = f"{len(rows) - len(valued)} matching documents have no {amount}"
    if not valued:
        return result
    if query.operation in ("max", "min"):
        pick = max if query.operation == "max" else min
        best, value = pick(valued, key=lambda pair: pair[1])
        result.best, result.value, result.currency = best, value, best.currency()
        return result
    days = amount.endswith("_days")
    groups: dict[str, list[Decimal]] = defaultdict(list)
    for record, value in valued:
        groups["" if days else (record.currency() or "?")].append(value)
    for currency, values in groups.items():
        total = sum(values, Decimal(0))
        if query.operation == "average":
            total = (total / len(values)).quantize(Decimal("0.01"))
        result.by_currency[currency] = total
    if len(groups) == 1:
        [(currency, value)] = result.by_currency.items()
        result.value, result.currency = value, currency or None
    else:
        result.note = "amounts are in several currencies, so there is no single total"
    return result


OPERATORS: dict[type, Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def evaluate(expression: str, values: dict[str, Decimal]) -> Decimal:
    """+ - * / over named results and numbers; anything else is rejected."""

    def walk(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError(f"{node.id} has no single numeric value")
            return values[node.id]
        raise ValueError(f"unsupported expression: {ast.dump(node)[:80]}")

    return walk(ast.parse(expression, mode="eval")).quantize(Decimal("0.01"))


def run_plan(records: Sequence[Record], plan: AggregatePlan) -> AggregateResult:
    results = [run_query(records, query) for query in plan.queries]
    outcome = AggregateResult(results, plan.expression)
    if plan.expression:
        values = {r.query.name: r.value for r in results if r.value is not None}
        try:
            outcome.expression_value = evaluate(plan.expression, values)
        except (ValueError, SyntaxError, ArithmeticError) as error:
            outcome.error = f"{plan.expression}: {error}"
    return outcome


# What the answer step reads ------------------------------------------------------------


def money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f}{f' {currency}' if currency else ''}"


def describe_row(record: Record) -> str:
    f = record.fields
    parts = [record.category.title(), record.number or record.title]
    if record.category == "invoice":
        parts += [
            f"issuer {record.issuer()}",
            f"recipient {record.recipient() or 'unknown'}",
            f"issued {f.get('issueDate') or 'unknown'}",
            f"due {f.get('dueDate') or 'unknown'}"
            + (f" ({d} days after issue)" if (d := record.days_to_due()) else ""),
            f"currency {record.currency() or 'unknown'}",
            f"net {money(record.amount('net_total'))}",
            f"VAT {money(record.amount('tax_total'))}",
            f"gross {money(record.amount('gross_total'))}",
        ]
        if f.get("reverseCharge"):
            parts.append("reverse charge")
        if f.get("paidDate"):
            parts.append(f"paid {f['paidDate']}")
        lines = [
            f"{line.get('description')}: {line.get('quantity')} x "
            f"{money(to_decimal(line.get('unitPrice')))} net"
            for line in (f.get("lineItems") or [])[:5]
        ]
        if lines:
            parts.append("lines: " + "; ".join(lines))
    elif record.category == "receipt":
        parts += [
            f"merchant {record.issuer()}",
            f"date {f.get('date') or 'unknown'}",
            f"total {money(record.amount('total'), record.currency())}",
        ]
    elif record.category == "contract":
        cap = f.get("valueCap") or {}
        parts += [
            "parties " + ", ".join(record.parties()),
            f"effective {f.get('effectiveDate') or 'unknown'}",
            f"ends {f.get('endDate') or 'not stated'}",
            f"value cap {money(to_decimal(cap.get('amount')), cap.get('currency'))}",
            f"payment within {f.get('paymentTermsDays') or 'unknown'} days",
            f"notice {f.get('noticePeriodDays') or 'unknown'} days",
            f"governing law {f.get('governingLaw') or 'not stated'}",
        ]
        if f.get("autoRenewal") is not None:
            parts.append(f"auto-renewal {'yes' if f['autoRenewal'] else 'no'}")
    return " · ".join(str(p) for p in parts)


def describe_result(result: QueryResult) -> str:
    q = result.query
    what = (
        f"{q.operation} of {q.field or DEFAULT_AMOUNT[q.category]}"
        if q.operation not in ("count", "list")
        else q.operation
    )
    text = f"{q.name}: {what} over {len(result.rows)} matching {q.category}s"
    if q.operation == "count":
        text += f" = {result.value}"
    elif result.best is not None:
        text += f" = {money(result.value, result.currency)} ({result.best.number or result.best.title})"
    elif result.value is not None:
        text += f" = {money(result.value, result.currency)}"
    elif result.by_currency:
        text += " = " + ", ".join(
            money(v, c) for c, v in sorted(result.by_currency.items())
        )
    if result.note:
        text += f" ({result.note})"
    return text
