"""Ajaia Docs — FastAPI backend.

Serves the frontend as static files and exposes a REST + WebSocket API.
All secrets come from environment variables (see .env.example).
"""

import json
import os
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
import httpx
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import (
    ALGORITHM,
    SECRET_KEY,
    create_access_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from database import Base, SessionLocal, engine, get_db
from email_service import (
    access_decision_email,
    access_request_email,
    invite_email,
    send_email,
    share_notification_email,
)
from file_parser import docx_to_delta, md_to_delta, txt_to_delta
import models

load_dotenv()

# ── Bootstrap ──────────────────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)


def _seed_if_empty() -> None:
    db = SessionLocal()
    try:
        if db.query(models.User).count() == 0:
            from seed import do_seed
            do_seed(db)
    finally:
        db.close()


_seed_if_empty()

UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", "uploads"))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

FRONTEND_PATH = Path(__file__).parent / "frontend"

APP_URL = os.getenv("APP_URL", "http://localhost:8000/")

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Ajaia Docs", version="1.0.0", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_PATH / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


# ── WebSocket presence manager ─────────────────────────────────────────────────
class PresenceManager:
    def __init__(self):
        # doc_id -> list[{"ws": WebSocket, "user_id": int, "username": str}]
        self._rooms: Dict[int, List[dict]] = {}

    async def join(self, ws: WebSocket, doc_id: int, user_id: int, username: str):
        await ws.accept()
        self._rooms.setdefault(doc_id, []).append(
            {"ws": ws, "user_id": user_id, "username": username}
        )
        await self._broadcast_presence(doc_id)

    def leave(self, ws: WebSocket, doc_id: int):
        if doc_id in self._rooms:
            self._rooms[doc_id] = [c for c in self._rooms[doc_id] if c["ws"] is not ws]
            if not self._rooms[doc_id]:
                del self._rooms[doc_id]

    async def _broadcast_presence(self, doc_id: int):
        if doc_id not in self._rooms:
            return
        users = [{"user_id": c["user_id"], "username": c["username"]} for c in self._rooms[doc_id]]
        msg = json.dumps({"type": "presence", "users": users})
        for conn in list(self._rooms.get(doc_id, [])):
            try:
                await conn["ws"].send_text(msg)
            except Exception:
                pass

    async def broadcast_to_others(self, ws: WebSocket, doc_id: int, payload: dict):
        msg = json.dumps(payload)
        for conn in list(self._rooms.get(doc_id, [])):
            if conn["ws"] is not ws:
                try:
                    await conn["ws"].send_text(msg)
                except Exception:
                    pass


presence = PresenceManager()


# ── Pydantic request/response models ──────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str


class DocumentCreate(BaseModel):
    title: str = "Untitled Document"
    content_delta: str = '{"ops":[{"insert":"\\n"}]}'


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    content_delta: Optional[str] = None
    word_target: Optional[int] = None


class ShareCreate(BaseModel):
    username: str
    permission: str = "viewer"  # viewer | editor


class InviteCreate(BaseModel):
    email: str
    permission: str = "viewer"


class SectionShareCreate(BaseModel):
    username: str
    section_heading: str
    section_index: int
    permission: str = "viewer"


class CommentCreate(BaseModel):
    content: str
    selection_index: Optional[int] = None
    selection_length: Optional[int] = None


class GrammarCheckRequest(BaseModel):
    text: str
    language: str = "en-US"


class PublicLinkCreate(BaseModel):
    permission: str = "viewer"  # viewer | editor


class AccessRequestCreate(BaseModel):
    requested_permission: str = "editor"
    message: Optional[str] = None


class AccessRequestDecision(BaseModel):
    approved: bool


# ── Helpers ────────────────────────────────────────────────────────────────────
def _require_doc_access(doc_id: int, user: models.User, db: Session):
    """Return (document, permission_str) or raise 403/404."""
    doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.owner_id == user.id:
        return doc, "owner"
    share = (
        db.query(models.DocumentShare)
        .filter(
            models.DocumentShare.document_id == doc_id,
            models.DocumentShare.shared_with_user_id == user.id,
        )
        .first()
    )
    if share:
        return doc, share.permission
    raise HTTPException(status_code=403, detail="Access denied")


def _fmt_doc(doc: models.Document, user_id: int, permission: str) -> dict:
    return {
        "id": doc.id,
        "title": doc.title,
        "content_delta": doc.content_delta,
        "owner_id": doc.owner_id,
        "owner_username": doc.owner.username,
        "word_target": doc.word_target,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "is_owner": doc.owner_id == user_id,
        "permission": permission,
        "public_token": doc.public_token,
        "public_permission": doc.public_permission,
    }


def _snapshot(doc: models.Document, user_id: int, db: Session):
    """Save a version snapshot, keeping at most 10 per document."""
    versions = (
        db.query(models.DocumentVersion)
        .filter(models.DocumentVersion.document_id == doc.id)
        .order_by(models.DocumentVersion.created_at.desc())
        .all()
    )
    if len(versions) >= 10:
        db.delete(versions[-1])
    v = models.DocumentVersion(
        document_id=doc.id,
        content_delta=doc.content_delta,
        title=doc.title,
        created_by=user_id,
    )
    db.add(v)


# ── HTML routes ────────────────────────────────────────────────────────────────
@app.get("/")
async def serve_login():
    return FileResponse(str(FRONTEND_PATH / "index.html"))


@app.get("/app")
async def serve_app():
    return FileResponse(str(FRONTEND_PATH / "app.html"))


@app.get("/p/{token}")
async def serve_public_viewer(token: str):
    public_html = FRONTEND_PATH / "public.html"
    if not public_html.exists():
        raise HTTPException(status_code=404, detail="Public viewer not available")
    return FileResponse(str(public_html))


# ── Auth ───────────────────────────────────────────────────────────────────────
@app.post("/api/auth/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    body.username = body.username.strip()
    body.email = body.email.strip().lower()
    if len(body.username) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if db.query(models.User).filter(models.User.username == body.username).first():
        raise HTTPException(status_code=409, detail="Username already taken")
    if db.query(models.User).filter(models.User.email == body.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    user = models.User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token({"sub": str(user.id), "username": user.username})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "username": user.username, "email": user.email},
    }


@app.post("/api/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == body.username).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token({"sub": str(user.id), "username": user.username})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "username": user.username, "email": user.email},
    }


@app.get("/api/auth/me")
def get_me(current_user: models.User = Depends(get_current_user)):
    return {"id": current_user.id, "username": current_user.username, "email": current_user.email}


# ── Users ──────────────────────────────────────────────────────────────────────
@app.get("/api/users")
def list_users(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    users = db.query(models.User).filter(models.User.id != current_user.id).all()
    return [{"id": u.id, "username": u.username, "email": u.email} for u in users]


# ── Documents ──────────────────────────────────────────────────────────────────
@app.post("/api/documents")
def create_document(
    body: DocumentCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = models.Document(
        title=body.title,
        content_delta=body.content_delta,
        owner_id=current_user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return _fmt_doc(doc, current_user.id, "owner")


@app.get("/api/documents")
def list_documents(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owned = db.query(models.Document).filter(models.Document.owner_id == current_user.id).all()
    shares = (
        db.query(models.DocumentShare)
        .filter(models.DocumentShare.shared_with_user_id == current_user.id)
        .all()
    )
    return {
        "owned": [_fmt_doc(d, current_user.id, "owner") for d in owned],
        "shared": [_fmt_doc(s.document, current_user.id, s.permission) for s in shares],
    }


@app.get("/api/documents/{doc_id}")
def get_document(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc, permission = _require_doc_access(doc_id, current_user, db)
    response = _fmt_doc(doc, current_user.id, permission)

    section_shares = (
        db.query(models.SectionShare)
        .filter(
            models.SectionShare.document_id == doc_id,
            models.SectionShare.shared_with_user_id == current_user.id,
        )
        .all()
    )
    if section_shares:
        response["section_shares"] = [
            {
                "section_heading": s.section_heading,
                "section_index": s.section_index,
                "permission": s.permission,
            }
            for s in section_shares
        ]
    return response


@app.put("/api/documents/{doc_id}")
def update_document(
    doc_id: int,
    body: DocumentUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc, permission = _require_doc_access(doc_id, current_user, db)
    if permission not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Viewer access — cannot edit")

    if body.title is not None:
        doc.title = body.title
    if body.content_delta is not None:
        _snapshot(doc, current_user.id, db)
        doc.content_delta = body.content_delta
    if body.word_target is not None:
        doc.word_target = body.word_target

    doc.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(doc)
    return _fmt_doc(doc, current_user.id, permission)


@app.delete("/api/documents/{doc_id}")
def delete_document(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found or you are not the owner")
    db.delete(doc)
    db.commit()
    return {"message": "Document deleted"}


# ── Version history ────────────────────────────────────────────────────────────
@app.get("/api/documents/{doc_id}/versions")
def get_versions(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_doc_access(doc_id, current_user, db)
    versions = (
        db.query(models.DocumentVersion)
        .filter(models.DocumentVersion.document_id == doc_id)
        .order_by(models.DocumentVersion.created_at.desc())
        .limit(10)
        .all()
    )
    return [
        {
            "id": v.id,
            "title": v.title,
            "created_at": v.created_at.isoformat(),
            "created_by_username": v.creator.username if v.creator else "unknown",
        }
        for v in versions
    ]


@app.get("/api/documents/{doc_id}/versions/{version_id}")
def get_version(
    doc_id: int,
    version_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_doc_access(doc_id, current_user, db)
    v = (
        db.query(models.DocumentVersion)
        .filter(
            models.DocumentVersion.id == version_id,
            models.DocumentVersion.document_id == doc_id,
        )
        .first()
    )
    if not v:
        raise HTTPException(status_code=404, detail="Version not found")
    return {
        "id": v.id,
        "title": v.title,
        "content_delta": v.content_delta,
        "created_at": v.created_at.isoformat(),
    }


@app.post("/api/documents/{doc_id}/versions/{version_id}/restore")
def restore_version(
    doc_id: int,
    version_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc, permission = _require_doc_access(doc_id, current_user, db)
    if permission not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Viewer access — cannot restore versions")

    v = (
        db.query(models.DocumentVersion)
        .filter(
            models.DocumentVersion.id == version_id,
            models.DocumentVersion.document_id == doc_id,
        )
        .first()
    )
    if not v:
        raise HTTPException(status_code=404, detail="Version not found")

    # Snapshot current state before restoring
    _snapshot(doc, current_user.id, db)

    # Delete comments created after the version snapshot
    db.query(models.Comment).filter(
        models.Comment.document_id == doc_id,
        models.Comment.created_at > v.created_at,
    ).delete(synchronize_session=False)

    doc.content_delta = v.content_delta
    doc.title = v.title
    doc.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(doc)
    return _fmt_doc(doc, current_user.id, permission)


# ── Sharing ────────────────────────────────────────────────────────────────────
@app.post("/api/documents/{doc_id}/shares")
def share_document(
    doc_id: int,
    body: ShareCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can share this document")
    if body.permission not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="permission must be 'viewer' or 'editor'")

    target = db.query(models.User).filter(models.User.username == body.username).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{body.username}' not found")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    existing = (
        db.query(models.DocumentShare)
        .filter(
            models.DocumentShare.document_id == doc_id,
            models.DocumentShare.shared_with_user_id == target.id,
        )
        .first()
    )
    if existing:
        existing.permission = body.permission
    else:
        db.add(
            models.DocumentShare(
                document_id=doc_id,
                shared_with_user_id=target.id,
                permission=body.permission,
            )
        )
    db.commit()

    # Send notification email if target has an email
    if target.email:
        doc_url = f"{APP_URL}app?doc={doc_id}"
        html = share_notification_email(current_user.username, doc.title, doc_url, body.permission)
        send_email(target.email, f"{current_user.username} shared a document with you", html)

    return {"message": f"Shared with {body.username}", "permission": body.permission}


@app.get("/api/documents/{doc_id}/shares")
def get_shares(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_doc_access(doc_id, current_user, db)
    shares = (
        db.query(models.DocumentShare)
        .filter(models.DocumentShare.document_id == doc_id)
        .all()
    )
    return [
        {
            "id": s.id,
            "username": s.shared_with.username,
            "user_id": s.shared_with_user_id,
            "permission": s.permission,
            "created_at": s.created_at.isoformat(),
        }
        for s in shares
    ]


@app.delete("/api/documents/{doc_id}/shares/{share_id}")
def revoke_share(
    doc_id: int,
    share_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can revoke access")
    share = (
        db.query(models.DocumentShare)
        .filter(
            models.DocumentShare.id == share_id,
            models.DocumentShare.document_id == doc_id,
        )
        .first()
    )
    if not share:
        raise HTTPException(status_code=404, detail="Share entry not found")
    db.delete(share)
    db.commit()
    return {"message": "Access revoked"}


# ── Email invitations ──────────────────────────────────────────────────────────
@app.post("/api/documents/{doc_id}/invite")
def invite_by_email(
    doc_id: int,
    body: InviteCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can send invitations")
    if body.permission not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="permission must be 'viewer' or 'editor'")

    email = body.email.strip().lower()

    # If the email belongs to an existing user, share directly
    existing_user = db.query(models.User).filter(models.User.email == email).first()
    if existing_user:
        if existing_user.id == current_user.id:
            raise HTTPException(status_code=400, detail="Cannot invite yourself")
        share = (
            db.query(models.DocumentShare)
            .filter(
                models.DocumentShare.document_id == doc_id,
                models.DocumentShare.shared_with_user_id == existing_user.id,
            )
            .first()
        )
        if share:
            share.permission = body.permission
        else:
            db.add(models.DocumentShare(
                document_id=doc_id,
                shared_with_user_id=existing_user.id,
                permission=body.permission,
            ))
        db.commit()
        doc_url = f"{APP_URL}app?doc={doc_id}"
        html = share_notification_email(current_user.username, doc.title, doc_url, body.permission)
        sent = send_email(email, f"{current_user.username} shared a document with you", html)
        return {"message": "User found and shared directly", "email_sent": sent}

    # Otherwise create an invitation token
    token = secrets.token_urlsafe(64)
    expires = datetime.utcnow() + timedelta(days=7)
    inv = models.DocumentInvitation(
        document_id=doc_id,
        invited_email=email,
        permission=body.permission,
        token=token,
        expires_at=expires,
    )
    db.add(inv)
    db.commit()

    invite_link = f"{APP_URL}?invite={token}"
    html = invite_email(current_user.username, doc.title, invite_link, body.permission)
    sent = send_email(email, f"{current_user.username} invited you to collaborate", html)

    return {
        "message": "Invitation created",
        "invite_link": invite_link,
        "email_sent": sent,
    }


@app.post("/api/invitations/accept")
def accept_invitation(
    token: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inv = (
        db.query(models.DocumentInvitation)
        .filter(
            models.DocumentInvitation.token == token,
            models.DocumentInvitation.accepted == False,
        )
        .first()
    )
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found or already used")
    if inv.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Invitation has expired")

    # Grant access
    existing = (
        db.query(models.DocumentShare)
        .filter(
            models.DocumentShare.document_id == inv.document_id,
            models.DocumentShare.shared_with_user_id == current_user.id,
        )
        .first()
    )
    if existing:
        existing.permission = inv.permission
    else:
        db.add(models.DocumentShare(
            document_id=inv.document_id,
            shared_with_user_id=current_user.id,
            permission=inv.permission,
        ))

    inv.accepted = True
    db.commit()
    return {"message": "Invitation accepted", "doc_id": inv.document_id, "permission": inv.permission}


# ── Public link sharing ────────────────────────────────────────────────────────
@app.post("/api/documents/{doc_id}/public-link")
def enable_public_link(
    doc_id: int,
    body: PublicLinkCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can manage the public link")
    if body.permission not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="permission must be 'viewer' or 'editor'")

    if not doc.public_token:
        doc.public_token = secrets.token_urlsafe(32)
    doc.public_permission = body.permission
    db.commit()

    public_url = f"{APP_URL}p/{doc.public_token}"
    return {
        "public_token": doc.public_token,
        "public_permission": doc.public_permission,
        "public_url": public_url,
    }


@app.delete("/api/documents/{doc_id}/public-link")
def disable_public_link(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can manage the public link")
    doc.public_token = None
    doc.public_permission = None
    db.commit()
    return {"message": "Public link disabled"}


@app.get("/api/public/{token}")
def get_public_document(token: str, db: Session = Depends(get_db)):
    doc = (
        db.query(models.Document)
        .filter(models.Document.public_token == token)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Public document not found")
    return {
        "id": doc.id,
        "title": doc.title,
        "content_delta": doc.content_delta,
        "owner_username": doc.owner.username,
        "permission": doc.public_permission,
        "updated_at": doc.updated_at.isoformat(),
    }


# ── Access requests ────────────────────────────────────────────────────────────
@app.post("/api/documents/{doc_id}/access-requests")
def create_access_request(
    doc_id: int,
    body: AccessRequestCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.owner_id == current_user.id:
        raise HTTPException(status_code=400, detail="You own this document")

    existing = (
        db.query(models.AccessRequest)
        .filter(
            models.AccessRequest.document_id == doc_id,
            models.AccessRequest.requester_id == current_user.id,
            models.AccessRequest.status == "pending",
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="You already have a pending request")

    req = models.AccessRequest(
        document_id=doc_id,
        requester_id=current_user.id,
        requested_permission=body.requested_permission,
        message=body.message,
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # Email the owner
    owner = doc.owner
    if owner.email:
        doc_url = f"{APP_URL}app?doc={doc_id}"
        html = access_request_email(
            current_user.username,
            current_user.email,
            doc.title,
            body.requested_permission,
            body.message or "",
            doc_url,
        )
        send_email(owner.email, f"{current_user.username} is requesting access to '{doc.title}'", html)

    return {"id": req.id, "status": req.status, "message": "Access request submitted"}


@app.get("/api/documents/{doc_id}/access-requests")
def list_access_requests(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can view access requests")
    reqs = (
        db.query(models.AccessRequest)
        .filter(
            models.AccessRequest.document_id == doc_id,
            models.AccessRequest.status == "pending",
        )
        .all()
    )
    return [
        {
            "id": r.id,
            "requester_id": r.requester_id,
            "requester_username": r.requester.username,
            "requester_email": r.requester.email,
            "requested_permission": r.requested_permission,
            "message": r.message,
            "created_at": r.created_at.isoformat(),
        }
        for r in reqs
    ]


@app.put("/api/access-requests/{request_id}")
def decide_access_request(
    request_id: int,
    body: AccessRequestDecision,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    req = db.query(models.AccessRequest).filter(models.AccessRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Access request not found")

    doc = (
        db.query(models.Document)
        .filter(models.Document.id == req.document_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the document owner can decide access requests")

    req.status = "approved" if body.approved else "denied"

    if body.approved:
        existing = (
            db.query(models.DocumentShare)
            .filter(
                models.DocumentShare.document_id == req.document_id,
                models.DocumentShare.shared_with_user_id == req.requester_id,
            )
            .first()
        )
        if existing:
            existing.permission = req.requested_permission
        else:
            db.add(models.DocumentShare(
                document_id=req.document_id,
                shared_with_user_id=req.requester_id,
                permission=req.requested_permission,
            ))

    db.commit()

    # Email the requester
    requester = req.requester
    if requester.email:
        html = access_decision_email(doc.title, body.approved)
        subject = f"Your access request for '{doc.title}' was {'approved' if body.approved else 'denied'}"
        send_email(requester.email, subject, html)

    return {"status": req.status}


# ── Section sharing ────────────────────────────────────────────────────────────
@app.post("/api/documents/{doc_id}/section-shares")
def share_section(
    doc_id: int,
    body: SectionShareCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can share sections")

    target = db.query(models.User).filter(models.User.username == body.username).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{body.username}' not found")

    existing = (
        db.query(models.SectionShare)
        .filter(
            models.SectionShare.document_id == doc_id,
            models.SectionShare.shared_with_user_id == target.id,
            models.SectionShare.section_heading == body.section_heading,
        )
        .first()
    )
    if existing:
        existing.permission = body.permission
    else:
        db.add(
            models.SectionShare(
                document_id=doc_id,
                section_heading=body.section_heading,
                section_index=body.section_index,
                shared_with_user_id=target.id,
                permission=body.permission,
            )
        )
    db.commit()
    return {"message": f"Section '{body.section_heading}' shared with {body.username}"}


@app.get("/api/documents/{doc_id}/section-shares")
def get_section_shares(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc, _ = _require_doc_access(doc_id, current_user, db)
    ss = (
        db.query(models.SectionShare)
        .filter(models.SectionShare.document_id == doc_id)
        .all()
    )
    return [
        {
            "id": s.id,
            "section_heading": s.section_heading,
            "section_index": s.section_index,
            "username": s.shared_with.username,
            "user_id": s.shared_with_user_id,
            "permission": s.permission,
        }
        for s in ss
    ]


@app.delete("/api/documents/{doc_id}/section-shares/{ss_id}")
def revoke_section_share(
    doc_id: int,
    ss_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the owner can revoke section access")
    ss = (
        db.query(models.SectionShare)
        .filter(models.SectionShare.id == ss_id, models.SectionShare.document_id == doc_id)
        .first()
    )
    if not ss:
        raise HTTPException(status_code=404, detail="Section share not found")
    db.delete(ss)
    db.commit()
    return {"message": "Section access revoked"}


# ── Comments ───────────────────────────────────────────────────────────────────
@app.post("/api/documents/{doc_id}/comments")
def add_comment(
    doc_id: int,
    body: CommentCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_doc_access(doc_id, current_user, db)
    c = models.Comment(
        document_id=doc_id,
        user_id=current_user.id,
        content=body.content,
        selection_index=body.selection_index,
        selection_length=body.selection_length,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return {
        "id": c.id,
        "content": c.content,
        "user_id": c.user_id,
        "author_username": current_user.username,
        "selection_index": c.selection_index,
        "selection_length": c.selection_length,
        "resolved": c.resolved,
        "created_at": c.created_at.isoformat(),
    }


@app.get("/api/documents/{doc_id}/comments")
def get_comments(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_doc_access(doc_id, current_user, db)
    comments = (
        db.query(models.Comment)
        .filter(models.Comment.document_id == doc_id)
        .order_by(models.Comment.created_at.asc())
        .all()
    )
    return [
        {
            "id": c.id,
            "content": c.content,
            "user_id": c.user_id,
            "author_username": c.author.username,
            "selection_index": c.selection_index,
            "selection_length": c.selection_length,
            "resolved": c.resolved,
            "created_at": c.created_at.isoformat(),
        }
        for c in comments
    ]


@app.put("/api/comments/{comment_id}/resolve")
def toggle_resolve(
    comment_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = db.query(models.Comment).filter(models.Comment.id == comment_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Comment not found")
    _require_doc_access(c.document_id, current_user, db)
    c.resolved = not c.resolved
    db.commit()
    return {"resolved": c.resolved}


@app.delete("/api/comments/{comment_id}")
def delete_comment(
    comment_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = (
        db.query(models.Comment)
        .filter(models.Comment.id == comment_id, models.Comment.user_id == current_user.id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Comment not found or not yours")
    db.delete(c)
    db.commit()
    return {"message": "Comment deleted"}


# ── File upload ────────────────────────────────────────────────────────────────
ALLOWED_DOC_EXTENSIONS = {".txt", ".md", ".docx"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


async def _parse_upload(file: UploadFile, save_path: Path) -> str:
    content_bytes = await file.read()
    async with aiofiles.open(save_path, "wb") as f:
        await f.write(content_bytes)

    ext = Path(file.filename).suffix.lower()
    if ext == ".txt":
        return txt_to_delta(content_bytes.decode("utf-8", errors="replace"))
    if ext == ".md":
        return md_to_delta(content_bytes.decode("utf-8", errors="replace"))
    if ext == ".docx":
        return docx_to_delta(str(save_path))
    raise ValueError(f"Unsupported extension: {ext}")


@app.post("/api/documents/upload-new")
async def upload_create(
    file: UploadFile = File(...),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_DOC_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_DOC_EXTENSIONS))}",
        )
    safe_name = f"new_{current_user.id}_{uuid.uuid4().hex}{ext}"
    save_path = UPLOADS_DIR / safe_name
    try:
        delta = await _parse_upload(file, save_path)
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Could not parse file: {e}")

    title = Path(file.filename).stem.replace("-", " ").replace("_", " ").title()
    doc = models.Document(title=title, content_delta=delta, owner_id=current_user.id)
    db.add(doc)
    db.commit()
    db.refresh(doc)

    db.add(
        models.FileAttachment(
            document_id=doc.id,
            filename=file.filename,
            file_type=ext,
            stored_path=str(save_path),
            file_size=save_path.stat().st_size,
        )
    )
    db.commit()
    db.refresh(doc)
    return _fmt_doc(doc, current_user.id, "owner")


@app.post("/api/documents/{doc_id}/upload")
async def upload_into_document(
    doc_id: int,
    file: UploadFile = File(...),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc, permission = _require_doc_access(doc_id, current_user, db)
    if permission not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Viewer access — cannot edit")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_DOC_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_DOC_EXTENSIONS))}",
        )
    safe_name = f"{doc_id}_{current_user.id}_{uuid.uuid4().hex}{ext}"
    save_path = UPLOADS_DIR / safe_name
    try:
        delta = await _parse_upload(file, save_path)
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Could not parse file: {e}")

    db.add(
        models.FileAttachment(
            document_id=doc_id,
            filename=file.filename,
            file_type=ext,
            stored_path=str(save_path),
            file_size=save_path.stat().st_size,
        )
    )
    db.commit()
    return {"delta": delta, "filename": file.filename}


@app.post("/api/documents/{doc_id}/images")
async def upload_image(
    doc_id: int,
    file: UploadFile = File(...),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc, permission = _require_doc_access(doc_id, current_user, db)
    if permission not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Viewer access — cannot upload images")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}",
        )

    safe_name = f"img_{doc_id}_{uuid.uuid4().hex}{ext}"
    save_path = UPLOADS_DIR / safe_name
    content_bytes = await file.read()
    async with aiofiles.open(save_path, "wb") as f:
        await f.write(content_bytes)

    db.add(
        models.FileAttachment(
            document_id=doc_id,
            filename=file.filename,
            file_type=ext,
            stored_path=str(save_path),
            file_size=len(content_bytes),
        )
    )
    db.commit()

    image_url = f"{APP_URL}uploads/{safe_name}"
    return {"url": image_url, "filename": safe_name}


# ── Attachments ────────────────────────────────────────────────────────────────
@app.get("/api/documents/{doc_id}/attachments")
def list_attachments(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_doc_access(doc_id, current_user, db)
    attachments = (
        db.query(models.FileAttachment)
        .filter(models.FileAttachment.document_id == doc_id)
        .order_by(models.FileAttachment.created_at.desc())
        .all()
    )
    return [
        {
            "id": a.id,
            "filename": a.filename,
            "file_type": a.file_type,
            "file_size": a.file_size,
            "created_at": a.created_at.isoformat(),
        }
        for a in attachments
    ]


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(
    attachment_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    att = db.query(models.FileAttachment).filter(models.FileAttachment.id == attachment_id).first()
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == att.document_id, models.Document.owner_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=403, detail="Only the document owner can delete attachments")
    try:
        Path(att.stored_path).unlink(missing_ok=True)
    except Exception:
        pass
    db.delete(att)
    db.commit()
    return {"message": "Attachment deleted"}


# ── Grammar check (proxied to LanguageTool free API — no key required) ─────────
@app.post("/api/grammar-check")
async def grammar_check(
    body: GrammarCheckRequest,
    current_user: models.User = Depends(get_current_user),
):
    if len(body.text) > 20000:
        raise HTTPException(status_code=400, detail="Text exceeds 20,000 character limit for grammar check")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.languagetool.org/v2/check",
                data={"text": body.text, "language": body.language, "enabledOnly": "false"},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Grammar service timed out — try again")
    except Exception:
        raise HTTPException(status_code=503, detail="Grammar service temporarily unavailable")


# ── Export ─────────────────────────────────────────────────────────────────────
@app.get("/api/documents/{doc_id}/export/markdown")
def export_markdown(
    doc_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    doc, _ = _require_doc_access(doc_id, current_user, db)
    try:
        delta = json.loads(doc.content_delta)
    except Exception:
        raise HTTPException(status_code=500, detail="Document content is corrupt")

    lines: list[str] = []
    buf = ""
    for op in delta.get("ops", []):
        ins = op.get("insert", "")
        if not isinstance(ins, str):
            continue
        attrs = op.get("attributes") or {}
        if ins == "\n" or ins.endswith("\n"):
            header = attrs.get("header")
            list_type = attrs.get("list")
            blockquote = attrs.get("blockquote")
            if header:
                lines.append(f"{'#' * header} {buf.strip()}")
            elif list_type == "bullet":
                lines.append(f"- {buf.strip()}")
            elif list_type == "ordered":
                lines.append(f"1. {buf.strip()}")
            elif blockquote:
                lines.append(f"> {buf.strip()}")
            else:
                lines.append(buf)
            buf = ""
        else:
            fragment = ins
            if attrs.get("bold") and attrs.get("italic"):
                fragment = f"***{ins}***"
            elif attrs.get("bold"):
                fragment = f"**{ins}**"
            elif attrs.get("italic"):
                fragment = f"*{ins}*"
            elif attrs.get("underline"):
                fragment = f"<u>{ins}</u>"
            buf += fragment

    md = "\n".join(lines)
    return JSONResponse({"filename": f"{doc.title}.md", "content": md})


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws/documents/{doc_id}")
async def ws_endpoint(websocket: WebSocket, doc_id: int, token: str):
    from jose import JWTError

    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
        username = payload["username"]
    except (JWTError, KeyError, ValueError):
        await websocket.close(code=4001)
        return

    db = SessionLocal()
    try:
        doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
        if not doc:
            await websocket.close(code=4004)
            return
        has_access = doc.owner_id == user_id or db.query(models.DocumentShare).filter(
            models.DocumentShare.document_id == doc_id,
            models.DocumentShare.shared_with_user_id == user_id,
        ).first()
        if not has_access:
            await websocket.close(code=4003)
            return
    finally:
        db.close()

    await presence.join(websocket, doc_id, user_id, username)
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")
            if msg_type == "cursor":
                await presence.broadcast_to_others(websocket, doc_id, {
                    "type": "cursor",
                    "user_id": user_id,
                    "username": username,
                    "index": msg.get("index"),
                    "length": msg.get("length"),
                })
            elif msg_type == "content_update":
                await presence.broadcast_to_others(websocket, doc_id, {
                    "type": "content_update",
                    "user_id": user_id,
                    "username": username,
                    "full_content": msg.get("full_content"),
                })
    except WebSocketDisconnect:
        pass
    finally:
        presence.leave(websocket, doc_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
