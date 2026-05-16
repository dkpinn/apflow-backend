from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.reconciliation import router as reconciliation_router
from app.db.supabase_client import get_supabase_client
from app.routers import invoices
from app.routers import suppliers

app = FastAPI(
    title="APFlow Backend",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reconciliation_router)
app.include_router(invoices.router)
app.include_router(suppliers.router)


@app.get("/")
def root():
    return {"message": "APFlow backend is running"}


@app.get("/test-db")
def test_db():
    supabase = get_supabase_client()
    data = supabase.table("suppliers").select("*").limit(2).execute()
    return data.data
