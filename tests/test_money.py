"""Money is integer cents, and every separator convention a receipt can print."""

import pytest

from spendtracker.money import MoneyError, format_cents, to_cents


@pytest.mark.parametrize("text,cents", [
    ("33.03", 3303), ("$33.03", 3303), ("0.01", 1), ("12", 1200), (".99", 99),
    ("1,234.56", 123456), ("1.234,56", 123456), ("1234,56", 123456),
    ("1,234,567.89", 123456789),
])
def test_an_amount_written_any_common_way_is_the_same_number_of_cents(text, cents):
    assert to_cents(text) == cents


def test_a_grouped_thousand_is_not_read_as_a_fraction():
    assert to_cents("1,234") == 123400


def test_a_sub_cent_unit_price_rounds_instead_of_becoming_a_thousand_times_itself():
    # "2 @ 0.745" is a real line on a real receipt. Read as a grouped number it is
    # $745.00, which is the bug this assertion exists to keep fixed.
    assert to_cents("0.745") == 75
    assert to_cents("0.004") == 0


@pytest.mark.parametrize("text,cents", [("(4.50)", -450), ("5.00-", -500), ("-2.00", -200)])
def test_both_ways_a_receipt_writes_a_refund_are_negative(text, cents):
    assert to_cents(text) == cents


def test_absence_and_unparseable_are_different_answers():
    assert to_cents(None) is None
    assert to_cents("") is None
    with pytest.raises(MoneyError):
        to_cents("THIRTY THREE")


def test_no_monetary_value_is_ever_a_float():
    for text in ("0.1", "0.2", "33.03", "0.745"):
        assert isinstance(to_cents(text), int)


def test_the_float_that_motivates_this_module_does_not_occur():
    assert to_cents("0.10") + to_cents("0.20") == to_cents("0.30")


def test_formatting_is_display_only_and_marks_absence():
    assert format_cents(3303) == "$33.03"
    assert format_cents(None) == "—"
    assert format_cents(-450) == "-$4.50"
