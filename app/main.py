"""FastAPI web application and API for the laboratory inbox bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import adjudicate
from .collector import Collector, ScanBusy
from .config import Settings, settings
from .db import Database
from .store import Store

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("bridge.web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["fromjson"] = lambda value: json.loads(value or "[]")


class ConfirmRequest(BaseModel):
    """Optimistic-concurrency payload for a conflict confirmation."""

    report_no: str
    candidate_sha256: str
    expected_current_version: int
    expected_current_sha256: str
    page_token: str | None = None


def _confirm_from_form(db, store, cfg, form, message_id: int) -> dict[str, Any]:
    fields = dict(form)
    for required in (
        "report_no",
        "candidate_sha256",
        "expected_current_version",
        "expected_current_sha256",
        "page_token",
    ):
        if not fields.get(required):
            raise adjudicate.AdjudicationError(f"缺少字段：{required}")
    try:
        expected_version = int(fields["expected_current_version"])
    except (TypeError, ValueError) as exc:
        raise adjudicate.AdjudicationError("现行版本号无效") from exc
    return adjudicate.confirm(
        db,
        store,
        account=cfg.account,
        mailbox=cfg.imap_mailbox,
        message_id=message_id,
        report_no=fields["report_no"],
        candidate_sha256=fields["candidate_sha256"],
        expected_current_version=expected_version,
        expected_current_sha256=fields["expected_current_sha256"],
        instance_id=cfg.instance_id,
        page_token=fields.get("page_token") or None,
    )


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _message_view(db: Database, row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["detail_list"] = json.loads(item.get("detail") or "[]")
    except json.JSONDecodeError:
        item["detail_list"] = []
    try:
        item["report_nos_list"] = json.loads(item.get("report_nos") or "[]")
    except json.JSONDecodeError:
        item["report_nos_list"] = []
    try:
        item["candidates"] = json.loads(item.get("candidate_attachments") or "[]")
    except json.JSONDecodeError:
        item["candidates"] = []
    eligible, reason = adjudicate.eligibility(row)
    item["adjudicable"] = eligible
    item["adjudicable_reason"] = reason
    item["reports"] = [dict(r) for r in db.reports_for_message(item["id"])]
    return item


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or settings
    db = Database(cfg.database_path)
    store = Store(cfg.storage_dir)
    collector = Collector(cfg, db, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init()
        store.init()
        stop = asyncio.Event()
        task: asyncio.Task | None = None
        if cfg.auto_scan_interval > 0:
            task = asyncio.create_task(auto_loop(stop))
        log.info(
            "instance %s ready db=%s storage=%s imap=%s:%s account=%s",
            cfg.instance_id,
            cfg.database_path,
            cfg.storage_dir,
            cfg.imap_host,
            cfg.imap_port,
            cfg.account,
        )
        try:
            yield
        finally:
            stop.set()
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def auto_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.auto_scan_interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(run_scan, "auto", False)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - loop must survive
                log.exception("automatic scan failed")

    def run_scan(trigger: str, raise_busy: bool = True):
        try:
            return collector.scan(trigger=trigger).as_dict()
        except ScanBusy:
            if raise_busy:
                raise
            log.info("scan skipped: another instance owns the lease")
            return None

    app = FastAPI(title=cfg.title, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.store = store
    app.state.collector = collector
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        mark_filter = request.query_params.get("mark") or None
        messages = [_message_view(db, row) for row in db.list_messages(100, mark_filter)]
        reports = [dict(r) for r in db.list_reports(200)]
        state = db.get_state(cfg.account, cfg.imap_mailbox)
        pending_conflicts = adjudicate.pending_conflicts(db, cfg.account, cfg.imap_mailbox)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "cfg": cfg,
                "stats": db.stats(),
                "pending_conflicts": pending_conflicts,
                "messages": messages,
                "reports": reports,
                "scans": [dict(r) for r in db.recent_scans(15)],
                "state": _row_to_dict(state),
                "mark_filter": mark_filter or "",
                "flash": request.query_params.get("flash", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.post("/actions/scan")
    async def action_scan():
        try:
            result = await asyncio.to_thread(run_scan, "web", True)
        except ScanBusy as exc:
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        except Exception as exc:  # surface the failure in the UI
            log.exception("web scan failed")
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        summary = (
            f"扫描完成：读取 {result['scanned']}，收讫 {result['accepted']}，"
            f"拒收 {result['rejected']}，跳过 {result['skipped']}，"
            f"标记 {result['marked']}，复用原件 {result['reused_reports']}"
        )
        if result["status"] != "completed":
            summary += f"；{result['error'] or result['status']}"
            return RedirectResponse(f"/?flash=&error={_q(summary)}", status_code=303)
        return RedirectResponse(f"/?flash={_q(summary)}", status_code=303)

    @app.post("/actions/retry-marks")
    async def action_retry_marks():
        try:
            marked = await asyncio.to_thread(collector.mark_pending_public)
        except ScanBusy as exc:
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        except Exception as exc:
            log.exception("retry marks failed")
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        return RedirectResponse(f"/?flash={_q(f'补标记完成：{marked} 封')}", status_code=303)

    # ---------------------------------------------------------- adjudication
    @app.get("/conflicts", response_class=HTMLResponse)
    def conflict_list(request: Request):
        pending = adjudicate.pending_conflicts(db, cfg.account, cfg.imap_mailbox)
        return templates.TemplateResponse(
            request,
            "conflicts.html",
            {
                "cfg": cfg,
                "pending": pending,
                "decisions": [dict(r) for r in db.list_adjudications(50)],
                "flash": request.query_params.get("flash", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get("/conflicts/{message_id}", response_class=HTMLResponse)
    def conflict_detail(request: Request, message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        view = adjudicate.conflict_view(db, row, cfg.account, cfg.imap_mailbox)
        if view["eligible"]:
            report_no = view["report_no"]
            view["history"] = [
                dict(r)
                for r in db.list_adjudications(200)
                if r["report_no"] == report_no
            ]
        else:
            view["history"] = []
        return templates.TemplateResponse(
            request,
            "conflict.html",
            {
                "cfg": cfg,
                "view": view,
                "flash": request.query_params.get("flash", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.post("/actions/adjudicate")
    async def action_adjudicate(request: Request):
        # Plain urlencoded form: parsed by hand so the service keeps a minimal
        # dependency set (Starlette needs python-multipart for request.form()).
        body = (await request.body()).decode("utf-8")
        form = {
            key: values[0]
            for key, values in parse_qs(body, keep_blank_values=True).items()
        }
        try:
            message_id = int(form.get("message_id") or 0)
        except ValueError:
            message_id = 0
        try:
            result = await asyncio.to_thread(
                _confirm_from_form, db, store, cfg, form, message_id
            )
        except adjudicate.AdjudicationError as exc:
            # Includes StaleDecision when another instance adjudicated first.
            return RedirectResponse(
                f"/conflicts/{message_id}?flash=&error={_q(str(exc))}", status_code=303
            )
        except Exception as exc:  # never leave a half-written decision
            log.exception("adjudication failed")
            return RedirectResponse(
                f"/conflicts/{message_id}?flash=&error={_q(f'裁决失败，当前报告未改变：{exc}')}",
                status_code=303,
            )
        if result["status"] == "created":
            summary = f"已确认 {form.get('report_no')} 第 {result['version']} 版为当前有效报告"
        elif result["status"] == "idempotent":
            summary = (
                f"该候选已于此前裁决（第 {result['version']} 版），本次未新增版本"
            )
        else:
            summary = f"候选已与当前第 {result['version']} 版一致，无需变更"
        return RedirectResponse(f"/conflicts?flash={_q(summary)}", status_code=303)

    # -------------------------------------------------------------------- API
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "instance": cfg.instance_id}

    @app.get("/api/status")
    async def api_status():
        state = db.get_state(cfg.account, cfg.imap_mailbox)
        probe: dict[str, Any]
        try:
            probe = await asyncio.to_thread(collector.probe)
        except Exception as exc:
            probe = {"ok": False, "error": str(exc)}
        return {
            "instance": cfg.instance_id,
            "account": cfg.account,
            "mailbox": cfg.imap_mailbox,
            "stats": db.stats(),
            "mailbox_state": _row_to_dict(state),
            "imap": probe,
            "active_scan": _row_to_dict(db.active_scan()),
        }

    @app.post("/api/scan")
    async def api_scan():
        try:
            result = await asyncio.to_thread(run_scan, "api", True)
        except ScanBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"扫描失败：{exc}") from exc
        # A stale/interrupted scan is not a failure of the request: it reports
        # what was committed, plus why it stopped.
        return result

    @app.post("/api/retry-marks")
    async def api_retry_marks():
        try:
            marked = await asyncio.to_thread(collector.mark_pending_public)
        except ScanBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"补标记失败：{exc}") from exc
        return {"marked": marked}

    @app.get("/api/messages")
    def api_messages(limit: int = 100, mark_state: str | None = None):
        return [_message_view(db, row) for row in db.list_messages(limit, mark_state)]

    @app.get("/api/conflicts")
    def api_conflicts():
        return adjudicate.pending_conflicts(db, cfg.account, cfg.imap_mailbox)

    @app.get("/api/conflicts/{message_id}")
    def api_conflict(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        return adjudicate.conflict_view(db, row, cfg.account, cfg.imap_mailbox)

    @app.get("/conflicts/{message_id}/candidate")
    def download_candidate(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        view = adjudicate.conflict_view(db, row, cfg.account, cfg.imap_mailbox)
        if not view["eligible"]:
            raise HTTPException(status_code=404, detail=view["ineligible_reason"])
        candidate = view["candidate"]
        path = store.absolute(candidate["attachment_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="候选附件文件缺失")
        return FileResponse(
            path,
            media_type="text/csv; charset=utf-8",
            filename=candidate["attachment_name"],
        )

    @app.post("/api/conflicts/{message_id}/confirm")
    def api_confirm(message_id: int, payload: ConfirmRequest):
        try:
            return adjudicate.confirm(
                db,
                store,
                account=cfg.account,
                mailbox=cfg.imap_mailbox,
                message_id=message_id,
                report_no=payload.report_no,
                candidate_sha256=payload.candidate_sha256,
                expected_current_version=payload.expected_current_version,
                expected_current_sha256=payload.expected_current_sha256,
                instance_id=cfg.instance_id,
                page_token=payload.page_token,
            )
        except adjudicate.StaleDecision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except adjudicate.NotEligible as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/report-versions/{version_id}/attachment")
    def download_version(version_id: int):
        row = db.report_version(version_id)
        if row is None:
            raise HTTPException(status_code=404, detail="version not found")
        path = store.absolute(row["attachment_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="附件文件缺失")
        return FileResponse(
            path,
            media_type="text/csv; charset=utf-8",
            filename=row["attachment_name"],
        )

    @app.get("/api/adjudications/{adjudication_id}/receipt")
    def download_adjudication_receipt(adjudication_id: int):
        row = db.get_adjudication(adjudication_id)
        if row is None:
            raise HTTPException(status_code=404, detail="adjudication not found")
        if not row["receipt_path"]:
            raise HTTPException(status_code=404, detail="回执尚未写入")
        path = store.absolute(row["receipt_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="回执文件缺失")
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"adjudication-{adjudication_id}.json",
        )

    @app.get("/api/messages/{message_id}")
    def api_message(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        return _message_view(db, row)

    @app.get("/api/messages/{message_id}/original")
    def download_original(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        path = store.absolute(row["original_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="原件文件缺失")
        return FileResponse(
            path,
            media_type="message/rfc822",
            filename=f"{row['raw_sha256'][:16]}.eml",
        )

    @app.get("/api/messages/{message_id}/receipt")
    def download_receipt(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        path = store.absolute(row["receipt_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="回执文件缺失")
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"receipt-{row['uidvalidity']}-{row['uid']}.json",
        )

    @app.get("/api/reports/{report_id}/attachment")
    def download_attachment(report_id: int):
        row = db.get_report(report_id)
        if row is None:
            raise HTTPException(status_code=404, detail="report not found")
        path = store.absolute(row["attachment_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="附件文件缺失")
        return FileResponse(
            path,
            media_type="text/csv; charset=utf-8",
            filename=row["attachment_name"],
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app


def _q(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


app = create_app()
