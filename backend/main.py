from fastapi import FastAPI, UploadFile, HTTPException, Form, File, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import tempfile
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
import uuid
import re
import logging
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_pinecone import PineconeVectorStore
from langchain_core.messages import HumanMessage, SystemMessage
from langchain.chat_models import init_chat_model
from pinecone import Pinecone, ServerlessSpec
from security import (
    limiter, validate_api_key, check_file_size, validate_pdf_content,
    update_usage_metrics, check_quota, RATE_LIMIT_MINUTE, api_keys, 
    verify_firebase_token, db
)
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from firebase_admin import firestore
from slowapi import Limiter
from slowapi.util import get_remote_address
# Load environment variables
load_dotenv()
# Initialize Firestore
db = firestore.client()
# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("api.log"), logging.StreamHandler()]
)
logger = logging.getLogger("pdf_api")

# Check if we're in production mode
is_production = os.getenv("ENVIRONMENT") == "production"
logger.info(f"Starting application in {'PRODUCTION' if is_production else 'DEVELOPMENT'} mode")

# Initialize FastAPI app
app = FastAPI(title="PDF Q&A API")

# Add middleware
if is_production:
    # Production - get allowed origins from environment variables
    frontend_urls = [
        os.getenv("FRONTEND_URL1", ""),
        os.getenv("FRONTEND_URL2", "")
    ]
    # Filter out empty strings
    frontend_urls = [url for url in frontend_urls if url]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=frontend_urls,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS", "PUT"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Device-Fingerprint"],
    )
else:
    # Development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # Frontend dev server
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)



# Create a limiter based on IP address
ip_limiter = Limiter(key_func=get_remote_address)

# Initialize Groq LLM
llm = init_chat_model("llama-3.3-70b-versatile", model_provider="groq")

# Initialize Pinecone client
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
PINECONE_INDEX_NAME = "pdf-search"
DIMENSION = 768

# Store the current active document namespace
CURRENT_NAMESPACE = None

# Pydantic models for request/response
class QuestionRequest(BaseModel):
    question: str

class QuestionResponse(BaseModel):
    answer: str
    context: Optional[str] = None

@app.get("/health")
async def health_check():
    return {"status": "healthy", "environment": "production" if is_production else "development"}

# Sanitize namespace name to avoid Pinecone errors
def sanitize_namespace(name):
    # Remove file extension
    name = name.replace('.pdf', '')
    # Replace spaces and special characters with underscores
    sanitized = re.sub(r'[^a-zA-Z0-9]', '_', name)
    # Ensure it's not too long (Pinecone may have limits)
    if len(sanitized) > 50:
        sanitized = sanitized[:50]
    return sanitized

# Ensure Pinecone index exists
def create_index_if_not_exists():
    try:
        pc.describe_index(PINECONE_INDEX_NAME)
        logger.info(f"Found existing Pinecone index: {PINECONE_INDEX_NAME}")
    except Exception as e:
        logger.info(f"Creating new Pinecone index: {PINECONE_INDEX_NAME}")
        pc.create_index(
            name=PINECONE_INDEX_NAME,
            dimension=DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )

# Check if namespace exists
def namespace_exists(index, namespace):
    try:
        stats = index.describe_index_stats()
        namespaces = stats.get("namespaces", {})
        return namespace in namespaces
    except Exception as e:
        logger.error(f"Error checking namespace existence: {str(e)}")
        return False

# Initialize embeddings model
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-mpnet-base-v2")



