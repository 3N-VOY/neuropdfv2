from datetime import datetime, timedelta
from typing import Dict, Optional
from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address
import os
import PyPDF2
from io import BytesIO
import firebase_admin
from firebase_admin import credentials, auth, firestore
import logging
from dotenv import load_dotenv
import base64
import json
# Load environment variables
load_dotenv()
# Setup simple logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("security")

# Initialize Firebase Admin with credentials from environment variable
# Initialize Firebase Admin with credentials from environment variable
firebase_creds_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
if not firebase_creds_json:
    firebase_creds_json = os.getenv("FIREBASE_CREDENTIALS_PATH")

if firebase_creds_json:
    try:
        # Try to decode as base64 first
        try:
            decoded_bytes = base64.b64decode(firebase_creds_json)
            cred_dict = json.loads(decoded_bytes)
            cred = credentials.Certificate(cred_dict)
        except (base64.binascii.Error, json.JSONDecodeError):
            # If base64 decoding or JSON fails, treat as a file path
            cred = credentials.Certificate(firebase_creds_json)

        if not firebase_admin._apps:
            firebase_app = firebase_admin.initialize_app(cred)
        db = firestore.client()
    except Exception as e:
        print(f"Error initializing Firebase: {str(e)}")
        raise
else:
    print("No Firebase credentials found in environment variables")
    raise ValueError("Firebase credentials not found")

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Security constants
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_PAGES = 50
ALLOWED_MIME_TYPES = ['application/pdf']
RATE_LIMIT_MINUTE = 60
RATE_LIMIT_HOUR = 300
RATE_LIMIT_DAY = 1000

# API Key header
api_key_header = APIKeyHeader(name="X-API-Key")

# Store API keys
api_keys: Dict[str, Dict] = {}

def is_pdf(file_content: bytes) -> bool:
    """Check if the file content is a PDF by examining the header."""
    return file_content.startswith(b'%PDF-')

# Function to verify Firebase tokens
def verify_firebase_token(token):
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")



def validate_api_key(api_key: str = Security(api_key_header)) -> str:
    # First check memory cache
    if api_key in api_keys:
        # Check if expired
        if datetime.now() > api_keys[api_key].get("expires_at", datetime.max):
            logger.warning(f"Expired API key used: {api_key[:8]}...")
            raise HTTPException(status_code=401, detail="API key expired")
        return api_key

    # If not in memory, check Firestore
    try:
        key_doc = db.collection('api_keys').document(api_key).get()
        if not key_doc.exists:
            logger.warning(f"Invalid API key attempt: {api_key[:8]}...")
            raise HTTPException(status_code=401, detail="Invalid API key")

        key_data = key_doc.to_dict()
        expires_at = datetime.fromisoformat(key_data.get('expires_at'))

        if datetime.now() > expires_at:
            logger.warning(f"Expired API key used: {api_key[:8]}...")
            raise HTTPException(status_code=401, detail="API key expired")

        # Add to memory cache
        api_keys[api_key] = {
            "user_id": key_data.get('user_id'),
            "daily_usage": key_data.get('daily_usage', 0),
            "last_reset": datetime.fromisoformat(key_data.get('last_reset')),
            "expires_at": expires_at
        }

        return api_key
    except Exception as e:
        logger.error(f"Error validating API key: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Error validating API key: {str(e)}")

def check_file_size(file_size: int):
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File size exceeds maximum limit of {MAX_FILE_SIZE/1024/1024}MB"
        )

def validate_pdf_content(file_content: bytes):
    # Check if it's a PDF by header
    if not is_pdf(file_content):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only PDF files are allowed."
        )

    # Check page count
    try:
        pdf = PyPDF2.PdfReader(BytesIO(file_content))
        if len(pdf.pages) > MAX_PAGES:
            raise HTTPException(
                status_code=400,
                detail=f"PDF exceeds maximum page limit of {MAX_PAGES}"
            )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Invalid PDF file"
        )

def update_usage_metrics(api_key: str, bytes_processed: int):
    if api_key not in api_keys:
        # Try to get from Firestore
        key_doc = db.collection('api_keys').document(api_key).get()
        if key_doc.exists:
            key_data = key_doc.to_dict()
            api_keys[api_key] = {
                "user_id": key_data.get('user_id'),
                "daily_usage": key_data.get('daily_usage', 0),
                "last_reset": datetime.fromisoformat(key_data.get('last_reset')),
                "expires_at": datetime.fromisoformat(key_data.get('expires_at'))
            }
        else:
            api_keys[api_key] = {
                "daily_usage": 0,
                "last_reset": datetime.now()
            }

    # Reset daily usage if it's a new day
    if datetime.now() - api_keys[api_key]["last_reset"] > timedelta(days=1):
        api_keys[api_key]["daily_usage"] = 0
        api_keys[api_key]["last_reset"] = datetime.now()

    api_keys[api_key]["daily_usage"] += bytes_processed

    # Update in Firestore
    try:
        db.collection('api_keys').document(api_key).update({
            'daily_usage': api_keys[api_key]["daily_usage"],
            'last_reset': api_keys[api_key]["last_reset"].isoformat()
        })
    except Exception as e:
        logger.error(f"Error updating usage in Firestore: {str(e)}")

    logger.info(f"Updated usage for API key {api_key[:8]}...: {api_keys[api_key]['daily_usage']/1024/1024:.2f}MB")

def check_quota(api_key: str):
    DAILY_QUOTA = 50 * 1024 * 1024  # 50MB per day

    if api_keys[api_key]["daily_usage"] > DAILY_QUOTA:
        raise HTTPException(
            status_code=429,
            detail="Daily quota exceeded"
        )