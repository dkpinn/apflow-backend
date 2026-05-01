from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.reconciliation import router as reconciliation_router
from app.db.supabase_client import get_supabase_client
from app.routers import invoices

app = FastAPI(
    title="APFlow Backend",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reconciliation_router)
app.include_router(invoices.router)


@app.get("/")
def root():
    return {"message": "APFlow backend is running"}


@app.get("/test-db")
def test_db():
    supabase = get_supabase_client()
    data = supabase.table("suppliers").select("*").limit(2).execute()
    return data.data