from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.request_size_limit import RequestSizeLimitMiddleware
from app.webhook_limits import WEBHOOK_BODY_LIMITS
from app.routers.reconciliation import router as reconciliation_router
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
from app.routers import bank_attachments
from app.routers import bank_extraction_benchmark
from app.routers import bank_extraction_admin
from app.routers import invoice_extraction_admin
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
from app.routers import customer_statements
from app.routers import supplier_payment_runs
from app.routers import receipt_inbox
from app.routers import recurring_transactions
from app.routers import budgets
from app.routers import review_queue
from app.routers import notifications
from app.routers import suspense_clearing
from app.routers import global_search

app = FastAPI(
    title="APPayPal Backend",
    version="0.1.0",
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
app.add_middleware(RequestSizeLimitMiddleware, limits=WEBHOOK_BODY_LIMITS)

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
app.include_router(bank_attachments.router)
app.include_router(bank_extraction_benchmark.router)
app.include_router(bank_extraction_admin.router)
app.include_router(invoice_extraction_admin.router)
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
app.include_router(customer_statements.router)
app.include_router(supplier_payment_runs.router)
app.include_router(receipt_inbox.router)
app.include_router(recurring_transactions.router)
app.include_router(budgets.router)
app.include_router(review_queue.router)
app.include_router(notifications.router)
app.include_router(suspense_clearing.router)
app.include_router(global_search.router)


@app.get("/")
def root():
    return {"message": "APPayPal backend is running"}
