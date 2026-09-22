from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
import os

from app.services.market_monitor.router import router as market_monitor_router
from app.services.mfi_drafter.router import router as mfi_drafter_router
from app.services.seasonal_outlook.router import router as seasonal_outlook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logging.getLogger().setLevel(logging.INFO)

_fastapi_root_path = (os.getenv("FASTAPI_ROOT_PATH") or "").strip()

app = FastAPI(
    title="VAM LLM Report Drafting API",
    description="Backend API for WFP market and food security report drafting",
    version="1.0.0",
    root_path=_fastapi_root_path or "",
)

def _get_cors_allow_origins() -> list[str]:
    raw = (os.getenv("CORS_ALLOW_ORIGINS") or os.getenv("ALLOW_ORIGINS") or "").strip()
    if not raw:
        return ["*"]
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]

_cors_allow_origins = _get_cors_allow_origins()
_cors_allow_credentials = _cors_allow_origins != ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🚦 Smistamento ai servizi
app.include_router(market_monitor_router, prefix="/market-monitor", tags=["Market Monitor"])
app.include_router(mfi_drafter_router, prefix="/mfi-drafter", tags=["MFI Drafter"])
app.include_router(seasonal_outlook_router, prefix="/seasonal-outlook", tags=["Seasonal Outlook"])

@app.get("/")
def root():
    return {
        "status": "ok",
        "services": [
            {"id": "market-monitor", "name": "Market Monitor Generator", "endpoint": "/market-monitor/generate"},
            {"id": "mfi-drafter", "name": "MFI Report Generator", "endpoint": "/mfi-drafter/generate"},
            {"id": "seasonal-outlook", "name": "Seasonal Outlook Drafter", "endpoint": "/seasonal-outlook/info"}
        ]
    }

@app.get("/health")
def health():
    return {"status": "healthy"}
