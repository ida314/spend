"""Bank descriptions, and what a merchant name has to survive to be comparable.

These matter more than they look. The category rules match substrings against a merchant
name, and a statement row and a photographed receipt cannot be compared at all until they
agree on what to call the shop.
"""

from __future__ import annotations

import pytest

from spend import normalize


@pytest.mark.parametrize(("description", "expected"), [
    ("SQ *BLUE BOTTLE COFFEE   BROOKLYN NY", "Blue Bottle Coffee"),
    ("TST* DIME SQUARE 8005551234", "Dime Square"),
    ("AMZN Mktp US*RT4G91OJ3", "Amazon"),
    ("POS DEBIT 09/12 KEY FOOD #1188 BROOKLYN", "Key Food"),
    ("APLPAY 7-ELEVEN 3392", "7-Eleven"),
    ("TRADER JOES #546 BROOKLYN NY", "Trader Joe's"),
    ("WHOLEFDS BWY 10008", "Whole Foods"),
    ("PURCHASE AUTHORIZED ON 09/12 SPOTIFY USA", "Spotify"),
])
def test_a_processor_prefix_never_reaches_the_merchant_name(description, expected):
    assert normalize.merchant(description) == expected


def test_the_alias_file_survives_a_bank_padding_its_columns():
    """"CAPITAL ONE      AUTOPAY PYMT" does not contain "capital one autopay" as a substring
    at all, and the alias file would look broken for a reason nobody could guess."""
    assert normalize.merchant("CAPITAL ONE      AUTOPAY PYMT") == "Capital One Autopay"


def test_a_description_with_nothing_in_it_is_absence_not_an_empty_name():
    for empty in ("", "   ", None, "###", "12345"):
        assert normalize.merchant(empty) is None


def test_the_raw_description_is_never_what_gets_matched_for_a_category():
    """The whole reason this module exists: the shipped rule says "coffee"."""
    from spend.project import Rules
    rules = Rules.load()
    raw = "SQ *BLUE BOTTLE COFFEE 00291 BROOKLYN NY"
    assert rules.categorise(raw) == "restaurant"          # by luck: "coffee" is in there
    assert rules.categorise(normalize.merchant(raw)) == "restaurant"

    # This one is not luck. Nothing in the raw string matches any shipped pattern.
    raw = "WHOLEFDS BWY 10008"
    assert rules.categorise(raw) is None
    assert rules.categorise(normalize.merchant(raw)) == "groceries"


def test_two_spellings_of_one_shop_share_a_key():
    """What a receipt says and what a statement says have to meet somewhere."""
    from_receipt = normalize.key(normalize.merchant("TRADER JOE'S #546"))
    from_statement = normalize.key(normalize.merchant("TRADER JOES #546 BROOKLYN NY"))
    assert from_receipt == from_statement == "trader joes"


def test_a_flow_is_matched_on_the_raw_description_not_the_tidy_name():
    """Why flows.toml runs before normalisation, with the case that actually proves it.

    Apple Card writes a card payment as "ACH Deposit Internet Transfer" in `Description` and
    "Apple Card" in `Merchant`. The projector prefers `Merchant` for the name, because it is
    already clean -- so if flow were derived from the name, a $1,204 card payment would be
    classified from the string "Apple Card", match nothing, fall through to the structural
    default, and be counted as a refund. The signal lives only in the description.
    """
    flows = normalize.Flows.load()
    assert flows.classify("ACH Deposit Internet Transfer") == "payment"
    assert flows.classify("Apple Card") is None


@pytest.mark.parametrize(("description", "flow"), [
    ("PAYMENT THANK YOU - WEB", "payment"),
    ("ZELLE TO SAM", "transfer"),
    ("ONLINE TRANSFER TO SAVINGS", "transfer"),
    ("PAYROLL ACME INC DIR DEP", "income"),
    ("INTEREST CHARGE ON PURCHASES", "fee"),
    ("ACH Deposit Internet Transfer", "payment"),
    ("SQ *BLUE BOTTLE COFFEE", None),
])
def test_what_is_and_is_not_money_leaving_the_household(description, flow):
    assert normalize.Flows.load().classify(description) == flow
