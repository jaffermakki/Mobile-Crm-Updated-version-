"""v52.1 — sales tax rounds half-up to the cent (found by the whole-build end-to-end run).

Python's round() on a float turned $4.50 x 13% = 0.585 into 0.58 (0.585 is stored as 0.58499...), so about 2% of tax
lines were a cent short against what the card terminal, a calculator or an accountant gets."""
from decimal import Decimal, ROUND_HALF_UP

import pytest

from app.tax import PROVINCE_TAX, calc_canadian_tax


def _ref(cents, rate):
    return (Decimal(cents) * Decimal(repr(rate)) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)


@pytest.mark.parametrize("amount,province,expect_tax", [
    (4.50, "ON", 0.59),       # 0.585 -> 0.59
    (6.50, "ON", 0.85),       # 0.845
    (12.50, "ON", 1.63),      # 1.625
    (53.99, "BC", 6.48),      # GST 2.6995 -> 2.70, PST 3.7793 -> 3.78
    (10.00, "AB", 0.50),
    (0.10, "AB", 0.01),       # 0.005 -> 0.01
    (0.09, "AB", 0.00),       # 0.0045 -> 0.00
])
def test_known_half_cent_cases(amount, province, expect_tax):
    r = calc_canadian_tax(amount, province)
    assert r["tax_total"] == pytest.approx(expect_tax, abs=1e-9)
    assert r["total"] == pytest.approx(round(amount + expect_tax, 2), abs=1e-9)


def test_every_amount_up_to_300_dollars_matches_exact_decimal_math_in_every_province():
    for prov, rates in PROVINCE_TAX.items():
        for cents in range(0, 30001):
            r = calc_canadian_tax(cents / 100, prov)
            ref = [_ref(cents, rate) for _, rate in rates]
            assert [Decimal(str(line["amount"])) for line in r["lines"]] == ref, (prov, cents)
            assert Decimal(str(r["tax_total"])) == sum(ref), (prov, cents)
            assert Decimal(str(r["total"])) == Decimal(cents) / 100 + sum(ref), (prov, cents)


def test_float_noise_in_the_input_does_not_move_the_answer():
    # a subtotal built from float arithmetic (e.g. 24.99 + 29.00 + 0.1 + 0.2) is a hair off its cents value
    noisy = 0.1 + 0.2 + 4.2
    assert calc_canadian_tax(noisy, "ON") == calc_canadian_tax(4.5, "ON")


def test_zero_and_unknown_province():
    assert calc_canadian_tax(0, "BC")["total"] == 0
    assert calc_canadian_tax(10, "ZZ") == calc_canadian_tax(10, "ON")      # unknown province falls back to Ontario
