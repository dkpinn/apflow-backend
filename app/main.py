from contextlib import asynccontextmanager
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.reconciliation import router as reconciliation_router
from app.db.supabase_client import get_supabase_client
from app.routers import invoices
from app.routers import invoices_gl
from app.routers import invoices_queue
from app.routers import invoices_review
from app.routers import organisations
from app.routers import suppliers
from app.routers import supplier_kyc
from app.routers import supplier_branches
from app.routers import supplier_allocation_rules
from app.routers import themes
from app.routers import consolidation
from app.routers import webhooks
from app.routers import channels
from app.routers import admin_integrations
from app.routers import admin_accounts
from app.routers import integrations
from app.routers import bank
from app.routers import bank_journals
from app.routers import bank_rules
from app.routers import bank_uploads
from app.routers import bank_lines
from app.routers import bank_extraction_benchmark
from app.routers import bank_extraction_admin
from app.routers import reports
from app.routers import asset_types
from app.routers import document_autofill
from app.routers import customers
from app.routers import sales_invoices
from app.routers import sales_invoices_actions
from app.routers import customer_receipts
from app.routers import inventory
from app.routers import finance_leases
from app.routers import command_centre
from app.routers import go_live_checklist
from app.routers import accounting_periods
from app.routers import opening_balances
from app.routers import audit_trail
from app.routers import customer_collections
from app.routers import supplier_payment_runs

logger = logging.getLogger("apflow.sweep")


def _rescue_pending_invoices(supabase_client) -> None:
    """
    Startup + periodic sweep:
    1.  Reset invoices stuck in 'processing' for > 10 min back to 'pending'
    1b. Reset 'completed' invoices that have no extracted record back to 'pending'
    2.  Queue any 'pending' invoices that have no active processing job
    3.  Drain the extraction queue per-org (sequential, lock-guarded inside worker)
    """
    from app.services.document_jobs import create_processing_job, safe_update_invoice_raw_status
    from app.services.audit_log import log_invoice_event

    sweep_start = datetime.now(timezone.utc)
    logger.info("=== Sweep start ===")

    stale_reset    = 0
    orphans_reset  = 0
    orphans_failed = 0
    newly_queued   = 0

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
            logger.info("Resetting stale invoice %s (processing → pending)", row["id"])
            safe_update_invoice_raw_status(supabase_client, invoice_raw_id=row["id"], parse_status="pending")
            stale_reset += 1
    except Exception:
        logger.exception("Error resetting stale processing items")

    # Step 1b — detect 'queued' raw invoices with no active job (orphaned after server restart/job failure)
    try:
        queued_raw = (
            supabase_client.table("invoices_raw")
            .select("id")
            .eq("parse_status", "queued")
            .execute()
        ).data or []
        if queued_raw:
            queued_ids = [r["id"] for r in queued_raw]
            active_job_raw_ids = {
                row["invoice_raw_id"]
                for row in (
                    supabase_client.table("document_processing_jobs")
                    .select("invoice_raw_id")
                    .in_("invoice_raw_id", queued_ids)
                    .in_("status", ["queued", "processing"])
                    .execute()
                ).data or []
            }
            for row in queued_raw:
                if row["id"] not in active_job_raw_ids:
                    logger.info("Orphaned queued invoice %s (no active job) → resetting to pending", row["id"])
                    safe_update_invoice_raw_status(supabase_client, invoice_raw_id=row["id"], parse_status="pending")
                    stale_reset += 1
    except Exception:
        logger.exception("Error resetting orphaned queued items")

    # Step 1c — detect 'completed' raw invoices with no extracted record and reset to 'pending'
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
                    logger.debug("Could not count past jobs for invoice %s; assuming 0", raw_id)
                    past_jobs = 0

                if past_jobs >= MAX_SWEEP_ATTEMPTS:
                    logger.warning(
                        "Giving up on orphaned invoice %s after %d attempts → marking failed",
                        raw_id, past_jobs,
                    )
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
                    orphans_failed += 1
                    continue

                logger.info(
                    "Orphaned invoice %s (completed, no extracted record, attempt %d/%d) → resetting to pending",
                    raw_id, past_jobs + 1, MAX_SWEEP_ATTEMPTS,
                )
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
                orphans_reset += 1
    except Exception:
        logger.exception("Error checking for orphaned completed invoices")

    # Step 2 — find 'pending' items with no active job and queue them.
    # Collect orgs_to_drain here; do NOT return early — step 3 must always run.
    orgs_to_drain: set[str] = set()

    try:
        pending_rows = (
            supabase_client.table("invoices_raw")
            .select("id, organisation_id")
            .eq("parse_status", "pending")
            .execute()
        ).data or []

        if pending_rows:
            pending_ids = [r["id"] for r in pending_rows]

            # Collect orgs now, before we filter to_queue
            for r in pending_rows:
                if r.get("organisation_id"):
                    orgs_to_drain.add(r["organisation_id"])

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
                    logger.info("Queued invoice %s", raw_id)
                    newly_queued += 1
                except Exception:
                    logger.exception("Failed to queue invoice %s", raw_id)

    except Exception:
        logger.exception("Error finding pending items")
        # orgs_to_drain may be partial; fall through to step 3 anyway

    # Step 3 — augment orgs_to_drain with any org that already has queued jobs
    # (covers the case where pending_rows was empty but leftover jobs exist from
    # a prior sweep cycle or an API-triggered queue call)
    try:
        pre_queued = (
            supabase_client.table("document_processing_jobs")
            .select("organisation_id")
            .eq("status", "queued")
            .limit(500)
            .execute()
        ).data or []
        for row in pre_queued:
            if row.get("organisation_id"):
                orgs_to_drain.add(row["organisation_id"])
    except Exception:
        logger.exception("Error fetching pre-queued orgs")

    # Step 3 — drain per-org (sequential; EXTRACT_WORKER_LOCK guards inside)
    for org_id in orgs_to_drain:
        try:
            invoices.run_extract_worker_until_empty(organisation_id=org_id)
        except Exception:
            logger.exception("Worker error for org %s", org_id)

    elapsed = (datetime.now(timezone.utc) - sweep_start).total_seconds()
    logger.info(
        "Done in %.2fs — stale=%d orphans_reset=%d orphans_failed=%d queued=%d drain_orgs=%d",
        elapsed, stale_reset, orphans_reset, orphans_failed, newly_queued, len(orgs_to_drain),
    )


