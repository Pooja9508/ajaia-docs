from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    documents    = relationship("Document", back_populates="owner", foreign_keys="Document.owner_id")
    shares       = relationship("DocumentShare", back_populates="shared_with", foreign_keys="DocumentShare.shared_with_user_id")
    comments     = relationship("Comment", back_populates="author")


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False, default="Untitled Document")
    content_delta = Column(Text, default='{"ops":[{"insert":"\\n"}]}')
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    word_target = Column(Integer, nullable=True)
    # Public link sharing
    public_token      = Column(String(100), unique=True, nullable=True, index=True)
    public_permission = Column(String(20), nullable=True)   # "viewer" | "editor" | None
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner          = relationship("User", back_populates="documents", foreign_keys=[owner_id])
    shares         = relationship("DocumentShare", back_populates="document", cascade="all, delete-orphan")
    section_shares = relationship("SectionShare",  back_populates="document", cascade="all, delete-orphan")
    comments       = relationship("Comment",        back_populates="document", cascade="all, delete-orphan")
    versions       = relationship("DocumentVersion", back_populates="document", cascade="all, delete-orphan")
    attachments    = relationship("FileAttachment", back_populates="document", cascade="all, delete-orphan")
    invitations    = relationship("DocumentInvitation", back_populates="document", cascade="all, delete-orphan")
    access_requests = relationship("AccessRequest", back_populates="document", cascade="all, delete-orphan")


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    content_delta = Column(Text, nullable=False)
    title = Column(String(255), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="versions")
    creator  = relationship("User", foreign_keys=[created_by])


class DocumentShare(Base):
    __tablename__ = "document_shares"
    id = Column(Integer, primary_key=True, index=True)
    document_id        = Column(Integer, ForeignKey("documents.id"), nullable=False)
    shared_with_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    permission = Column(String(20), nullable=False, default="viewer")
    created_at = Column(DateTime, default=datetime.utcnow)

    document     = relationship("Document", back_populates="shares")
    shared_with  = relationship("User", back_populates="shares", foreign_keys=[shared_with_user_id])


class SectionShare(Base):
    __tablename__ = "section_shares"
    id = Column(Integer, primary_key=True, index=True)
    document_id        = Column(Integer, ForeignKey("documents.id"), nullable=False)
    section_heading    = Column(String(500), nullable=False)
    section_index      = Column(Integer, nullable=False)
    shared_with_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    permission = Column(String(20), nullable=False, default="viewer")
    created_at = Column(DateTime, default=datetime.utcnow)

    document    = relationship("Document", back_populates="section_shares")
    shared_with = relationship("User", foreign_keys=[shared_with_user_id])


class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True, index=True)
    document_id      = Column(Integer, ForeignKey("documents.id"), nullable=False)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False)
    content          = Column(Text, nullable=False)
    selection_index  = Column(Integer, nullable=True)
    selection_length = Column(Integer, nullable=True)
    resolved         = Column(Boolean, default=False)
    created_at       = Column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="comments")
    author   = relationship("User", back_populates="comments")


class FileAttachment(Base):
    __tablename__ = "file_attachments"
    id = Column(Integer, primary_key=True, index=True)
    document_id  = Column(Integer, ForeignKey("documents.id"), nullable=False)
    filename     = Column(String(255), nullable=False)
    file_type    = Column(String(50), nullable=False)
    stored_path  = Column(String(500), nullable=False)
    file_size    = Column(Integer, nullable=True)   # bytes
    created_at   = Column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="attachments")


class DocumentInvitation(Base):
    __tablename__ = "document_invitations"
    id = Column(Integer, primary_key=True, index=True)
    document_id    = Column(Integer, ForeignKey("documents.id"), nullable=False)
    invited_email  = Column(String(100), nullable=False)
    permission     = Column(String(20), nullable=False, default="viewer")
    token          = Column(String(128), unique=True, nullable=False, index=True)
    accepted       = Column(Boolean, default=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    expires_at     = Column(DateTime, nullable=False)

    document = relationship("Document", back_populates="invitations")


class AccessRequest(Base):
    __tablename__ = "access_requests"
    id = Column(Integer, primary_key=True, index=True)
    document_id           = Column(Integer, ForeignKey("documents.id"), nullable=False)
    requester_id          = Column(Integer, ForeignKey("users.id"), nullable=False)
    requested_permission  = Column(String(20), nullable=False, default="editor")
    status                = Column(String(20), nullable=False, default="pending")  # pending|approved|denied
    message               = Column(Text, nullable=True)
    created_at            = Column(DateTime, default=datetime.utcnow)

    document  = relationship("Document", back_populates="access_requests")
    requester = relationship("User", foreign_keys=[requester_id])
