from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.reconciliation import router as reconciliation_router

app = FastAPI(
    title="APFlow Backend",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reconciliation_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "APFlow backend is running"}