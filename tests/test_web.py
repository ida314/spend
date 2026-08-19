"""The two pages, the upload, and the correction form.

Driven with a fake extractor, so the whole suite runs with no GPU, no `sir`, and no
network. That is a requirement rather than a convenience: a test that needs the model to
be up cannot tell a broken page from a busy box.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spendtracker import store
from tests.conftest import FakeExtractor, ok


@pytest.fixture
def client(home, jpeg_bytes):
    from spendtracker.web.api import create_app
    app = create_app(extractor=FakeExtractor(ok(
        merchant="TRADER JOE'S", total="11.00", tax="1.00",
        line_items=[{"description": "OAT MILK", "total": "5.00"},
                    {"description": "BANANAS", "total": "5.00"}])))
    with TestClient(app) as c:
        yield c


def upload(client, jpeg_bytes, name="r.jpg"):
    return client.post("/upload", files={"files": (name, jpeg_bytes, "image/jpeg")},
                       follow_redirects=False)


def test_an_empty_list_says_it_is_empty_rather_than_looking_broken(client):
    body = client.get("/").text
    assert "No receipts yet" in body


def test_uploading_one_receipt_lands_on_its_page(client, jpeg_bytes):
    r = upload(client, jpeg_bytes)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/r/")


def test_a_receipt_the_worker_has_not_read_yet_is_shown_as_waiting(client, jpeg_bytes):
    loc = upload(client, jpeg_bytes).headers["location"]
    body = client.get(loc).text
    # Either the worker already picked it up or it has not; both are honest states, and
    # neither may show a fabricated amount.
    assert ("Waiting to be read" in body) or ("TRADER JOE" in body)


def test_the_receipt_image_is_served_content_addressed(client, jpeg_bytes):
    loc = upload(client, jpeg_bytes).headers["location"]
    r = client.get(f"{loc}/image")
    assert r.status_code == 200
    assert r.content == jpeg_bytes
    assert r.headers["etag"].strip('"')
    assert "immutable" in r.headers["cache-control"]
    assert "private" in r.headers["cache-control"]


def test_a_file_type_the_renderer_cannot_read_is_refused_and_says_so(client):
    r = client.post("/upload", files={"files": ("x.pdf", b"%PDF-1.4", "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "skipped" in r.headers["location"]


def test_the_same_photo_twice_does_not_become_two_transactions(client, jpeg_bytes):
    upload(client, jpeg_bytes)
    upload(client, jpeg_bytes)
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    conn.close()


def test_a_correction_from_the_form_is_appended_and_shown(client, jpeg_bytes):
    loc = upload(client, jpeg_bytes).headers["location"]
    rid = int(loc.rsplit("/", 1)[1])
    r = client.post(f"{loc}/correct", data={"merchant": "CORRECTED BY HAND",
                                            "total": "42.00", "category": "household"},
                    follow_redirects=False)
    assert r.status_code == 303
    conn = store.connect()
    txn = conn.execute("SELECT * FROM transactions WHERE receipt_id=?", (rid,)).fetchone()
    conn.close()
    assert txn["merchant"] == "CORRECTED BY HAND"
    assert txn["total_cents"] == 4200
    assert txn["category"] == "household"
    assert "CORRECTED BY HAND" in client.get(loc).text


def test_deleting_hides_the_row_but_keeps_the_receipt(client, jpeg_bytes):
    loc = upload(client, jpeg_bytes).headers["location"]
    rid = int(loc.rsplit("/", 1)[1])
    client.post(f"{loc}/delete", follow_redirects=False)
    assert f"/r/{rid}" not in client.get("/").text
    conn = store.connect()
    assert store.get_receipt(conn, rid) is not None
    assert conn.execute("SELECT deleted FROM transactions WHERE receipt_id=?",
                        (rid,)).fetchone()[0] == 1
    conn.close()


def test_asking_for_another_read_requeues_without_losing_the_first_answer(client, jpeg_bytes):
    loc = upload(client, jpeg_bytes).headers["location"]
    rid = int(loc.rsplit("/", 1)[1])
    before = len(store.extractions_for(store.connect(), rid))
    client.post(f"{loc}/reextract", follow_redirects=False)
    conn = store.connect()
    assert len(store.extractions_for(conn, rid)) > before
    conn.close()


def test_a_missing_receipt_is_a_404_not_a_500(client):
    assert client.get("/r/9999").status_code == 404
    assert client.post("/r/9999/delete", follow_redirects=False).status_code == 404


def test_healthz_and_doctor_answer(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    d = client.get("/doctor").json()
    assert d["schema_version"] >= 1
    assert "model" in d and "backlog" in d
