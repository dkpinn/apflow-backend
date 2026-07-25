"""Dedicated, database-coordinated invoice extraction worker.

Run with: ``python -m app.workers.invoice_worker``
Multiple replicas are safe: PostgreSQL claims jobs with ``SKIP LOCKED``.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from threading import Event

from app.db.supabase_client import get_supabase_client
from app.services.invoice_extraction_service import process_next_queued_invoice_job
from app.services.recurring_transactions import generate_due_drafts

logger = logging.getLogger("apflow.invoice_worker")


def build_worker_id() -> str:
    configured = os.getenv("INVOICE_WORKER_ID", "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def maintain_queue(db) -> None:
    result = db.rpc("maintain_invoice_processing_queue").execute()
    logger.info("Invoice queue maintenance: %s", result.data)
    try:
        generate_due_drafts(db)
    except Exception:
        logger.exception("Recurring draft generation failed")


def run(*, stop_event: Event | None = None) -> None:
    stop = stop_event or Event()
    worker_id = build_worker_id()
    poll_seconds = max(0.1, float(os.getenv("INVOICE_WORKER_POLL_SECONDS", "2")))
    maintenance_seconds = max(10.0, float(os.getenv("INVOICE_WORKER_MAINTENANCE_SECONDS", "60")))
    next_maintenance = 0.0
    db = get_supabase_client()

    logger.info("Invoice worker %s started", worker_id)
    while not stop.is_set():
        now = time.monotonic()
        if now >= next_maintenance:
            try:
                maintain_queue(db)
            except Exception:
                logger.exception("Invoice queue maintenance failed")
            next_maintenance = now + maintenance_seconds

        try:
            result = process_next_queued_invoice_job(worker_id=worker_id)
        except Exception:
            logger.exception("Invoice worker polling failed")
            stop.wait(poll_seconds)
            continue

        if result.get("status") == "empty":
            stop.wait(poll_seconds)
        elif result.get("status") == "failed":
            logger.error("Invoice job failed: %s", result)

    logger.info("Invoice worker %s stopped", worker_id)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop = Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    run(stop_event=stop)


if __name__ == "__main__":
    main()
