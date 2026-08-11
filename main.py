# main.py - Complete Log Analyzer API

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
import jwt
from typing import List, Optional, Dict, Any
import os
import re
import json
import uuid
from google import genai
from dotenv import load_dotenv
from db import db, init_database
from agent import start_analysis, resume_analysis

load_dotenv()

# ============================================
# CONFIGURATION
# ============================================

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

VALID_USER = {
    "username": os.getenv("ADMIN_USERNAME", "admin"),
    "password": os.getenv("ADMIN_PASSWORD", "password123")
}

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not found in .env")

client = genai.Client(api_key=GEMINI_API_KEY)
CLASSIFICATION_MODEL = os.getenv("CLASSIFICATION_MODEL", "models/gemini-3.5-flash")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "models/gemini-embedding-001")

# ============================================
# PYDANTIC MODELS
# ============================================

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str

class LogEntry(BaseModel):
    id: Optional[int] = None
    timestamp: str
    level: str
    message: str
    user: Optional[str] = None

class LogClassification(BaseModel):
    severity: str
    likely_cause: str
    suggested_action: str
    category: str

# ============================================
# FASTAPI APP
# ============================================

app = FastAPI(title="Log Analyzer API")
security = HTTPBearer()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# JWT AUTH
# ============================================

def create_access_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

# ============================================
# HELPERS
# ============================================

def get_embedding(text: str) -> Optional[List[float]]:
    try:
        response = client.models.embed_content(model=EMBEDDING_MODEL, contents=text)
        return response.embeddings[0].values
    except Exception as e:
        print(f"❌ Embedding error: {e}")
        return None

def classify_log_with_gemini(log: dict) -> dict:
    prompt = f"""Classify this log. Return ONLY JSON:
{{
    "severity": "critical|warning|info",
    "likely_cause": "brief cause",
    "suggested_action": "action to fix",
    "category": "auth|network|database|deployment|federation"
}}

Log: {log.get('message', '')}"""
    try:
        response = client.models.generate_content(model=CLASSIFICATION_MODEL, contents=prompt)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        return json.loads(match.group()) if match else {}
    except:
        return {"severity": "info", "likely_cause": "Unknown", "suggested_action": "Investigate", "category": "deployment"}

# ============================================
# API ENDPOINTS
# ============================================

@app.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    if request.username != VALID_USER["username"] or request.password != VALID_USER["password"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": create_access_token(request.username), "token_type": "bearer"}

@app.get("/logs")
async def get_logs(username: str = Depends(verify_token)):
    return db.execute("SELECT id, timestamp, level, message, user_name as user FROM logs ORDER BY id")

@app.get("/logs/{log_id}")
async def get_log(log_id: int, username: str = Depends(verify_token)):
    result = db.execute("SELECT id, timestamp, level, message, user_name as user FROM logs WHERE id = %s", [log_id])
    if not result:
        raise HTTPException(status_code=404, detail="Log not found")
    return result[0]

@app.post("/logs/{log_id}/classify")
async def classify_log(log_id: int, username: str = Depends(verify_token)):
    log = db.execute("SELECT id, level, message FROM logs WHERE id = %s", [log_id])
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    return classify_log_with_gemini(log[0])

@app.get("/logs/{log_id}/similar")
async def similar_logs(log_id: int, limit: int = 5, username: str = Depends(verify_token)):
    # Get embedding
    emb_result = db.execute("SELECT embedding FROM log_embeddings WHERE log_id = %s", [log_id])
    if not emb_result:
        return {"log_id": log_id, "similar_logs": [], "count": 0}
    
    target_emb = emb_result[0]['embedding']
    results = db.execute("""
        SELECT l.id, l.timestamp, l.level, l.message, l.user_name,
               1 - (le.embedding <=> %s) AS similarity
        FROM logs l
        JOIN log_embeddings le ON l.id = le.log_id
        WHERE l.id != %s
        ORDER BY le.embedding <=> %s
        LIMIT %s
    """, [target_emb, log_id, target_emb, limit])
    
    return {"log_id": log_id, "similar_logs": results, "count": len(results)}

class ApprovalRequest(BaseModel):
    approved: bool

@app.post("/logs/{log_id}/analyze")
async def analyze_log(log_id: int, username: str = Depends(verify_token)):
    """LangGraph pipeline, part 1: classify → similar logs → PAUSE for approval."""
    thread_id = str(uuid.uuid4())  # names this run for the checkpointer
    result = start_analysis(log_id, thread_id)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])

    if "__interrupt__" in result:
        return {
            "status": "awaiting_approval",
            "thread_id": thread_id,   # client must send this back to approve
            "prompt": result["__interrupt__"][0].value,
            "log": result["log"],
            "classification": result["classification"],
            "similar_logs": result["similar_logs"],
        }
    return {"status": "complete", "report": result["report"]}

@app.post("/analysis/{thread_id}/approve")
async def approve_analysis(thread_id: str, request: ApprovalRequest,
                           username: str = Depends(verify_token)):
    """LangGraph pipeline, part 2: resume the paused graph with the decision."""
    try:
        result = resume_analysis(thread_id, request.approved)
    except Exception:
        raise HTTPException(status_code=404, detail="No paused analysis for that thread_id")
    return {
        "status": "complete",
        "approved": request.approved,
        "report": result["report"],
    }

@app.get("/health")
async def health():
    return {"status": "healthy", "model": CLASSIFICATION_MODEL}

@app.get("/")
async def index():
    return FileResponse("index.html")

# ============================================
# STARTUP
# ============================================

@app.on_event("startup")
async def startup():
    print("🚀 Starting Log Analyzer API...")
    init_database()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))