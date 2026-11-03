from decimal import Decimal

import pytest

from docai.corpus.synthetic.world import WORLD_COUNT, build_world, polish_nip


@pytest.mark.parametrize("index", range(WORLD_COUNT))
def test_planted_anomalies_hold(index: int) -> None:
    world = build_world(index)

    capped = world.anomaly_contract("cap_exceeded")
    nets = [i.net for i in world.invoices_for(capped.vendor)]
    assert nets[0] + nets[1] < capped.cap < sum(nets)
    for contract in world.contracts:
        if contract != capped:
            assert (
                sum(i.net for i in world.invoices_for(contract.vendor)) < contract.cap
            )

    ended = world.anomaly_contract("after_contract_end")
    assert world.invoices_for(ended.vendor)[2].issued > ended.end

    terms = world.anomaly_contract("wrong_payment_terms")
    late = world.invoices_for(terms.vendor)[1]
    assert (late.due - late.issued).days != terms.payment_days

    priced = world.anomaly_contract("price_mismatch")
    line = world.invoices_for(priced.vendor)[1].lines[0]
    assert line.unit_price != priced.services[0].unit_price


@pytest.mark.parametrize("index", range(WORLD_COUNT))
def test_documents_add_up(index: int) -> None:
    world = build_world(index)
    assert (
        len(world.contracts) == 4
        and len(world.invoices) == 12
        and len(world.receipts) == 4
    )
    assert len({i.number for i in world.invoices}) == 12
    for invoice in world.invoices:
        assert invoice.gross == invoice.net + invoice.vat
        assert invoice.vat == (Decimal("0") if invoice.reverse_charge else invoice.vat)
    assert sum(i.format == "scanned_pdf" for i in world.invoices) == 3
    assert sum(i.paid_on is not None for i in world.invoices) == 4


def test_worlds_are_reproducible_and_distinct() -> None:
    assert build_world(1) == build_world(1)
    names = [v.name for i in range(WORLD_COUNT) for v in build_world(i).vendors]
    assert len(set(names)) == len(names)


def test_polish_nip_has_a_valid_check_digit() -> None:
    import random

    digits = polish_nip(random.Random(5)).replace("-", "")
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    assert sum(int(d) * w for d, w in zip(digits, weights)) % 11 == int(digits[-1])
