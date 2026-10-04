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

log = logging.getLogger("assistant_worker")


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
        if kind != "nutrient_compute":
            await db.mark_assistant_job_failed(conn, job_id, "UNSUPPORTED_JOB_KIND", f"Unsupported kind: {kind}", lease=job)
            return
        database = await db.fetchone(conn, "PRAGMA database_list")
        if database and database["file"]:
            heartbeat = asyncio.create_task(_keep_assistant_lease(str(database["file"]), job))
        stage = "parse_request_json"
        request_json_raw = str(job.get("request_json") or "{}")
        request_payload = json.loads(request_json_raw)
        replace_run_id = int(request_payload.get("replace_run_id") or 0) or None

        stage = "run_compute_pipeline"
        result = await run_nutrient_assistant_compute_pipeline(
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
            if idle_ticks % stale_check_every == 0:
                recovered = await db.recover_stale_assistant_jobs(conn, stale_after_seconds=600)
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
