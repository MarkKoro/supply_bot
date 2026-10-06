from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import socket
import time

import db
from nutrient_assistant_pipeline import NutrientAssistantPipelineError, run_nutrient_assistant_compute_pipeline
from settings import get_settings
from openai_client import OpenAIConfig, extract_lab_document_ai, explain_lab_history_ai
from pathlib import Path

log = logging.getLogger("assistant_worker")


async def _reserve_lab_ai_usage(tx, tg_user_id: int) -> None:
    month = db.utc_now_iso()[:7]
    used = await db.fetchone(tx, "SELECT used FROM lab_ai_monthly_usage WHERE tg_user_id=? AND month_utc=?", (tg_user_id, month))
    if used and int(used["used"]) >= 20:
        raise ValueError("Достигнут лимит: 20 обращений к ИИ в месяц. История и ручной ввод остаются доступны")
    await tx.execute("""INSERT INTO lab_ai_monthly_usage(tg_user_id,month_utc,used) VALUES(?,?,1)
        ON CONFLICT(tg_user_id,month_utc) DO UPDATE SET used=used+1""", (tg_user_id, month))


async def process_lab_explanation_job(conn, job: dict, payload: dict) -> None:
    tg_user_id = int(job["tg_user_id"])
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise ValueError("OpenAI пока не подключён")
    async with db.transaction(conn) as tx:
        if not await db.assistant_lease_valid(tx, job):
            return
        uid = await db.get_or_create_user(tx, tg_user_id)
        row = await db.fetchone(tx, "SELECT * FROM lab_explanations WHERE user_id=? AND job_id=?", (uid, int(job["id"])))
        if not row:
            await db.mark_assistant_job_done(tx, int(job["id"]), 0, lease=job)
            return
        if int(job.get("attempts") or 0) > 1:
            raise ValueError("Пояснение прервалось. Запустите его повторно, если оно ещё нужно")
        await _reserve_lab_ai_usage(tx, tg_user_id)
    response = await asyncio.to_thread(explain_lab_history_ai, OpenAIConfig(settings.OPENAI_API_KEY, settings.OPENAI_LAB_PARSE_MODEL), payload["snapshot"])
    async with db.transaction(conn) as tx:
        if not await db.assistant_lease_valid(tx, job):
            return
        uid = await db.get_or_create_user(tx, tg_user_id)
        await tx.execute("""UPDATE lab_explanations SET status='done',response_json=?,updated_at_utc=?,error_text=''
            WHERE user_id=? AND job_id=? AND input_hash=?""", (json.dumps(response, ensure_ascii=False), db.utc_now_iso(), uid, int(job["id"]), payload["input_hash"]))
        await db.mark_assistant_job_done(tx, int(job["id"]), 0, lease=job)


