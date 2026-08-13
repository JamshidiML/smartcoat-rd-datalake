from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from minio import Minio
from pydantic import BaseModel

from database import PostgresRepository
from domain import Actor, IngestionService, ReviewService, StateConflict
from security import InvalidSession, issue_session, verify_session
from storage import MinioObjectStorage
from validation import UploadValidationError


DATABASE_URL = os.environ["DATABASE_URL"]
LOCAL_USER_ID = os.getenv("LOCAL_USER_ID", "usr_founder")
LOCAL_USER_DISPLAY_NAME = os.getenv("LOCAL_USER_DISPLAY_NAME", "SmartCoat Founder")
LOCAL_USER_EMAIL = os.getenv("LOCAL_USER_EMAIL", "founder@localhost")
LOCAL_USER_PASSWORD = os.environ["LOCAL_USER_PASSWORD"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
WEB_ORIGIN = os.getenv("WEB_ORIGIN", "http://127.0.0.1:8080")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
ALLOW_SOLO_REVIEW = os.getenv("ALLOW_PHASE_1_SOLO_SELF_REVIEW", "true").lower() == "true"

repository = PostgresRepository(DATABASE_URL)
minio_client = Minio(
    os.getenv("MINIO_ENDPOINT", "minio:9000"),
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
)
storage = MinioObjectStorage(minio_client)
ingestion_service = IngestionService(repository, storage, MAX_UPLOAD_BYTES)
review_service = ReviewService(repository, ALLOW_SOLO_REVIEW)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    repository.ensure_local_user(LOCAL_USER_ID, LOCAL_USER_DISPLAY_NAME, LOCAL_USER_EMAIL)
    yield


app = FastAPI(title="SmartCoat R&D Data Lake API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


class LoginRequest(BaseModel):
    email: str
    password: str


class ReviewRequest(BaseModel):
    verified_text: str
    decision: str
    correction_summary: str = ""
    explicit_confirmation: bool
    administrator_exception_reason: str | None = None


class RevisionRequest(BaseModel):
    text: str


def current_actor(authorization: Annotated[str | None, Header()] = None) -> Actor:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        user_id = verify_session(authorization.removeprefix("Bearer "), SESSION_SECRET)
    except InvalidSession as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if user_id != LOCAL_USER_ID:
        raise HTTPException(status_code=403, detail="Unknown local user")
    return Actor(LOCAL_USER_ID, LOCAL_USER_DISPLAY_NAME)


def serializable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serializable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readiness() -> dict[str, str]:
    with repository.connection() as connection:
        connection.execute("SELECT 1")
    if not minio_client.bucket_exists("sc-rd-bronze-manifests"):
        raise HTTPException(status_code=503, detail="Bronze manifest bucket unavailable")
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(request: LoginRequest) -> dict[str, str]:
    email_ok = hmac.compare_digest(request.email.lower(), LOCAL_USER_EMAIL.lower())
    password_ok = hmac.compare_digest(request.password, LOCAL_USER_PASSWORD)
    if not (email_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid local credentials")
    return {
        "access_token": issue_session(LOCAL_USER_ID, SESSION_SECRET),
        "token_type": "bearer",
        "user_id": LOCAL_USER_ID,
        "display_name": LOCAL_USER_DISPLAY_NAME,
    }


@app.post("/api/uploads", status_code=201)
async def upload(
    actor: Annotated[Actor, Depends(current_actor)],
    file: Annotated[UploadFile, File()],
    document_category: Annotated[str, Form()],
    context_note: Annotated[str, Form()],
    capture_date: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        return serializable(
            ingestion_service.ingest(
                actor,
                file.filename or "upload",
                data,
                document_category,
                context_note,
                capture_date or None,
            )
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "reason": exc.reason}) from exc
    except StateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/uploads")
def uploads(_: Annotated[Actor, Depends(current_actor)]) -> list[dict[str, Any]]:
    return serializable(repository.list_uploads())


@app.get("/api/uploads/{ingestion_id}/source")
def source(ingestion_id: str, _: Annotated[Actor, Depends(current_actor)]) -> Response:
    record = repository.get_upload(ingestion_id)
    data = storage.get("sc-rd-bronze-originals", record["stored_object_key"])
    return Response(data, media_type=record["detected_mime_type"], headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/uploads/{ingestion_id}/audit")
def audit(ingestion_id: str, _: Annotated[Actor, Depends(current_actor)]) -> list[dict[str, Any]]:
    return serializable(repository.audit_events(ingestion_id))


@app.get("/api/drafts")
def drafts(_: Annotated[Actor, Depends(current_actor)]) -> list[dict[str, Any]]:
    return serializable(repository.list_drafts())


@app.get("/api/drafts/{draft_id}")
def draft(draft_id: str, _: Annotated[Actor, Depends(current_actor)]) -> dict[str, Any]:
    return serializable(repository.get_review_context(draft_id))


@app.post("/api/drafts/{draft_id}/review")
def review(
    draft_id: str,
    request: ReviewRequest,
    actor: Annotated[Actor, Depends(current_actor)],
) -> dict[str, Any]:
    try:
        result = review_service.review(
            draft_id,
            actor,
            request.verified_text,
            request.decision,
            request.correction_summary,
            request.explicit_confirmation,
            request.administrator_exception_reason,
        )
        return serializable(result or {"status": "REVIEW_REJECTED"})
    except (ValueError, StateConflict) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/verified/{ingestion_id}/revisions", status_code=201)
def revise(
    ingestion_id: str,
    request: RevisionRequest,
    actor: Annotated[Actor, Depends(current_actor)],
) -> dict[str, Any]:
    try:
        return serializable(review_service.edit_verified(ingestion_id, actor, request.text))
    except StateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
