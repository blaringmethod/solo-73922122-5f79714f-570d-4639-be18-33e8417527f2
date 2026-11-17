"""Single-report conflict adjudication: eligibility, versions, races."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import adjudicate
from app.main import create_app


def _client(demo, instance: str = "inst-a") -> TestClient:
    collector = demo.make_collector(instance)
    app = create_app(collector.cfg)
    app.state.collector = collector
    app.state.db = collector.db
    app.state.store = collector.store
    return TestClient(app)


def _conflict(demo, report_no: str = "R1", value: str = "99"):
    """Deliver a first report, then a conflicting correction for it."""
    demo.deliver("initial", [demo.attachment(report_no, value="12")])
    collector = demo.make_collector()
    collector.scan()
    demo.deliver(
        "corrected",
        [demo.attachment(report_no, value=value)],
    )
    collector.scan()
    message = collector.db.list_messages(1)[0]
    assert message["decision"] == "rejected"
    assert message["reason_code"] == "report_conflict"
    return collector, message


def test_conflict_candidate_is_held_back_and_listed(demo):
    collector, message = _conflict(demo)

    # The rejection still left no new report rows.
    assert [r["report_no"] for r in collector.db.list_reports()] == ["R1"]
    current = collector.db.current_version("lab@test", "INBOX", "R1")
    assert current["version"] == 1
    assert current["attachment_sha256"] != json.loads(message["candidate_attachments"])[0][
        "attachment_sha256"
    ]

    client = _client(demo)
    pending = client.get("/api/conflicts").json()
    assert len(pending) == 1
    assert pending[0]["message"]["id"] == message["id"]
    assert pending[0]["report_no"] == "R1"

    page = client.get(f"/conflicts/{message['id']}")
    assert page.status_code == 200
    assert "现行有效报告" in page.text and "候选原件" in page.text
    assert pending[0]["candidate"]["attachment_sha256"] in page.text


def test_confirm_promotes_candidate_and_keeps_history(demo):
    collector, message = _conflict(demo)
    client = _client(demo)

    view = client.get(f"/api/conflicts/{message['id']}").json()
    expected = view["current"]

    response = client.post(
        f"/api/conflicts/{message['id']}/confirm",
        json={
            "report_no": view["report_no"],
            "candidate_sha256": view["candidate"]["attachment_sha256"],
            "expected_current_version": expected["version"],
            "expected_current_sha256": expected["attachment_sha256"],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["version"] == 2

    versions = collector.db.report_versions("lab@test", "INBOX", "R1")
    assert [v["version"] for v in versions] == [2, 1]
    states = {v["version"]: v["state"] for v in versions}
    assert states == {1: "superseded", 2: "current"}

    report = collector.db.get_report_by_no("lab@test", "INBOX", "R1")
    assert report["value"] == "99"
    assert report["attachment_sha256"] == view["candidate"]["attachment_sha256"]

    # Both versions remain downloadable.
    for version in versions:
        download = client.get(f"/api/report-versions/{version['id']}/attachment")
        assert download.status_code == 200
        assert "报告号,样本号,项目,测值" in download.content.decode()

    # The original rejection receipt and original message are untouched.
    receipt = client.get(f"/api/messages/{message['id']}/receipt")
    assert json.loads(receipt.content)["type"] == "rejection"
    original = client.get(f"/api/messages/{message['id']}/original")
    assert original.status_code == 200

    adjudications = collector.db.list_adjudications()
    assert len(adjudications) == 1
    decision_receipt = client.get(f"/api/adjudications/{adjudications[0]['id']}/receipt")
    assert json.loads(decision_receipt.content)["type"] == "adjudication"


def test_stale_confirmation_from_other_instance_is_refused(demo):
    collector, message = _conflict(demo)
    client_a = _client(demo, "inst-a")
    client_b = _client(demo, "inst-b")

    view_a = client_a.get(f"/api/conflicts/{message['id']}").json()
    view_b = client_b.get(f"/api/conflicts/{message['id']}").json()
    # Both engineers opened the page at the same current version, but each
    # page carries its own token.
    assert view_a["page_token"] != view_b["page_token"]
    payload_b = {
        "report_no": view_b["report_no"],
        "candidate_sha256": view_b["candidate"]["attachment_sha256"],
        "expected_current_version": view_b["current"]["version"],
        "expected_current_sha256": view_b["current"]["attachment_sha256"],
        "page_token": view_b["page_token"],
    }
    payload = {
        "report_no": view_a["report_no"],
        "candidate_sha256": view_a["candidate"]["attachment_sha256"],
        "expected_current_version": view_a["current"]["version"],
        "expected_current_sha256": view_a["current"]["attachment_sha256"],
        "page_token": view_a["page_token"],
    }

    first = client_b.post(f"/api/conflicts/{message['id']}/confirm", json=payload_b)
    assert first.status_code == 200 and first.json()["status"] == "created"

    # A holds the page it read before B adjudicated; confirming must fail and
    # must not produce version 3 or change the current report.
    second = client_a.post(f"/api/conflicts/{message['id']}/confirm", json=payload)
    assert second.status_code == 409
    assert "过期" in second.json()["detail"]

    versions = collector.db.report_versions("lab@test", "INBOX", "R1")
    assert [v["version"] for v in versions] == [2, 1]
    current = collector.db.current_version("lab@test", "INBOX", "R1")
    assert current["version"] == 2
    assert current["attachment_sha256"] == payload["candidate_sha256"]


def test_repeated_confirmation_adds_no_version(demo):
    collector, message = _conflict(demo)
    client = _client(demo)
    view = client.get(f"/api/conflicts/{message['id']}").json()
    payload = {
        "report_no": view["report_no"],
        "candidate_sha256": view["candidate"]["attachment_sha256"],
        "expected_current_version": view["current"]["version"],
        "expected_current_sha256": view["current"]["attachment_sha256"],
        "page_token": view["page_token"],
    }

    assert client.post(
        f"/api/conflicts/{message['id']}/confirm", json=payload
    ).json()["status"] == "created"
    # Same decision submitted again, now with a stale expectation: it is a
    # duplicate, not a conflict, so it is idempotent.
    again = client.post(f"/api/conflicts/{message['id']}/confirm", json=payload)
    assert again.status_code == 200
    assert again.json()["status"] == "idempotent"
    assert again.json()["version"] == 2
    assert [v["version"] for v in collector.db.report_versions("lab@test", "INBOX", "R1")] == [2, 1]


def test_failed_save_leaves_current_report_unchanged(demo, monkeypatch):
    collector, message = _conflict(demo)
    view_client = _client(demo)
    view = view_client.get(f"/api/conflicts/{message['id']}").json()

    def boom(_receipt):
        raise OSError("disk full")

    monkeypatch.setattr(collector.store, "put_receipt", boom)
    with pytest.raises(OSError):
        adjudicate.confirm(
            collector.db,
            collector.store,
            account="lab@test",
            mailbox="INBOX",
            message_id=message["id"],
            report_no=view["report_no"],
            candidate_sha256=view["candidate"]["attachment_sha256"],
            expected_current_version=view["current"]["version"],
            expected_current_sha256=view["current"]["attachment_sha256"],
            instance_id="inst-a",
        )

    current = collector.db.current_version("lab@test", "INBOX", "R1")
    assert current["version"] == 1
    assert current["attachment_sha256"] == view["current"]["attachment_sha256"]
    assert collector.db.list_adjudications() == []
    assert collector.db.get_report_by_no("lab@test", "INBOX", "R1")["value"] == "12"


def test_confirm_rejects_missing_candidate_file(demo):
    collector, message = _conflict(demo)
    client = _client(demo)
    view = client.get(f"/api/conflicts/{message['id']}").json()
    (collector.store.root / view["candidate"]["attachment_path"]).unlink()

    response = client.post(
        f"/api/conflicts/{message['id']}/confirm",
        json={
            "report_no": view["report_no"],
            "candidate_sha256": view["candidate"]["attachment_sha256"],
            "expected_current_version": view["current"]["version"],
            "expected_current_sha256": view["current"]["attachment_sha256"],
        },
    )
    assert response.status_code == 422
    assert collector.db.current_version("lab@test", "INBOX", "R1")["version"] == 1


def test_only_single_attachment_conflicts_are_adjudicable(demo):
    demo.deliver("initial", [demo.attachment("R1")])
    collector = demo.make_collector()
    collector.scan()
    # A conflict-rejected mail carrying two attachments: R2 cannot be adopted
    # implicitly, so it is not offered for adjudication.
    demo.deliver(
        "two",
        [demo.attachment("R2"), demo.attachment("R1", value="99")],
    )
    collector.scan()
    message = collector.db.list_messages(1)[0]
    assert message["reason_code"] == "report_conflict"

    ok, reason = adjudicate.eligibility(message)
    assert not ok and "多个附件" in reason
    client = _client(demo)
    assert client.get("/api/conflicts").json() == []
    assert client.get(f"/api/conflicts/{message['id']}").json()["eligible"] is False

    # Other rejection reasons are never adjudicable.
    demo.deliver("bad", [{"filename": "R3.csv", "content": "bad,header\n1,2\n"}])
    collector.scan()
    bad = collector.db.list_messages(1)[0]
    ok, reason = adjudicate.eligibility(bad)
    assert not ok
    assert client.post(
        f"/api/conflicts/{bad['id']}/confirm",
        json={
            "report_no": "R3",
            "candidate_sha256": "0" * 64,
            "expected_current_version": 1,
            "expected_current_sha256": "0" * 64,
        },
    ).status_code == 422


def test_later_mail_is_judged_against_current_version(demo):
    collector, message = _conflict(demo, value="99")
    client = _client(demo)
    view = client.get(f"/api/conflicts/{message['id']}").json()
    assert client.post(
        f"/api/conflicts/{message['id']}/confirm",
        json={
            "report_no": view["report_no"],
            "candidate_sha256": view["candidate"]["attachment_sha256"],
            "expected_current_version": view["current"]["version"],
            "expected_current_sha256": view["current"]["attachment_sha256"],
        },
    ).json()["status"] == "created"

    # Re-sending the newly confirmed content is reuse, not a conflict.
    demo.deliver("repeat-new", [demo.attachment("R1", value="99")])
    reuse = collector.scan()
    assert reuse.counts["reused_reports"] == 1
    assert len(collector.db.list_reports()) == 1

    # Re-sending the superseded original now conflicts against the new current.
    demo.deliver("repeat-old", [demo.attachment("R1", value="12")])
    old = collector.scan()
    assert old.counts["rejected"] == 1
    latest = collector.db.list_messages(1)[0]
    assert latest["reason_code"] == "report_conflict"
    # The conflict rejection is again adjudicable (candidate = old content).
    assert adjudicate.eligibility(latest)[0]


def test_conflict_list_page_and_form_flow(demo):
    collector, message = _conflict(demo)
    client = _client(demo)
    listing = client.get("/conflicts")
    assert listing.status_code == 200
    assert "待裁决" in listing.text

    detail = client.get(f"/conflicts/{message['id']}")
    assert detail.status_code == 200
    view = client.get(f"/api/conflicts/{message['id']}").json()
    import re

    token = re.search(r'name="page_token" value="([0-9a-f]+)"', detail.text).group(1)
    response = client.post(
        "/actions/adjudicate",
        data={
            "message_id": str(message["id"]),
            "report_no": view["report_no"],
            "candidate_sha256": view["candidate"]["attachment_sha256"],
            "expected_current_version": str(view["current"]["version"]),
            "expected_current_sha256": view["current"]["attachment_sha256"],
            "page_token": token,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "第 2 版为当前有效报告" in response.text
    assert collector.db.current_version("lab@test", "INBOX", "R1")["version"] == 2


def test_legacy_database_versions_are_seeded(tmp_path):
    """A database written before the version ledger existed upgrades cleanly."""
    import sqlite3

    from app.db import Database

    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE message_record (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL, mailbox TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL, generation INTEGER NOT NULL,
            raw_sha256 TEXT NOT NULL, subject TEXT, from_addr TEXT, date_header TEXT,
            decision TEXT NOT NULL, reason_code TEXT, reason TEXT,
            mark_state TEXT NOT NULL DEFAULT 'pending', mark_attempts INTEGER NOT NULL DEFAULT 0,
            marked_at TEXT, receipt_sha256 TEXT NOT NULL, receipt_path TEXT NOT NULL,
            original_path TEXT NOT NULL, report_nos TEXT NOT NULL DEFAULT '[]',
            detail TEXT, scan_instance TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (account, mailbox, uidvalidity, uid)
        );
        CREATE TABLE report (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL, mailbox TEXT NOT NULL, report_no TEXT NOT NULL,
            sample_no TEXT NOT NULL, item TEXT NOT NULL, value TEXT NOT NULL,
            rows_json TEXT NOT NULL, attachment_sha256 TEXT NOT NULL,
            attachment_name TEXT NOT NULL, attachment_path TEXT NOT NULL,
            source_message_id INTEGER NOT NULL REFERENCES message_record(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, UNIQUE (account, mailbox, report_no)
        );
        INSERT INTO message_record
          (id, account, mailbox, uidvalidity, uid, generation, raw_sha256,
           decision, mark_state, receipt_sha256, receipt_path, original_path,
           report_nos, created_at, updated_at)
        VALUES (1,'lab@test','INBOX',1,1,1,'deadbeef','accepted','marked',
                'rsha','receipts/rsha.json','msgs/deadbeef.eml','["R1"]',
                '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
        INSERT INTO report VALUES
          (1,'lab@test','INBOX','R1','S1','ALT','7',
           '[["R1","S1","ALT","7"]]','abc','R1.csv',
           'attachments/R1/abc-R1.csv',1,'2026-01-01T00:00:00+00:00',
           '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    db.init()  # creates the other tables and seeds the ledger
    versions = db.report_versions("lab@test", "INBOX", "R1")
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["state"] == "current"
    assert versions[0]["attachment_sha256"] == "abc"


def test_form_double_submit_is_idempotent(demo):
    collector, message = _conflict(demo)
    from fastapi.testclient import TestClient

    client = _client(demo)
    page = client.get(f"/conflicts/{message['id']}")
    import re

    token = re.search(r'name="page_token" value="([0-9a-f]+)"', page.text).group(1)
    view = client.get(f"/api/conflicts/{message['id']}").json()
    form = {
        "message_id": str(message["id"]),
        "report_no": view["report_no"],
        "candidate_sha256": view["candidate"]["attachment_sha256"],
        "expected_current_version": str(view["current"]["version"]),
        "expected_current_sha256": view["current"]["attachment_sha256"],
        "page_token": token,
    }
    first = client.post("/actions/adjudicate", data=form, follow_redirects=True)
    assert "第 2 版为当前有效报告" in first.text
    second = client.post("/actions/adjudicate", data=form, follow_redirects=True)
    assert "本次未新增版本" in second.text
    assert [v["version"] for v in collector.db.report_versions("lab@test", "INBOX", "R1")] == [2, 1]


def test_concurrent_confirmations_only_one_wins(demo):
    """Two instances racing on the same page: exactly one version is created."""
    import threading

    collector, message = _conflict(demo)
    db_a = demo.make_collector("inst-a")
    db_b = demo.make_collector("inst-b")
    store = collector.store
    view_a = adjudicate.conflict_view(
        db_a.db, db_a.db.get_message(message["id"]), "lab@test", "INBOX"
    )
    view_b = adjudicate.conflict_view(
        db_b.db, db_b.db.get_message(message["id"]), "lab@test", "INBOX"
    )
    # Two engineers, two rendered pages: same current version, different tokens.
    assert view_a["page_token"] != view_b["page_token"]
    common = dict(
        account="lab@test",
        mailbox="INBOX",
        message_id=message["id"],
        report_no=view_a["report_no"],
        candidate_sha256=view_a["candidate"]["attachment_sha256"],
        expected_current_version=view_a["current"]["version"],
        expected_current_sha256=view_a["current"]["attachment_sha256"],
    )
    payload_a = dict(common, page_token=view_a["page_token"])
    payload_b = dict(common, page_token=view_b["page_token"])
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def run(target, payload):
        try:
            barrier.wait()
            results.append(
                adjudicate.confirm(target.db, store, instance_id="inst-race", **payload)
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(db_a, payload_a)),
        threading.Thread(target=run, args=(db_b, payload_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1 and len(errors) == 1, (results, errors)
    assert isinstance(errors[0], adjudicate.StaleDecision)
    assert results[0]["status"] == "created"
    versions = collector.db.report_versions("lab@test", "INBOX", "R1")
    assert [v["version"] for v in versions] == [2, 1]
    assert collector.db.list_adjudications() and len(collector.db.list_adjudications()) == 1


def test_ineligible_conflict_page_explains_refusal(demo):
    demo.deliver("initial", [demo.attachment("R1")])
    collector = demo.make_collector()
    collector.scan()
    demo.deliver("two", [demo.attachment("R2"), demo.attachment("R1", value="99")])
    collector.scan()
    message = collector.db.list_messages(1)[0]
    client = _client(demo)

    page = client.get(f"/conflicts/{message['id']}")
    assert page.status_code == 200
    assert "不可裁决" in page.text
    assert "多个附件" in page.text
    # No confirmation form is rendered for an ineligible message.
    assert 'action="/actions/adjudicate"' not in page.text

    # The candidate download endpoint is likewise closed.
    assert client.get(f"/conflicts/{message['id']}/candidate").status_code == 404


def test_candidate_download_and_receipt_traceability(demo):
    collector, message = _conflict(demo)
    client = _client(demo)
    view = client.get(f"/api/conflicts/{message['id']}").json()
    download = client.get(f"/conflicts/{message['id']}/candidate")
    assert download.status_code == 200
    body = download.content.decode()
    assert "报告号,样本号,项目,测值" in body and "99" in body
    # Its digest matches what the page displayed.
    import hashlib

    assert hashlib.sha256(download.content).hexdigest() == view["candidate"][
        "attachment_sha256"
    ]
    # The rejection receipt records the same candidate for offline audit.
    receipt = json.loads(client.get(f"/api/messages/{message['id']}/receipt").content)
    assert receipt["candidate_attachments"][0]["attachment_sha256"] == view["candidate"][
        "attachment_sha256"
    ]