async def process_lab_document_job(conn, job: dict, payload: dict) -> None:
    tg_user_id, report_id = int(job["tg_user_id"]), int(payload["report_id"])
    report = await db.get_lab_report_by_id(conn, tg_user_id, report_id)
    if not report or report["parse_status"] != "queued" or int(report["parse_job_id"]) != int(job["id"]):
        await db.mark_assistant_job_done(conn, int(job["id"]), 0, lease=job)
        return
    database = await db.fetchone(conn, "PRAGMA database_list")
    root = (Path(database["file"]).resolve().parent / "uploads" / "labs" / str(tg_user_id)).resolve()
    path = Path(report["source_file_path"]).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError("Исходный документ не найден. Загрузите его снова")
    document = payload.get("document") or {}
    extracted = {"results": document.get("results") or [], "report_date": "", "lab_name": ""}
    message = "Проверьте значения, единицы, дату и референсы перед сохранением"
    if payload.get("allow_ai"):
        settings = get_settings()
        if not settings.OPENAI_API_KEY:
            message = "ИИ-распознавание пока не настроено. Проверьте результаты или внесите их вручную"
        else:
            async with db.transaction(conn) as tx:
                if not await db.assistant_lease_valid(tx, job):
                    return
                current = await db.get_lab_report_by_id(tx, tg_user_id, report_id)
                if int(current.get("ai_attempts") or 0) and int(job.get("attempts") or 0) > 1:
                    # A crash after an upstream call must not silently spend again.
                    raise ValueError("Распознавание прервалось. Проверьте документ или запустите его повторно")
                uid = await db.get_or_create_user(tx, tg_user_id)
                await _reserve_lab_ai_usage(tx, tg_user_id)
                await tx.execute("UPDATE lab_reports SET ai_attempts=ai_attempts+1 WHERE id=? AND user_id=?", (report_id, uid))
            extracted = await asyncio.to_thread(extract_lab_document_ai, OpenAIConfig(settings.OPENAI_API_KEY, settings.OPENAI_LAB_PARSE_MODEL), str(path), document)
    async with db.transaction(conn) as tx:
        if not await db.assistant_lease_valid(tx, job):
            return
        current = await db.get_lab_report_by_id(tx, tg_user_id, report_id)
        if not current or current["parse_status"] != "queued" or int(current["parse_job_id"]) != int(job["id"]):
            await db.mark_assistant_job_done(tx, int(job["id"]), 0, lease=job)
            return
        results = list(extracted.get("results") or [])
        for item in results:
            item["measured_at"] = db.validate_lab_date(item.get("measured_at"))
        try:
            extracted_date = db.validate_lab_date(extracted.get("report_date"))
        except ValueError:
            extracted_date = ""
        previous_metadata = json.loads(current.get("parse_metadata_json") or "{}")
        if not previous_metadata.get("confirmed"):
            await db.replace_lab_results(tx, tg_user_id, report_id, results)
        uid = await db.get_or_create_user(tx, tg_user_id)
        await tx.execute("""UPDATE lab_reports SET parse_status='needs_manual_review',parse_notes=?,
            report_date=CASE WHEN report_date='' THEN ? ELSE report_date END,
            lab_name=CASE WHEN lab_name='' THEN ? ELSE lab_name END,parse_metadata_json=?,updated_at_utc=?
            WHERE id=? AND user_id=?""", (message, extracted_date, str(extracted.get("lab_name") or "")[:200],
            json.dumps({"model": get_settings().OPENAI_LAB_PARSE_MODEL if payload.get("allow_ai") else "local",
                        "usage": extracted.get("usage") or {}, "confirmed": bool(previous_metadata.get("confirmed")),
                        "pending_results": results}, ensure_ascii=False), db.utc_now_iso(), report_id, uid))
        await db.mark_assistant_job_done(tx, int(job["id"]), 0, json.dumps({"report_id": report_id}), lease=job)


async def _keep_assistant_lease(db_path: str, job: dict, interval_seconds: float = 20) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        heartbeat_conn = await db.connect(db_path)
        try:
            if not await db.touch_assistant_job(heartbeat_conn, int(job["id"]), lease=job):
                return
        finally:
            await heartbeat_conn.close()