@app.post("/upload")
@limiter.limit(f"{RATE_LIMIT_MINUTE}/minute")
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
    api_key: str = Depends(validate_api_key)
):
    try:
        content = await file.read()

        # Security checks
        check_file_size(len(content))
        validate_pdf_content(content)
        check_quota(api_key)

        # Update usage metrics
        update_usage_metrics(api_key, len(content))

        # Generate a unique namespace for this document
        global CURRENT_NAMESPACE
        user_id = api_keys[api_key].get("user_id", "anonymous")
        CURRENT_NAMESPACE = f"{user_id}_{sanitize_namespace(file.filename)}"

        logger.info(f"Processing PDF: {file.filename} with namespace: {CURRENT_NAMESPACE}")

        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        # Process PDF
        docs = PyPDFLoader(temp_path).load()
        logger.info(f"Loaded {len(docs)} pages from PDF")

        # Split text into smaller chunks for better retrieval
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=100,
            add_start_index=True
        )
        chunks = text_splitter.split_documents(docs)
        logger.info(f"Created {len(chunks)} chunks from PDF")

        # Add enhanced metadata to chunks
        for i, chunk in enumerate(chunks):
            chunk.metadata["chunk_id"] = i
            chunk.metadata["filename"] = file.filename
            # Ensure page metadata exists
            if "page" not in chunk.metadata:
                chunk.metadata["page"] = chunk.metadata.get("page", "unknown")

        # Connect to Pinecone index
        index = pc.Index(PINECONE_INDEX_NAME)

        # Only try to delete the namespace if it exists
        if namespace_exists(index, CURRENT_NAMESPACE):
            try:
                index.delete(delete_all=True, namespace=CURRENT_NAMESPACE)
                logger.info(f"Cleared namespace {CURRENT_NAMESPACE} in Pinecone index")
            except Exception as e:
                # Log the error but continue processing
                logger.warning(f"Failed to delete namespace {CURRENT_NAMESPACE}: {str(e)}")
        else:
            logger.info(f"Namespace {CURRENT_NAMESPACE} doesn't exist yet, skipping deletion")

        # Store in Pinecone with namespace
        vector_store = PineconeVectorStore(
            embedding=embeddings,
            index=index,
            namespace=CURRENT_NAMESPACE
        )

        ids = vector_store.add_documents(documents=chunks)
        logger.info(f"Added {len(ids)} chunks to Pinecone namespace: {CURRENT_NAMESPACE}")

        # Cleanup
        os.unlink(temp_path)

        return {
            "message": f"PDF processed successfully. Created {len(chunks)} chunks in namespace {CURRENT_NAMESPACE}.",
            "namespace": CURRENT_NAMESPACE
        }

    except RateLimitExceeded:
        logger.warning(f"Rate limit exceeded for API key: {api_key}")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later."
        )
    except Exception as e:
        logger.error(f"Error processing PDF: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    


@app.post("/ask", response_model=QuestionResponse)
@limiter.limit(f"{RATE_LIMIT_MINUTE}/minute")
async def ask_question(
    request: Request,
    question: QuestionRequest,
    api_key: str = Depends(validate_api_key)
):
    try:
        global CURRENT_NAMESPACE
        if not CURRENT_NAMESPACE:
            raise HTTPException(
                status_code=400,
                detail="No document has been uploaded yet. Please upload a PDF first."
            )

        logger.info(f"Processing question: {question.question} in namespace: {CURRENT_NAMESPACE}")

        # Query vector store with the current namespace
        index = pc.Index(PINECONE_INDEX_NAME)
        vector_store = PineconeVectorStore(
            embedding=embeddings,
            index=index,
            namespace=CURRENT_NAMESPACE
        )

        # Retrieve relevant documents
        results = vector_store.similarity_search(question.question, k=5)

        if not results:
            return QuestionResponse(
                answer="I don't have enough information in the document to answer this question.",
                context="No relevant content found in the document."
            )

        # Format context with clear section markers
        context_parts = []
        for i, doc in enumerate(results):
            # Add document metadata to help LLM understand the source
            metadata_str = f"[Document: {doc.metadata.get('filename', 'Unknown')}, Page: {doc.metadata.get('page', 'Unknown')}]"
            context_parts.append(f"DOCUMENT SECTION {i+1} {metadata_str}:\n{doc.page_content}")

        context = "\n\n" + "\n\n".join(context_parts)

        # Enhanced system prompt
        system_message = """
        You have access to a PDF document. Your task is to answer the user's questions strictly based on the content of the PDF.
        If a question cannot be answered from the PDF, respond with: "The answer is not found in the document."
        Be accurate, concise, and reference relevant sections or quotes when possible. Wait for the user's question.
        """

        # Improved prompt format
        messages = [
            SystemMessage(content=system_message),
            HumanMessage(content=f"""CONTEXT:
{context}

QUESTION: {question.question}

Remember: ONLY use information from the document provided. If the answer isn't in the document, say "I don't have enough information in the document to answer this question."
""")
        ]

        logger.info(f"Sending prompt to LLM with context length: {len(context)}")

        # Get answer from LLM
        response = llm.invoke(messages)
        logger.info(f"Received response from LLM")

        return QuestionResponse(
            answer=response.content,
            context=context
        )

    except RateLimitExceeded:
        logger.warning(f"Rate limit exceeded for API key: {api_key}")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later."
        )
    except Exception as e:
        logger.error(f"Error in ask_question: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Debug endpoints - only available in non-production environments
if not is_production:
    @app.get("/debug/index-info")
    async def get_index_info(api_key: str = Depends(validate_api_key)):
        """Get information about the Pinecone index for debugging"""
        try:
            index = pc.Index(PINECONE_INDEX_NAME)
            stats = index.describe_index_stats()
            return {
                "index_name": PINECONE_INDEX_NAME,
                "vector_count": stats.get("total_vector_count", 0),
                "dimension": stats.get("dimension", DIMENSION),
                "namespaces": stats.get("namespaces", {}),
                "current_namespace": CURRENT_NAMESPACE
            }
        except Exception as e:
            return {"error": str(e)}

    @app.post("/debug/clear-index")
    async def clear_index(api_key: str = Depends(validate_api_key)):
        """Clear the entire Pinecone index for debugging"""
        try:
            index = pc.Index(PINECONE_INDEX_NAME)
            index.delete(delete_all=True)
            global CURRENT_NAMESPACE
            CURRENT_NAMESPACE = None
            return {"message": "Index cleared successfully"}
        except Exception as e:
            return {"error": str(e)}


@app.on_event("startup")
async def startup_event():
    # Create Pinecone index if needed
    create_index_if_not_exists()

    # Load API keys from Firestore
    try:
        keys_ref = db.collection('api_keys').stream()
        for key_doc in keys_ref:
            key_data = key_doc.to_dict()
            api_key = key_doc.id
            api_keys[api_key] = {
                "user_id": key_data.get('user_id'),
                "daily_usage": key_data.get('daily_usage', 0),
                "last_reset": datetime.fromisoformat(key_data.get('last_reset')),
                "expires_at": datetime.fromisoformat(key_data.get('expires_at'))
            }
        logger.info(f"Loaded {len(api_keys)} API keys from Firestore")
    except Exception as e:
        logger.error(f"Error loading API keys from Firestore: {str(e)}")

    logger.info("API started and connected to Pinecone index")
    
# New endpoint to create API keys with expiration
# Replace your existing create_api_key endpoint with this:
@app.post("/create-api-key")
@limiter.limit(f"{RATE_LIMIT_MINUTE}/minute")  # Your existing rate limit
@ip_limiter.limit("2/day")
async def create_api_key(request: Request):
    # Get the token from the Authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        # Reject unauthenticated requests
        raise HTTPException(
            status_code=401,
            detail="Authentication required"
        )

    # Extract and verify the token
    token = auth_header.split("Bearer ")[1]
    try:
        # Verify the Firebase token (works with any auth method including Google)
        user_data = verify_firebase_token(token)
        user_id = user_data["uid"]

        # Check if user exists in your database
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()

        if not user_doc.exists:
            # First time user - create user record
            auth_provider = "google" if "firebase.google.com" in user_data.get("firebase", {}).get("sign_in_provider", "") else "email"
            user_ref.set({
                'email': user_data.get('email', ''),
                'display_name': user_data.get('name', user_data.get('email', '').split('@')[0]),
                'created_at': datetime.now().isoformat(),
                'auth_provider': auth_provider,
                'last_login': datetime.now().isoformat()
            })
            logger.info(f"Created new user {user_id} with email {user_data.get('email', '')}")
        else:
            # Update last login time
            user_ref.update({
                'last_login': datetime.now().isoformat()
            })
            logger.info(f"User {user_id} logged in")

        # Add expiration (30 days from now)
        expires_at = datetime.now() + timedelta(days=30)

        # Create an API key associated with this user
        api_key = str(uuid.uuid4())

        # Store in Firestore
        db.collection('api_keys').document(api_key).set({
            'user_id': user_id,
            'daily_usage': 0,
            'last_reset': datetime.now().isoformat(),
            'expires_at': expires_at.isoformat()
        })

        # Also keep in memory for quick access
        api_keys[api_key] = {
            "user_id": user_id,
            "daily_usage": 0,
            "last_reset": datetime.now(),
            "expires_at": expires_at
        }

        logger.info(f"Created new API key for user {user_id}")

        return {"api_key": api_key, "expires_at": expires_at.isoformat()}
    except Exception as e:
        # If token verification fails, reject the request
        logger.error(f"API key creation failed: {str(e)}")
        raise HTTPException(
            status_code=401,
            detail=f"Invalid authentication token: {str(e)}"
        )
        
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)