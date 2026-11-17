"""Human adjudication of a single-report conflict.

The collector rejects a whole email when a report number already exists with a
different original.  This module holds the *only* sanctioned way to resolve
such a conflict: a human compares the current report with the candidate
attachment from the rejected email and promotes the candidate to the current
version.

Rules enforced here:

* Only messages rejected *specifically* because of ``report_conflict`` and
  carrying exactly one compliant CSV attachment are adjudicable.
* Confirmation is optimistic-concurrency controlled: the page carries the
  current version number and attachment SHA-256 that the engineer saw.  If
  another instance adjudicated first, that expectation no longer matches and
  the late confirmation is refused (``StaleDecision``).
* Exactly one current version exists per report number.  Superseded versions
  keep their attachment path, so both originals stay downloadable; the
  rejection receipt on the source message is never modified.
* Re-submitting an already-recorded decision is idempotent and adds no version.
* The version flip and the audit row commit in one SQLite transaction after all
  files are durable, so a failed save cannot change the current report.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .db import Database, transaction, utcnow
from .store import Store


class AdjudicationError(RuntimeError):
    """Base class for a refused adjudication."""


class NotEligible(AdjudicationError):
    """The message is not a single-report conflict-rejected email."""


class StaleDecision(AdjudicationError):
    """The confirmation refers to a current version that has since changed."""


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def candidate_attachments(message_row) -> list[dict[str, Any]]:
    """Validated attachments persisted when the email was conflict-rejected."""
    raw = message_row["candidate_attachments"] if "candidate_attachments" in message_row.keys() else None
    items = _load_json(raw, [])
    return items if isinstance(items, list) else []


def _detail_reasons(message_row) -> list[dict[str, Any]]:
    return _load_json(message_row["detail"], [])


def eligibility(message_row) -> tuple[bool, str | None]:
    """Return ``(eligible, reason_if_not)`` for a message row."""
    if message_row is None:
        return False, "邮件记录不存在"
    if message_row["decision"] != "rejected":
        return False, "该邮件不是拒收记录"
    reasons = _detail_reasons(message_row)
    if message_row["reason_code"] != "report_conflict" or any(
        reason.get("code") != "report_conflict" for reason in reasons
    ):
        return False, "拒收原因不是报告号冲突"
    candidates = candidate_attachments(message_row)
    if len(candidates) == 1:
        return True, None
    if not candidates:
        return False, "该邮件没有可裁决的合规 CSV 附件"
    return False, "该邮件含多个附件，请分别核实后由发件方重发单附件更正邮件"


def _version_dict(row) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def new_page_token() -> str:
    """Opaque token identifying one rendered comparison page.

    Two pages can show the same versions; the token is what lets the server
    tell an ordinary resubmission of *this* page (idempotent) from a
    confirmation that was rendered before another instance adjudicated
    (stale, must be refused).
    """
    return uuid.uuid4().hex


def conflict_view(
    db: Database, message_row, account: str, mailbox: str
) -> dict[str, Any]:
    """Everything the comparison page needs, plus its eligibility verdict."""
    ok, reason = eligibility(message_row)
    item: dict[str, Any] = {
        "message": dict(message_row),
        "eligible": ok,
        "ineligible_reason": reason,
        "candidates": candidate_attachments(message_row),
        "current": None,
        "versions": [],
        "adjudications": [],
        "page_token": new_page_token(),
    }
    if not ok:
        return item
    candidate = item["candidates"][0]
    report_no = candidate["report_no"]
    item["report_no"] = report_no
    current = db.current_version(account, mailbox, report_no)
    item["current"] = _version_dict(current)
    item["versions"] = [dict(r) for r in db.report_versions(account, mailbox, report_no)]
    item["adjudications"] = [
        dict(r) for r in db.adjudications_for_message(message_row["id"])
    ]
    decision = next(
        (
            row
            for row in item["adjudications"]
            if row["report_no"] == report_no
            and row["candidate_sha256"] == candidate["attachment_sha256"]
        ),
        None,
    )
    item["decision"] = decision
    item["candidate"] = candidate
    return item


def pending_conflicts(db: Database, account: str, mailbox: str) -> list[dict[str, Any]]:
    views = []
    for row in db.conflict_messages(account, mailbox, limit=200):
        ok, reason = eligibility(row)
        if not ok:
            continue
        candidate = candidate_attachments(row)[0]
        views.append(
            {
                "message": dict(row),
                "candidate": candidate,
                "report_no": candidate["report_no"],
                "decided": db.adjudication_for_candidate(
                    account, mailbox, candidate["report_no"], candidate["attachment_sha256"]
                )
                is not None,
            }
        )
    return views


def confirm(
    db: Database,
    store: Store,
    *,
    account: str,
    mailbox: str,
    message_id: int,
    report_no: str,
    candidate_sha256: str,
    expected_current_version: int,
    expected_current_sha256: str,
    instance_id: str,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Promote the candidate attachment to the current report version.

    Returns a dict with ``status`` of ``created``, ``idempotent`` or ``noop``.
    Raises :class:`NotEligible` or :class:`StaleDecision` otherwise.  Every
    check and write happens inside one ``BEGIN IMMEDIATE`` transaction, which is
    what serialises two instances adjudicating the same report number.

    ``page_token`` identifies the rendered comparison page.  It separates the
    two "nothing new to do" cases from a genuinely stale confirmation:

    * same token re-submitted (double click / browser resend) → idempotent;
    * token from a page rendered before another instance adjudicated, whose
      expectation no longer matches → :class:`StaleDecision`.
    """
    with db.connect() as conn:
        with transaction(conn):
            message = conn.execute(
                "SELECT * FROM message_record WHERE id=?", (message_id,)
            ).fetchone()
            ok, reason = eligibility(message)
            if not ok:
                raise NotEligible(reason or "该邮件不可裁决")
            candidates = candidate_attachments(message)
            candidate = next(
                (
                    item
                    for item in candidates
                    if item.get("report_no") == report_no
                    and item.get("attachment_sha256") == candidate_sha256
                ),
                None,
            )
            if candidate is None:
                raise NotEligible("候选附件与拒收记录不一致，请重新打开页面")
            if message["account"] != account or message["mailbox"] != mailbox:
                raise NotEligible("邮件不属于当前账号或邮箱")

            # Verify the candidate bytes on disk against the recorded digest.
            candidate_path = store.absolute(candidate["attachment_path"])
            if not candidate_path.is_file():
                raise NotEligible("候选原件文件缺失，无法裁决")
            if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != candidate_sha256:
                raise NotEligible("候选原件校验失败，拒绝对比")

            previous = conn.execute(
                """SELECT * FROM adjudication
                   WHERE account=? AND mailbox=? AND report_no=?
                     AND candidate_sha256=?""",
                (account, mailbox, report_no, candidate_sha256),
            ).fetchone()
            # Re-submitting the very page that produced the decision is a
            # duplicate, never a new version.
            if (
                previous is not None
                and page_token is not None
                and previous["page_token"] == page_token
            ):
                return {
                    "status": "idempotent",
                    "adjudication_id": int(previous["id"]),
                    "version": int(previous["resulting_version"]),
                    "version_created": False,
                }

            current = conn.execute(
                """SELECT * FROM report_version
                   WHERE account=? AND mailbox=? AND report_no=?
                     AND state='current'
                   ORDER BY version DESC LIMIT 1""",
                (account, mailbox, report_no),
            ).fetchone()
            if current is None:
                raise NotEligible("当前有效报告不存在，无法裁决")
            # This is the optimistic-concurrency gate: the page saw a specific
            # current version; if it has moved on, the confirmation is stale.
            if (
                int(current["version"]) != int(expected_current_version)
                or current["attachment_sha256"] != expected_current_sha256
            ):
                raise StaleDecision(
                    "现行报告已在其他实例更新（现为第 "
                    f"{current['version']} 版 {current['attachment_sha256'][:12]}…），"
                    "本次确认已过期，请重新打开页面核对"
                )
            if current["attachment_sha256"] == candidate_sha256:
                return {
                    "status": "idempotent" if previous is not None else "noop",
                    "adjudication_id": int(previous["id"]) if previous is not None else None,
                    "version": int(current["version"]),
                    "version_created": False,
                }
            if previous is not None:
                # The candidate was already promoted by another page, but the
                # confirmation's expectation still matches the current version
                # because that page was rendered afterwards.
                return {
                    "status": "idempotent",
                    "adjudication_id": int(previous["id"]),
                    "version": int(current["version"]),
                    "version_created": False,
                }

            next_version = int(
                conn.execute(
                    """SELECT COALESCE(MAX(version), 0) FROM report_version
                       WHERE account=? AND mailbox=? AND report_no=?""",
                    (account, mailbox, report_no),
                ).fetchone()[0]
            ) + 1
            now = utcnow()
            rows = candidate.get("rows") or []
            first = rows[0] if rows else [report_no, "", "", ""]

            receipt = {
                "type": "adjudication",
                "account": account,
                "mailbox": mailbox,
                "report_no": report_no,
                "message_id": message_id,
                "uidvalidity": message["uidvalidity"],
                "uid": message["uid"],
                "rejected_raw_sha256": message["raw_sha256"],
                "rejection_receipt_sha256": message["receipt_sha256"],
                "candidate_attachment_sha256": candidate_sha256,
                "candidate_attachment_name": candidate["attachment_name"],
                "previous_version": int(current["version"]),
                "previous_attachment_sha256": current["attachment_sha256"],
                "resulting_version": next_version,
                "instance_id": instance_id,
                "created_at": now,
            }
            receipt_sha, receipt_rel = store.put_receipt(receipt)

            conn.execute(
                """UPDATE report_version
                   SET state='superseded', superseded_at=?
                   WHERE account=? AND mailbox=? AND report_no=?
                     AND state='current'""",
                (now, account, mailbox, report_no),
            )
            conn.execute(
                """INSERT INTO report_version
                   (account, mailbox, report_no, version, state,
                    sample_no, item, value, rows_json, attachment_sha256,
                    attachment_name, attachment_path, source_message_id,
                    created_at)
                   VALUES (?, ?, ?, ?, 'current', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account,
                    mailbox,
                    report_no,
                    next_version,
                    first[1],
                    first[2],
                    first[3],
                    json.dumps(rows, ensure_ascii=False),
                    candidate_sha256,
                    candidate["attachment_name"],
                    candidate["attachment_path"],
                    message_id,
                    now,
                ),
            )
            conn.execute(
                """UPDATE report
                   SET sample_no=?, item=?, value=?, rows_json=?,
                       attachment_sha256=?, attachment_name=?, attachment_path=?,
                       source_message_id=?, updated_at=?
                   WHERE account=? AND mailbox=? AND report_no=?""",
                (
                    first[1],
                    first[2],
                    first[3],
                    json.dumps(rows, ensure_ascii=False),
                    candidate_sha256,
                    candidate["attachment_name"],
                    candidate["attachment_path"],
                    message_id,
                    now,
                    account,
                    mailbox,
                    report_no,
                ),
            )
            cur = conn.execute(
                """INSERT INTO adjudication
                   (account, mailbox, report_no, message_id, candidate_sha256,
                    previous_sha256, previous_version, resulting_version,
                    expected_sha256, instance_id, page_token, receipt_sha256,
                    receipt_path, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account,
                    mailbox,
                    report_no,
                    message_id,
                    candidate_sha256,
                    current["attachment_sha256"],
                    int(current["version"]),
                    next_version,
                    expected_current_sha256,
                    instance_id,
                    page_token,
                    receipt_sha,
                    str(receipt_rel),
                    now,
                ),
            )
            return {
                "status": "created",
                "adjudication_id": int(cur.lastrowid),
                "version": next_version,
                "version_created": True,
                "previous_version": int(current["version"]),
            }
