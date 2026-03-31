from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pymongo import AsyncMongoClient
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import os
import certifi
import httpx

# Load environment variables
load_dotenv()

# Track if a task is currently running to prevent duplicates
is_task_running = False

# Create FastAPI app
app = FastAPI(title="AI Research Digest API", version="1.0.0")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ FIXED MongoDB connection
client = AsyncMongoClient(
    os.getenv("MONGODB_URI"),
    tls=True,
    tlsCAFile=certifi.where()
)

db = client[os.getenv("MONGODB_DB")]
collection = db[os.getenv("MONGODB_COLLECTION")]

# Root API
@app.get("/")
async def root():
    return {"message": "API is working!"}


# 🔥 Task to call external API, save, and return data
async def run_crew_and_save():
    global is_task_running
    is_task_running = True
    
    try:
        print("🚀 Starting CrewAI task via external API...")

        # Call the new external API endpoint
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post("https://crewai-api-news.onrender.com/run-crew")
            response.raise_for_status()
            data = response.json()
        
        # Extract the dictionary contained in the "result" key
        report_dict = data.get("result")
        
        if not report_dict or not isinstance(report_dict, dict):
            print(f"❌ Invalid or missing 'result' data from API: {data}")
            return {"error": "Invalid data received from external API"}

        # Add tracking metadata
        report_dict["topic_searched"] = "LLM, Agentic AI, AI Updates, AI Tools, Machine Learning"
        report_dict["created_at"] = datetime.now(timezone.utc)

        result = await collection.insert_one(report_dict)
        
        # Convert ObjectId to string so we can return it as JSON safely
        report_dict["_id"] = str(result.inserted_id)

        print("✅ Data saved successfully!")
        return report_dict

    except Exception as e:
        print(f"❌ Error in task: {e}")
        return {"error": str(e)}
        
    finally:
        is_task_running = False


# 🔥 Start process API (Uses BackgroundTasks again to prevent timeout)
@app.post("/start_crew_save")
async def save_data(background_tasks: BackgroundTasks):
    global is_task_running
    
    if is_task_running:
        return {
            "message": "A task is already running in the background! 🚀",
            "status": "processing"
        }

    background_tasks.add_task(run_crew_and_save)

    return {
        "message": "Processing started in the background! 🚀 Please check the /get endpoint periodically.",
        "status": "processing"
    }


# 🔥 Get data API
@app.get("/get")
async def get_data():
    try:
        cursor = collection.find().sort("_id", -1).limit(10)
        reports = await cursor.to_list(length=10)

        for report in reports:
            report["_id"] = str(report["_id"])

        return reports

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
