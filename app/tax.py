from decimal import Decimal, ROUND_HALF_UP

PROVINCE_TAX = {
    "ON": [("HST", 0.13)],
    "BC": [("GST", 0.05), ("PST", 0.07)],
    "AB": [("GST", 0.05)],
    "SK": [("GST", 0.05), ("PST", 0.06)],
    "MB": [("GST", 0.05), ("PST", 0.07)],
    "QC": [("GST", 0.05), ("QST", 0.09975)],
    "NB": [("HST", 0.15)],
    "NS": [("HST", 0.15)],
    "PE": [("HST", 0.15)],
    "NL": [("HST", 0.15)],
}


def _half_up_cents(taxable_cents: int, rate: str) -> int:
    """Tax on a whole number of cents, rounded half-up to the cent (what a till, the card terminal and an accountant
    all do). Python's round() on a float rounds 0.585 down to 0.58 because 0.585 is stored as 0.58499999...; that
    made about 2% of tax lines a cent short."""
    exact = Decimal(taxable_cents) * Decimal(rate)            # still in cents, e.g. 450 * 0.13 = 58.5
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calc_canadian_tax(taxable: float, province: str = "ON"):
    rates = PROVINCE_TAX.get(province, PROVINCE_TAX["ON"])
    taxable_cents = int((Decimal(str(round(float(taxable or 0), 2))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    lines = []
    tax_cents = 0
    for label, rate in rates:
        cents = _half_up_cents(taxable_cents, repr(rate))
        tax_cents += cents
        lines.append({"label": f"{label} ({rate * 100:.3g}%)", "amount": cents / 100})
    return {
        "lines": lines,
        "tax_total": tax_cents / 100,
        "total": (taxable_cents + tax_cents) / 100,
    }

PROVINCE_LABELS = {
    "ON": "Ontario — HST 13%", "BC": "British Columbia — GST+PST 12%",
    "AB": "Alberta — GST 5%", "SK": "Saskatchewan — GST+PST 11%",
    "MB": "Manitoba — GST+PST 12%", "QC": "Quebec — GST+QST 14.975%",
    "NB": "New Brunswick — HST 15%", "NS": "Nova Scotia — HST 15%",
    "PE": "Prince Edward Island — HST 15%", "NL": "Newfoundland — HST 15%",
}
