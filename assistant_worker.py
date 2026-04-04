from __future__ import annotations

import asyncio
import logging
import socket

import db
from settings import get_settings

log = logging.getLogger("assistant_worker")


async def process_assistant_job(conn, job: dict, worker_name: str) -> None:
    job_id = int(job.get("id") or 0)
    tg_user_id = int(job.get("tg_user_id") or 0)
    kind = str(job.get("kind") or "")
    debug_id = str(job.get("debug_id") or "")
    log.info(
        "assistant worker claimed job_id=%s tg_user_id=%s kind=%s debug_id=%s",
        job_id,
        tg_user_id,
        kind,
        debug_id,
    )
    await db.mark_assistant_job_processing(conn, job_id, worker_name)
    await db.mark_assistant_job_failed(
        conn,
        job_id,
        "NOT_IMPLEMENTED_PHASE1",
        "Worker stub in phase 1: processing pipeline will be implemented in phase 2.",
    )


async def run_worker() -> None:
    settings = get_settings()
    worker_name = f"assistant-worker:{socket.gethostname()}"
    poll_interval_sec = 2.0
    idle_log_every = 30
    idle_ticks = 0

    log.info("assistant worker starting worker_name=%s db_path=%s", worker_name, settings.DB_PATH)

    while True:
        conn = await db.connect(settings.DB_PATH)
        try:
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