async def process_assistant_job(conn, job: dict, worker_name: str) -> None:
    started_ts = time.monotonic()
    job_id = int(job.get("id") or 0)
    tg_user_id = int(job.get("tg_user_id") or 0)
    kind = str(job.get("kind") or "")
    debug_id = str(job.get("debug_id") or "")
    stage = "claim"
    heartbeat = None
    log.info("assistant worker claimed job_id=%s tg_user_id=%s kind=%s debug_id=%s", job_id, tg_user_id, kind, debug_id)
    try:
        stage = "mark_processing"
        if not await db.assistant_lease_valid(conn, job):
            return
        if kind not in {"nutrient_compute", "lab_parse", "lab_explain"}:
            await db.mark_assistant_job_failed(conn, job_id, "UNSUPPORTED_JOB_KIND", f"Unsupported kind: {kind}", lease=job)
            return
        database = await db.fetchone(conn, "PRAGMA database_list")
        if database and database["file"]:
            heartbeat = asyncio.create_task(_keep_assistant_lease(str(database["file"]), job))
        stage = "parse_request_json"
        request_json_raw = str(job.get("request_json") or "{}")
        request_payload = json.loads(request_json_raw)
        if kind in {"lab_parse", "lab_explain"}:
            try:
                if kind == "lab_parse":
                    await process_lab_document_job(conn, job, request_payload)
                else:
                    await process_lab_explanation_job(conn, job, request_payload)
            except Exception as exc:
                await conn.rollback()
                async with db.transaction(conn) as tx:
                    if await db.assistant_lease_valid(tx, job):
                        safe = str(exc) if isinstance(exc, ValueError) else "ИИ сейчас недоступен. Сохранённая история остаётся доступна"
                        if kind == "lab_explain":
                            uid = await db.get_or_create_user(tx, tg_user_id)
                            await tx.execute("UPDATE lab_explanations SET status='failed',error_text=?,updated_at_utc=? WHERE user_id=? AND job_id=?", (safe[:400], db.utc_now_iso(), uid, job_id))
                        report = await db.get_lab_report_by_id(tx, tg_user_id, int(request_payload.get("report_id") or 0))
                        if report and report["parse_status"] == "queued" and int(report["parse_job_id"]) == job_id:
                            safe = str(exc) if isinstance(exc, ValueError) else "Распознавание недоступно. Можно проверить документ вручную или повторить позже"
                            await tx.execute("UPDATE lab_reports SET parse_status='needs_manual_review',parse_notes=?,updated_at_utc=? WHERE id=?", (safe[:400], db.utc_now_iso(), int(report["id"])))
                        await db.mark_assistant_job_failed(tx, job_id, "LAB_PARSE_ERROR", type(exc).__name__, lease=job)
            return
        replace_run_id = int(request_payload.get("replace_run_id") or 0) or None

        stage = "run_compute_pipeline"
        await run_nutrient_assistant_compute_pipeline(
            conn,
            tg_user_id=tg_user_id,
            assistant_profile=dict(request_payload.get("assistant_profile") or {}),
            labs=list(request_payload.get("labs") or []),
            assistant_context=dict(request_payload.get("assistant_context") or {}),
            debug_id=debug_id,
            job_lease=job,
            replace_run_id=replace_run_id,
        )
        elapsed_ms = int((time.monotonic() - started_ts) * 1000)
        log.info(
            "assistant worker success job_id=%s tg_user_id=%s debug_id=%s stage=%s elapsed_ms=%s",
            job_id,
            tg_user_id,
            debug_id,
            stage,
            elapsed_ms,
        )
    except NutrientAssistantPipelineError as exc:
        await conn.rollback()
        await db.mark_assistant_job_failed(conn, job_id, exc.code, f"{exc.stage}: {exc.message}".strip(": "), lease=job)
        elapsed_ms = int((time.monotonic() - started_ts) * 1000)
        log.warning(
            "assistant worker failed job_id=%s tg_user_id=%s debug_id=%s stage=%s elapsed_ms=%s code=%s",
            job_id,
            tg_user_id,
            debug_id,
            exc.stage,
            elapsed_ms,
            exc.code,
        )
    except Exception as exc:
        await conn.rollback()
        await db.mark_assistant_job_failed(conn, job_id, "ASSISTANT_COMPUTE_ERROR", f"{stage}: {type(exc).__name__}", lease=job)
        elapsed_ms = int((time.monotonic() - started_ts) * 1000)
        log.exception(
            "assistant worker failed job_id=%s tg_user_id=%s debug_id=%s stage=%s elapsed_ms=%s",
            job_id,
            tg_user_id,
            debug_id,
            stage,
            elapsed_ms,
        )
    finally:
        if heartbeat:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat


async def run_worker() -> None:
    settings = get_settings()
    worker_name = f"assistant-worker:{socket.gethostname()}"
    poll_interval_sec = 2.5
    idle_log_every = 48
    idle_ticks = 0
    stale_check_every = 24

    log.info("assistant worker starting worker_name=%s db_path=%s", worker_name, settings.DB_PATH)

    while True:
        conn = await db.connect(settings.DB_PATH)
        try:
            await db.process_lab_file_cleanup(conn)
            if idle_ticks % stale_check_every == 0:
                recovered = await db.recover_stale_assistant_jobs(conn, stale_after_seconds=600)
                recovered += await db.recover_stale_assistant_jobs(conn, stale_after_seconds=600, kind="lab_parse")
                recovered += await db.recover_stale_assistant_jobs(conn, stale_after_seconds=600, kind="lab_explain")
                if recovered:
                    log.warning("assistant worker stale recovered count=%s job_ids=%s", len(recovered), recovered[:10])
            job = await db.claim_next_assistant_job(conn, worker_name)
            if not job:
                idle_ticks += 1
                if idle_ticks % idle_log_every == 0:
                    log.info("assistant worker idle polling interval=%ss", poll_interval_sec)
                await asyncio.sleep(poll_interval_sec)
                continue

            idle_ticks = 0
            await process_assistant_job(conn, job, worker_name)
        except Exception:
            log.exception("assistant worker loop error")
            await asyncio.sleep(poll_interval_sec)
        finally:
            await conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(run_worker())
