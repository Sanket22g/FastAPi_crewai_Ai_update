from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pymongo import AsyncMongoClient
from dotenv import load_dotenv
from datetime import datetime, timezone
from pydantic import BaseModel
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import os
import certifi
import httpx
import logging
import pytz

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Track if a task is currently running to prevent duplicate runs
is_task_running = False

# -------------------------------------------------
# API Key Configuration (for Android app)
# -------------------------------------------------
API_KEY_HEADER = APIKeyHeader(name="X-API-Key")

async def verify_api_key(api_key: str = Depends(API_KEY_HEADER)):
    """Verify API key from Android app request header"""
    valid_api_key = os.getenv("API_KEY")
    if not valid_api_key:
        logger.error("API_KEY environment variable is not set!")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error"
        )
    if api_key != valid_api_key:
        logger.warning("Invalid API key attempt detected.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    return api_key

# -------------------------------------------------
# Pydantic Models
# -------------------------------------------------
class ReportResponse(BaseModel):
    _id: str
    topic_searched: str
    created_at: datetime

    class Config:
        from_attributes = True

# -------------------------------------------------
# MongoDB Connection
# -------------------------------------------------
try:
    client = AsyncMongoClient(
        os.getenv("MONGODB_URI"),
        tls=True,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=5000,
        socketTimeoutMS=5000
    )
    db = client[os.getenv("MONGODB_DB")]
    collection = db[os.getenv("MONGODB_COLLECTION")]
    logger.info("MongoDB connected successfully")
except Exception as e:
    logger.error(f"MongoDB connection failed: {str(e)}")
    raise

# -------------------------------------------------
# Core Task: Fetch from CrewAI API and Save to MongoDB
#
# This runs automatically every morning at 9:00 AM IST.
# It is NOT exposed as a public API endpoint — only the
# scheduler inside the server triggers it.
# -------------------------------------------------
async def run_crew_and_save():
    global is_task_running

    if is_task_running:
        logger.warning("Scheduled task skipped — a task is already running.")
        return

    is_task_running = True
    logger.info("Scheduled job started: Fetching news from CrewAI API...")

    try:
        async with httpx.AsyncClient(timeout=120.0) as http_client:
            response = await http_client.post(
                "https://crewai-api-news.onrender.com/run-crew"
            )
            response.raise_for_status()
            data = response.json()

        report_dict = data.get("result")

        if not report_dict or not isinstance(report_dict, dict):
            logger.error(f"Invalid response from CrewAI API: {data}")
            return

        # Add metadata before saving to MongoDB
        report_dict["topic_searched"] = "LLM, Agentic AI, AI Updates, AI Tools, Machine Learning"
        report_dict["created_at"] = datetime.now(timezone.utc)

        await collection.insert_one(report_dict)
        logger.info("News data saved to MongoDB successfully!")

    except httpx.HTTPError as e:
        logger.error(f"HTTP Error calling CrewAI API: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error in scheduled job: {str(e)}")
    finally:
        is_task_running = False

# -------------------------------------------------
# Scheduler Setup: Runs at 9:00 AM IST every day
# -------------------------------------------------
IST = pytz.timezone("Asia/Kolkata")
scheduler = AsyncIOScheduler(timezone=IST)

scheduler.add_job(
    run_crew_and_save,
    trigger=CronTrigger(hour=9, minute=0, timezone=IST),  # 9:00 AM IST daily
    id="daily_news_fetch",
    name="Daily CrewAI News Fetch",
    replace_existing=True,
    misfire_grace_time=300  # Allow up to 5 min late if server was briefly down
)

# -------------------------------------------------
# App Lifespan: Start/Stop Scheduler with the app
# -------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting scheduler - news will be fetched daily at 9:00 AM IST")
    scheduler.start()
    yield
    logger.info("Shutting down scheduler...")
    scheduler.shutdown()

# -------------------------------------------------
# FastAPI App
# -------------------------------------------------
app = FastAPI(
    title="AI News Digest API",
    version="2.0.0",
    description="Serves AI-curated news to the Android app. News is auto-fetched every day at 9 AM IST.",
    lifespan=lifespan,
    docs_url=None,      # Disable /docs in production
    redoc_url=None,     # Disable /redoc in production
    openapi_url=None    # Disable /openapi.json in production
)

# CORS - Android native apps don't use CORS but keep for any future web client
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # We use API key headers, not cookies
    allow_methods=["GET"],
    allow_headers=["X-API-Key", "Content-Type"],
)

# -------------------------------------------------
# Public Endpoints
# -------------------------------------------------

@app.get("/", tags=["health"])
async def root():
    """Health check - no auth required"""
    next_run = scheduler.get_job("daily_news_fetch").next_run_time
    return {
        "status": "API is running",
        "version": "2.0.0",
        "next_scheduled_fetch": str(next_run)
    }


@app.get("/get", tags=["data"])
async def get_data(api_key: str = Depends(verify_api_key)):
    """
    Retrieve the latest 10 news reports from MongoDB.

    Android App: Include X-API-Key in the request header.

    News is automatically updated every day at 9:00 AM IST.
    """
    try:
        cursor = collection.find(
            {},
            {"_id": 1, "topic_searched": 1, "created_at": 1, "title": 1, "summary": 1, "articles": 1}
        ).sort("_id", -1).limit(10)

        reports = await cursor.to_list(length=10)

        for report in reports:
            report["_id"] = str(report["_id"])

        return {
            "count": len(reports),
            "data": reports
        }

    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        return JSONResponse(
            content={"error": "Failed to retrieve data"},
            status_code=500
        )
