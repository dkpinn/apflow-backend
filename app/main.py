from contextlib import asynccontextmanager
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.reconciliation import router as reconciliation_router
from app.db.supabase_client import get_supabase_client
from app.routers import invoices
from app.routers import suppliers
from app.routers import themes


def _rescue_pending_invoices() -> None:
    """
    Startup + periodic sweep:
    1.  Reset invoices stuck in 'processing' for > 10 min back to 'pending'
    1b. Reset 'completed' invoices that have no extracted record back to 'pending'
    2.  Queue any 'pending' invoices that have no active processing job
    3.  Run the extraction worker to drain the queue
    """
    from app.services.document_jobs import create_processing_job, safe_update_invoice_raw_status
    from app.services.audit_log import log_invoice_event

    try:
        supabase_client = get_supabase_client()
    except Exception as exc:
        print(f"SWEEP: Cannot connect to Supabase: {exc}")
        return

    # Step 1 — reset stale 'processing' items (stuck > 10 min) back to 'pending'
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    try:
        stale = (
            supabase_client.table("invoices_raw")
            .select("id")
            .eq("parse_status", "processing")
            .lt("updated_at", cutoff)
            .execute()
        ).data or []
        for row in stale:
            print(f"SWEEP: Resetting stale invoice {row['id']} (processing → pending)")
            safe_update_invoice_raw_status(supabase_client, invoice_raw_id=row["id"], parse_status="pending")
    except Exception as exc:
        print(f"SWEEP: Error resetting stale processing items: {exc}")

    # Step 1b — detect 'completed' raw invoices with no extracted record and reset to 'pending'
    try:
        completed_rows = (
            supabase_client.table("invoices_raw")
            .select("id, organisation_id")
            .eq("parse_status", "completed")
            .execute()
        ).data or []

        if completed_rows:
            completed_ids = [r["id"] for r in completed_rows]

            extracted_raw_ids = {
                row["invoice_raw_id"]
                for row in (
                    supabase_client.table("invoices_extracted")
                    .select("invoice_raw_id")
                    .in_("invoice_raw_id", completed_ids)
                    .execute()
                ).data or []
            }

            MAX_SWEEP_ATTEMPTS = 3
            orphans = [r for r in completed_rows if r["id"] not in extracted_raw_ids]
            for row in orphans:
                raw_id = row["id"]
                org_id = row.get("organisation_id")

                # Count past extraction attempts so we can give up after MAX_SWEEP_ATTEMPTS
                try:
                    past_jobs = (
                        supabase_client.table("document_processing_jobs")
                        .select("id", count="exact")
                        .eq("invoice_raw_id", raw_id)
                        .execute()
                    ).count or 0
                except Exception:
                    past_jobs = 0

                if past_jobs >= MAX_SWEEP_ATTEMPTS:
                    print(f"SWEEP: Giving up on orphaned invoice {raw_id} after {past_jobs} attempts → marking failed")
                    safe_update_invoice_raw_status(
                        supabase_client,
                        invoice_raw_id=raw_id,
                        parse_status="failed",
                    )
                    if org_id:
                        log_invoice_event(
                            supabase_client,
                            organisation_id=org_id,
                            invoice_raw_id=raw_id,
                            job_id=None,
                            event_type="job_failed",
                            stage="failed",
                            actor_type="system",
                            notes=f"Marked failed by SWEEP after {past_jobs} extraction attempts with no extracted record.",
                        )
                    continue

                print(f"SWEEP: Orphaned invoice {raw_id} (completed but no extracted record, attempt {past_jobs + 1}/{MAX_SWEEP_ATTEMPTS}) → resetting to pending")
                safe_update_invoice_raw_status(
                    supabase_client,
                    invoice_raw_id=raw_id,
                    parse_status="pending",
                )
                if org_id:
                    log_invoice_event(
                        supabase_client,
                        organisation_id=org_id,
                        invoice_raw_id=raw_id,
                        job_id=None,
                        event_type="queued_for_processing",
                        stage="pending",
                        actor_type="system",
                        notes=f"Reset to pending by SWEEP (attempt {past_jobs + 1}/{MAX_SWEEP_ATTEMPTS}): completed status but no extracted record found.",
                    )
    except Exception as exc:
        print(f"SWEEP: Error checking for orphaned completed invoices: {exc}")

    # Step 2 — find 'pending' items with no active job and queue them
    try:
        pending_rows = (
            supabase_client.table("invoices_raw")
            .select("id, organisation_id")
            .eq("parse_status", "pending")
            .execute()
        ).data or []

        if not pending_rows:
            return

        pending_ids = [r["id"] for r in pending_rows]

        # Two separate .eq() calls to avoid enum cast issues with .in_() on status column
        queued_jobs = (
            supabase_client.table("document_processing_jobs")
            .select("invoice_raw_id")
            .in_("invoice_raw_id", pending_ids)
            .eq("status", "queued")
            .execute()
        ).data or []
        processing_jobs = (
            supabase_client.table("document_processing_jobs")
            .select("invoice_raw_id")
            .in_("invoice_raw_id", pending_ids)
            .eq("status", "processing")
            .execute()
        ).data or []
        already_active = {j["invoice_raw_id"] for j in queued_jobs + processing_jobs}

        to_queue = [r for r in pending_rows if r["id"] not in already_active]
        if not to_queue:
            return

        for row in to_queue:
            raw_id = row["id"]
            org_id = row.get("organisation_id")
            if not org_id:
                continue
            try:
                job = create_processing_job(
                    supabase_client,
                    organisation_id=org_id,
                    invoice_raw_id=raw_id,
                )
                safe_update_invoice_raw_status(
                    supabase_client,
                    invoice_raw_id=raw_id,
                    parse_status="queued",
                    extra={"parse_started_at": None, "parse_completed_at": None},
                )
                log_invoice_event(
                    supabase_client,
                    organisation_id=org_id,
                    invoice_raw_id=raw_id,
                    job_id=job["id"],
                    event_type="queued_for_processing",
                    stage="queued",
                    actor_type="system",
                    notes="Queued by startup/periodic sweep.",
                )
                print(f"SWEEP: Queued invoice {raw_id}")
            except Exception as exc:
                print(f"SWEEP: Failed to queue {raw_id}: {exc}")

    except Exception as exc:
        print(f"SWEEP: Error finding pending items: {exc}")
        return

    # Step 3 — drain the extraction queue
    try:
        invoices.run_extract_worker_until_empty()
    except Exception as exc:
        print(f"SWEEP: Worker error: {exc}")


def _background_sweep_thread() -> None:
    """Daemon thread: 5s startup delay then sweep every 60s."""
    time.sleep(5)
    while True:
        try:
            _rescue_pending_invoices()
        except Exception as exc:
            print(f"SWEEP THREAD ERROR: {exc}")
        time.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_background_sweep_thread, daemon=True, name="invoice-sweep")
    t.start()
    print("SWEEP: Background invoice rescue thread started")
    yield


app = FastAPI(
    title="APPayPal Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reconciliation_router)
app.include_router(invoices.router)
app.include_router(suppliers.router)
app.include_router(themes.router)


@app.get("/")
def root():
    return {"message": "APPayPal backend is running"}


@app.get("/test-db")
def test_db():
    supabase = get_supabase_client()
    data = supabase.table("suppliers").select("*").limit(2).execute()
    return data.data