def _background_sweep_thread() -> None:
    """Daemon thread: 5s startup delay then sweep every 60s."""
    time.sleep(5)

    supabase_client = None
    while True:
        if supabase_client is None:
            try:
                supabase_client = get_supabase_client()
            except Exception:
                logger.exception("Cannot connect to Supabase, will retry next cycle")
                time.sleep(60)
                continue

        try:
            _rescue_pending_invoices(supabase_client)
        except Exception:
            logger.exception("Unhandled sweep thread error — resetting Supabase client")
            # Reset client so the next cycle gets a fresh connection
            supabase_client = None

        time.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_background_sweep_thread, daemon=True, name="invoice-sweep")
    t.start()
    logger.info("Background invoice rescue thread started")
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
app.include_router(invoices_gl.router)
app.include_router(invoices_queue.router)
app.include_router(invoices_review.router)
app.include_router(organisations.router)
app.include_router(suppliers.router)
app.include_router(supplier_kyc.router)
app.include_router(supplier_branches.router)
app.include_router(supplier_allocation_rules.router)
app.include_router(themes.router)
app.include_router(consolidation.router)
app.include_router(webhooks.router)
app.include_router(channels.router)
app.include_router(admin_integrations.router)
app.include_router(admin_accounts.router)
app.include_router(integrations.router)
app.include_router(bank.router)
app.include_router(bank_journals.router)
app.include_router(bank_rules.router)
app.include_router(bank_uploads.router)
app.include_router(bank_lines.router)
app.include_router(bank_extraction_benchmark.router)
app.include_router(bank_extraction_admin.router)
app.include_router(reports.router)
app.include_router(asset_types.router)
app.include_router(document_autofill.router)
app.include_router(customers.router)
app.include_router(sales_invoices.router)
app.include_router(sales_invoices_actions.router)
app.include_router(customer_receipts.router)
app.include_router(inventory.router)
app.include_router(finance_leases.router)
app.include_router(command_centre.router)
app.include_router(go_live_checklist.router)
app.include_router(accounting_periods.router)
app.include_router(opening_balances.router)
app.include_router(audit_trail.router)
app.include_router(customer_collections.router)
app.include_router(supplier_payment_runs.router)


@app.get("/")
def root():
    return {"message": "APPayPal backend is running"}
