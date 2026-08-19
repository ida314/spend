"""The projection: what a transaction is derived from, and what beats what."""

from __future__ import annotations

import json

import pytest

from spendtracker.project import Rules, project
from spendtracker.schema import ReceiptData


def extraction(n: int = 1, status: str = "ok", **fields) -> dict:
    data = ReceiptData.model_validate({
        "merchant": "TEST MART", "purchased_on": "2026-08-14", "total": "12.34", **fields})
    return {"id": n, "status": status, "note": None,
            "payload": data.model_dump_json() if status == "ok" else None}


def correction(field: str, value, n: int = 1) -> dict:
    return {"id": n, "field": field, "value": value, "created_at": f"2026-08-14T00:00:0{n}"}


@pytest.fixture
def rules():
    return Rules.load()


def test_a_receipt_with_no_extraction_yet_is_pending_not_failed(rules):
    txn, items = project(1, [], [], rules)
    assert txn["status"] == "pending"
    assert txn["total_cents"] is None


def test_a_receipt_whose_every_extraction_failed_is_failed_and_says_why(rules):
    failed = {"id": 1, "status": "error", "payload": None, "note": "JobFailed: engine died"}
    txn, _ = project(1, [failed], [], rules)
    assert txn["status"] == "failed"
    assert "engine died" in txn["review_reason"]


def test_the_newest_successful_extraction_is_the_base(rules):
    old = extraction(1, merchant="OLD NAME")
    new = extraction(2, merchant="NEW NAME")
    txn, _ = project(1, [old, new], [], rules)
    assert txn["merchant"] == "NEW NAME"
    assert txn["extraction_id"] == 2


def test_a_correction_outlives_a_later_re_extraction(rules):
    """The reason the projection exists. Re-reading a receipt with a better prompt must
    not silently undo a value a human typed in."""
    first = extraction(1, merchant="TARDER JOES")
    fix = correction("merchant", "TRADER JOE'S")
    later = extraction(2, merchant="TRADER JOE S")
    txn, _ = project(1, [first, later], [fix], rules)
    assert txn["merchant"] == "TRADER JOE'S"


def test_the_last_correction_to_a_field_wins(rules):
    txn, _ = project(1, [extraction()],
                     [correction("total", "20.00", 1), correction("total", "25.00", 2)], rules)
    assert txn["total_cents"] == 2500


def test_manual_entry_works_with_no_extraction_at_all(rules):
    """A receipt the model could not read is still a receipt. Typing the numbers in is
    the same mechanism as correcting them."""
    txn, _ = project(1, [], [correction("merchant", "CASH ONLY DINER", 1),
                            correction("total", "18.00", 2),
                            correction("purchased_on", "2026-08-15", 3)], rules)
    assert (txn["merchant"], txn["total_cents"], txn["status"]) == (
        "CASH ONLY DINER", 1800, "ok")


def test_deletion_is_a_tombstone_that_keeps_the_row(rules):
    txn, _ = project(1, [extraction()], [correction("deleted", "1")], rules)
    assert txn["deleted"] == 1
    assert txn["merchant"] == "TEST MART"   # still there, just not shown


def test_the_rules_file_categorises_and_beats_the_models_guess(rules):
    txn, _ = project(1, [extraction(merchant="TRADER JOE'S #546", category="restaurant")],
                     [], rules)
    assert txn["category"] == "groceries"


def test_the_models_guess_stands_for_a_merchant_no_rule_matches(rules):
    txn, _ = project(1, [extraction(merchant="NONESUCH CO", category="services")], [], rules)
    assert txn["category"] == "services"


def test_a_category_set_by_hand_beats_both(rules):
    txn, _ = project(1, [extraction(merchant="TRADER JOE'S", category="groceries")],
                     [correction("category", "household")], rules)
    assert txn["category"] == "household"


def test_a_missing_field_flags_for_review_but_keeps_the_numbers_it_has(rules):
    txn, _ = project(1, [extraction(total=None)], [], rules)
    assert txn["status"] == "needs_review"
    assert "total" in txn["review_reason"]
    assert txn["merchant"] == "TEST MART"


def test_items_that_do_not_add_up_to_the_printed_total_flag_for_review(rules):
    e = extraction(total="12.34", tax="1.00",
                   line_items=[{"description": "A", "total": "5.00"},
                               {"description": "B", "total": "5.00"}])
    txn, items = project(1, [e], [], rules)
    assert txn["status"] == "needs_review"
    assert "11.00" in txn["review_reason"] and "12.34" in txn["review_reason"]
    assert len(items) == 2          # shown anyway; flagged, not hidden


def test_items_that_do_add_up_pass(rules):
    e = extraction(total="11.00", tax="1.00",
                   line_items=[{"description": "A", "total": "5.00"},
                               {"description": "B", "total": "5.00"}])
    txn, _ = project(1, [e], [], rules)
    assert txn["status"] == "ok"


def test_a_penny_of_rounding_does_not_flag(rules):
    e = extraction(total="10.01",
                   line_items=[{"description": "A", "total": "5.00"},
                               {"description": "B", "total": "5.00"}])
    txn, _ = project(1, [e], [], rules)
    assert txn["status"] == "ok"


def test_an_unparseable_amount_becomes_absence_never_zero(rules):
    """Zero would enter the totals and be indistinguishable from a free item."""
    txn, _ = project(1, [extraction(total="THIRTY THREE")], [], rules)
    assert txn["total_cents"] is None
    assert txn["status"] == "needs_review"


def test_an_item_can_be_corrected_and_an_emptied_description_removes_it(rules):
    e = extraction(line_items=[{"description": "MYSTERY", "total": "5.00"},
                               {"description": "TAX", "total": "1.00"}])
    _, items = project(1, [e], [correction("item.1.description", "OAT MILK", 1),
                              correction("item.2.description", "", 2)], rules)
    assert [i["description"] for i in items] == ["OAT MILK"]


def test_projection_is_a_pure_function_of_its_inputs(rules):
    args = (1, [extraction()], [correction("merchant", "X")], rules)
    assert project(*args) == project(*args)
